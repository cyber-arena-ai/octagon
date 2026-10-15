"""Metering: usage extraction, cost calc, JSONL sink.

The proxy doesn't translate formats, so usage is read straight off whatever the
upstream returned. ``extract_usage`` is format-agnostic: it reads OpenAI
(``prompt_tokens`` / ``completion_tokens``), Anthropic
(``input_tokens`` / ``output_tokens``), and Gemini native
(``promptTokenCount`` / ``candidatesTokenCount``) shapes alike. Cost comes only
from the config ``pricing`` table (no litellm).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any


def extract_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """Normalize an OpenAI-, Anthropic-, or Gemini-shaped ``usage`` object to
    {input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens},
    where ``input_tokens`` is always the **plain, uncached** input (cache buckets
    reported separately) regardless of provider — so a caller can bill the three
    buckets additively without double-counting.

    Takes the **max** across aliases rather than first-non-None: some upstream
    proxies (e.g. ppapi serving Claude/Gemini in OpenAI shape) populate BOTH
    sets of names but leave the Anthropic-side ``input_tokens``/``output_tokens``
    stuck at 0 while ``prompt_tokens``/``completion_tokens`` carry the real
    counts. Picking first-non-None would lock onto the 0; max() ignores it."""
    usage = usage or {}

    def _g(*keys: str) -> int:
        vals = [usage.get(k) for k in keys]
        vals = [int(v) for v in vals if v is not None]
        return max(vals) if vals else 0

    # OpenAI chat nests cached tokens under prompt_tokens_details; the Responses
    # API (codex/ppapi) uses input_tokens_details, and there cache WRITES ride
    # `cache_write_tokens` (not the top-level Anthropic
    # `cache_creation_input_tokens`). Gemini native reports cache hits as
    # `cachedContentTokenCount`; official docs say `promptTokenCount` remains
    # cache-inclusive, so normalize it the same way. Read whichever names are
    # present so all providers' cache buckets get billed.
    details = (usage.get("prompt_tokens_details")
               or usage.get("input_tokens_details") or {})
    cache_read = (
        _g("cache_read_input_tokens")
        or int(details.get("cached_tokens", 0) or 0)
        or _g("cachedContentTokenCount")
    )
    cache_creation = _g("cache_creation_input_tokens") or int(details.get("cache_write_tokens", 0) or 0)

    # Cache convention differs by provider. OpenAI Chat/Responses fold the cache
    # buckets INTO prompt_tokens/input_tokens (cached_tokens is a *subset* of that
    # total — confirmed against a live ppapi Responses reply), and mark the shape with
    # a `*_tokens_details` object. Anthropic reports input_tokens already EXCLUDING the
    # cache buckets and carries no details object. Note the Responses API reuses the
    # name `input_tokens` with OpenAI semantics, so the field name alone can't tell the
    # two apart — the details object is the signal. Strip the cache buckets only in the
    # OpenAI/Responses case so input_tokens is the plain uncached input everywhere;
    # billing cache_read/cache_write on top then never double-counts.
    input_tokens = _g("input_tokens", "prompt_tokens", "promptTokenCount")
    has_details = (
        ("prompt_tokens_details" in usage)
        or ("input_tokens_details" in usage)
        or ("promptTokenCount" in usage)
    )
    if has_details:
        input_tokens = max(0, input_tokens - cache_read - cache_creation)

    return {
        "input_tokens": input_tokens,
        "output_tokens": _g("output_tokens", "completion_tokens", "candidatesTokenCount"),
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }


def cost_from_pricing(
    pricing: dict[str, dict[str, float]],
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float | None:
    """USD cost from a ``{model: {input, output, cache_read, cache_write}}`` table
    (rates per 1M tokens). Returns None when the model is absent so the caller
    leaves cost null.

    Cache tokens are billed separately from plain input/output: ``cache_read``
    prices a cache HIT (``cache_read_input_tokens`` — usually far cheaper than a
    fresh input token) and ``cache_write`` prices a cache MISS that populates the
    cache (``cache_creation_input_tokens`` — usually pricier than a fresh input
    token). Both rate fields are optional and default to 0.0, so a pricing entry
    with only ``{input, output}`` still works exactly as before; it just leaves
    cache tokens unbilled rather than erroring."""
    rates = pricing.get(model)
    if rates is None:
        return None
    in_rate = float(rates.get("input", 0.0))
    out_rate = float(rates.get("output", 0.0))
    cache_read_rate = float(rates.get("cache_read", 0.0))
    cache_write_rate = float(rates.get("cache_write", 0.0))
    return (input_tokens * in_rate + output_tokens * out_rate
            + cache_read_tokens * cache_read_rate
            + cache_creation_tokens * cache_write_rate) / 1_000_000.0


_REDACTED = "<redacted>"

# Shape patterns for provider credentials that an upstream error body might echo
# back (the key we injected on forward, or one a proxy quotes in an auth message).
# Applied to the RAW string so a key embedded inside a longer JSON value is still
# caught. Specific shapes first; the last is a catch-all for unknown long secrets.
_KEY_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),          # Anthropic
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),              # OpenAI / reseller
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),              # Google
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-/+=]{12,}"),  # Authorization: Bearer <tok>
    re.compile(r"\bcad-[A-Za-z0-9_\-]{4,}"),            # a router principal token
    re.compile(r"[A-Za-z0-9+/_\-]{40,}={0,2}"),         # catch-all long blob
)


def redact(text: str, *, known_secrets: tuple[str, ...] = (), limit: int = 600) -> str:
    """Scrub secrets from a string before it enters a log line or a JSONL row.

    Three passes, in order: (1) exact-strip any KNOWN secret (pass the injected
    upstream key + the client token so a verbatim echo is fully removed); (2)
    shape-match unknown/echoed keys on the raw text; (3) truncate to ``limit``
    bytes AFTER redaction, so a secret straddling the cap cannot survive as a
    prefix. Pure — no I/O — so it is unit-tested directly (test_apirouter_redact)."""
    if not text:
        return text
    for s in known_secrets:
        if s and len(s) >= 8:
            text = text.replace(s, _REDACTED)
    for pat in _KEY_PATTERNS:
        text = pat.sub(_REDACTED, text)
    if len(text) > limit:
        text = text[:limit] + "…(truncated)"
    return text


def token_fingerprint(token: str | None) -> str:
    """A non-reversible label for a rejected token — enough to tell two bad tokens
    apart and to spot a stale-vs-absent credential, without ever writing the token
    itself (it may be a live principal token or a real provider key). Never log the
    token; log this."""
    if not token:
        return "missing"
    return "sha256:" + hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:8]


class MeteringSink:
    """Append-only JSONL writer. Best-effort: a write failure is swallowed so
    metering never takes down a request."""

    def __init__(self, jsonl_path: str | Path | None = None, stdout: bool = True) -> None:
        self.stdout = stdout
        self._f = None
        if jsonl_path:
            p = Path(jsonl_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._f = open(p, "a", buffering=1)  # line-buffered

    def record(self, event: dict[str, Any]) -> None:
        rec = {"ts": time.time(), **event}
        line = json.dumps(rec, default=str)
        if self._f is not None:
            try:
                self._f.write(line + "\n")
            except Exception:
                pass
        if self.stdout:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except Exception:
                pass

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.flush()
                self._f.close()
            except Exception:
                pass
