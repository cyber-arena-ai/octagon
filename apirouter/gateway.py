"""Reverse passthrough gateway + LLM metering.

The agent points its base_url at this gateway (plain HTTP inside the docker net).
Every request is forwarded **verbatim** to ``<upstream>.base_url + <path>`` — same
method, path, query, headers, body — and the upstream response is returned
unchanged. No format translation: whatever the client speaks (OpenAI / Anthropic)
is what the upstream gets.

*Which* upstream is chosen per request from the client's model name (``routes``
in the config), so one router can front several providers on different keys —
e.g. ``claude-*`` to api.anthropic.com on the official key, everything else to a
reseller. Each upstream carries its own key; the client only ever holds its
principal token.

Two side effects on the way through:
  * **markdown repair** — a non-streaming JSON body wrapped in a ```json fence is
    unwrapped (sanitize.py) so the client gets clean JSON.
  * **metering** — if the response carries a ``usage`` object (OpenAI or Anthropic
    shape), we read it + the request's model, compute cost from the pricing
    table, and append a JSONL row. Streaming responses are teed and metered at
    the end. Non-API traffic just passes through, unmetered.

**Operator debuggability.** Every request is stamped with a short correlation id that
prefixes each log line it emits (a retrying request emits several) and tags its JSONL
row, so one request's scattered log lines regroup under `grep req[<id>]`. Router-
generated error responses also echo it in their JSON body (``request_id``). Every
FAILED forward — a protocol-mismatch 404, an upstream 4xx/5xx,
a transport error, a mid-stream SSE error, an aborted stream, a pre-forward reject
— is logged (4xx=WARNING, 5xx/transport=ERROR) with team/upstream/model/path/status
/latency and a truncated+redacted upstream error body, and leaves an ``llm_error``
(or ``auth_reject`` / ``budget_block``) row in the sink. Verbatim passthrough is
unchanged: errors are surfaced, never rewritten. Secrets never reach a log or a
row — the injected upstream key and the client's principal token are scrubbed
(``metering.redact`` / ``token_fingerprint``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .budget import Budget
from .compat import (downgrade_to_json_object, is_json_schema_request,
                     normalize_tool_schemas, sanitize_request_body)
from .config import RouterConfig, Upstream
from .metering import (MeteringSink, cost_from_pricing, extract_usage, redact,
                       token_fingerprint)
from .sanitize import unwrap_markdown_json

log = logging.getLogger("apirouter.gateway")

# Hop-by-hop / connection-management headers we must not forward verbatim.
_HOP = {
    "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "accept-encoding",
}

_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]


def _new_request_id() -> str:
    # Prefixes every log line one request emits (per-retry, downgrade, final failure),
    # so `grep req[<id>]` regroups them when concurrent requests interleave in stdout.
    # Random, NOT derived from the client token — a token-derived id would leak whether
    # two requests share a key.
    return secrets.token_hex(6)


def _parse_model(body: bytes) -> str | None:
    try:
        return json.loads(body).get("model")
    except Exception:
        return None


def _usage_payload(obj: dict) -> dict | None:
    usage = (
        obj.get("usage")
        or (obj.get("message") or {}).get("usage")
        or (obj.get("response") or {}).get("usage")
        or obj.get("usageMetadata")
    )
    return usage if isinstance(usage, dict) else None


def _provider_model(obj: dict) -> str | None:
    for raw in (
        obj.get("modelVersion"),
        obj.get("model"),
        (obj.get("response") or {}).get("modelVersion"),
        (obj.get("response") or {}).get("model"),
        (obj.get("message") or {}).get("model"),
    ):
        if raw:
            return str(raw)
    return None


def _client_token(request: Request) -> str | None:
    tok = (
        request.headers.get("x-api-key")
        or request.headers.get("x-goog-api-key")
    )
    if tok:
        return tok
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _merge_stream_usage(buf: bytes) -> tuple[dict[str, int] | None, str | None]:
    """Pull usage out of an SSE body. Handles three shapes:
      - OpenAI chat include_usage chunk        -> evt.usage
      - Anthropic message_start/message_delta  -> evt.message.usage
      - OpenAI Responses API (codex)           -> evt.response.usage
      - Gemini native generateContent          -> evt.usageMetadata
        (the terminal `response.completed` event carries the cumulative usage;
        codex's own stdout `turn.completed` never fires for a stopped async
        agent, but each /responses HTTP call still completes with usage here).
    Takes the max of each field across events, and best-effort captures the
    provider-reported model version when present."""
    best: dict[str, int] = {}
    found = False
    provider_model = None
    for line in buf.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            evt = json.loads(payload)
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        provider_model = provider_model or _provider_model(evt)
        usage = _usage_payload(evt)
        if not usage:
            continue
        found = True
        u = extract_usage(usage)
        for k, v in u.items():
            best[k] = max(best.get(k, 0), v)
    return (best if found else None, provider_model)


# A server-side CYBER-POLICY refusal carried inside an SSE error event — OpenAI/ppapi's
# `code:"cyber_policy"` (with its "flagged for possible cybersecurity risk" message), the
# cross-vendor analog of Anthropic's cyber safeguard refusal. Labeled here so it can be
# COUNTED as a refusal, not a generic transport error: codex only surfaces "response.failed
# event received", hiding the cause, so without this the harness miscounts it as AGENT_FAILED.
_CYBER_REFUSAL_CODES = frozenset({"cyber_policy"})
_CYBER_REFUSAL_HINTS = ("flagged for possible cybersecurity risk",
                        "trusted access for cyber")


def _refusal_category_from_err(code: str, message: str) -> str | None:
    """"cyber" if this upstream SSE error is a server-side cyber-policy refusal, else None.
    Matches on the machine code first (`cyber_policy`), then on the message text as a
    fallback for resellers that drop the code but keep the wording."""
    if code in _CYBER_REFUSAL_CODES:
        return "cyber"
    ml = message.lower()
    if any(h in ml for h in _CYBER_REFUSAL_HINTS):
        return "cyber"
    return None


def _sse_error_event(buf: bytes) -> dict[str, str] | None:
    """Detect an error carried INSIDE an event stream — a 200 + SSE headers that
    then fails mid-stream: Anthropic `event: error / data:{"type":"error",...}` or
    the OpenAI-shape `data:{"error":{...}}`. Returns a SMALL dict ({type, message},
    plus `code` and `refusal_category` when present) — never the completion text /
    the full buffer, which is model output + any flag the agent just read — or None.
    This is what makes a mid-stream failure visible instead of being recorded as a
    clean 200 success, and what labels an OpenAI cyber-policy refusal as a refusal."""
    for line in buf.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            evt = json.loads(payload)
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        err = evt.get("error")
        if not isinstance(err, dict) and evt.get("type") == "error":
            err = {"type": "error"}
        if isinstance(err, dict):
            code = str(err.get("code") or "")
            message = str(err.get("message") or "")[:300]
            out = {"type": str(err.get("type") or "error"), "message": message}
            if code:
                out["code"] = code
            cat = _refusal_category_from_err(code, message)
            if cat:
                out["refusal_category"] = cat
            return out
    return None


def build_app(
    cfg: RouterConfig,
    *,
    sink: MeteringSink | None = None,
    client: httpx.AsyncClient | None = None,
    lifespan=None,
) -> FastAPI:
    app = FastAPI(title="apirouter", lifespan=lifespan)
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(600.0, connect=15.0), follow_redirects=True
    )
    budget = Budget(cfg.budgets)
    unpriced_seen: set[str] = set()   # rate-limit the unpriced-model warning to once/model

    def _fwd_headers(request: Request, upstream: Upstream) -> dict[str, str]:
        h = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        h["accept-encoding"] = "identity"  # so we can read/repair the text body
        # Always swap in the routed upstream's real key. What the client sent is a
        # principal token the router minted for billing, never a provider credential,
        # so it must not leave the router — and there is no case where forwarding it
        # would work. Covers OpenAI (Authorization), Anthropic / reseller
        # (x-api-key), and Gemini native (x-goog-api-key) clients.
        for k in list(h):
            if k.lower() in ("authorization", "x-api-key", "x-goog-api-key"):
                del h[k]
        h["authorization"] = f"Bearer {upstream.api_key}"
        h["x-api-key"] = upstream.api_key
        h["x-goog-api-key"] = upstream.api_key
        return h

    def _resp_headers(resp: httpx.Response) -> dict[str, str]:
        return {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}

    async def _forward(rid, upstream_id, method, url, params, headers, body):
        """Send upstream with optional transient retry (#10) + json_schema
        downgrade-retry (#4). Returns (response, attempts): an httpx.Response
        opened with stream=True (or an eager one reconstructed when a non-matching
        400 had to be read), plus the number of upstream sends performed. Retries
        and the silent request-mutating downgrade are LOGGED (never the body/headers,
        which carry the agent prompt + the injected key)."""
        c = cfg.compat
        attempt, cur, tried_downgrade = 0, body, False
        while True:
            req = http.build_request(method, url, params=params, headers=headers, content=cur)
            try:
                up = await http.send(req, stream=True)
            except httpx.HTTPError:
                if attempt < c.retry_on_5xx:
                    attempt += 1
                    log.warning("req[%s] upstream[%s] transport error, retry %d/%d",
                                rid, upstream_id, attempt, c.retry_on_5xx)
                    await asyncio.sleep((c.retry_backoff_ms / 1000.0) * (2 ** (attempt - 1)))
                    continue
                raise
            if up.status_code in (429, 502, 503, 504) and attempt < c.retry_on_5xx:
                log.warning("req[%s] upstream[%s] %d, retry %d/%d",
                            rid, upstream_id, up.status_code, attempt + 1, c.retry_on_5xx)
                await up.aclose()
                attempt += 1
                await asyncio.sleep((c.retry_backoff_ms / 1000.0) * (2 ** (attempt - 1)))
                continue
            if (up.status_code == 400 and c.json_schema_fallback and not tried_downgrade
                    and is_json_schema_request(cur)):
                err = await up.aread()
                await up.aclose()
                if b"json_schema" in err.lower() or b"response_format" in err.lower():
                    # A request MUTATION (not response translation): record the decision
                    # so a later loose-JSON completion isn't mistaken for a model fault.
                    log.warning("req[%s] upstream[%s] rejected json_schema (400); "
                                "downgrading response_format to json_object and retrying",
                                rid, upstream_id)
                    cur = downgrade_to_json_object(cur)
                    tried_downgrade = True
                    continue
                return httpx.Response(up.status_code, headers=up.headers, content=err), attempt + 1
            return up, attempt + 1

    def _record_error(*, rid, principal, client_ip, model, upstream_id, status,
                      latency_ms, stream, path, reason, body=None, exc=None,
                      up_rid=None, attempts=1, upstream_key=None, refusal_category=None):
        """Log + persist ONE failed forward. Never calls budget.record — an error
        has no billable usage, so it must not touch the ledger or the 402 pre-check.
        The upstream body / exception repr is truncated + key-scrubbed before it
        reaches either sink; request/forward headers are NEVER passed here."""
        snippet = None
        if body is not None:
            text = (body.decode("utf-8", "replace")
                    if isinstance(body, (bytes, bytearray)) else str(body))
            snippet = redact(text, known_secrets=(upstream_key or "",))
        elif exc is not None:
            snippet = redact(repr(exc), known_secrets=(upstream_key or "",))
        # 4xx -> WARNING, 5xx/transport-class -> ERROR. A mid-stream abort starts life
        # as a 200 header, so key it on the reason, not the (misleading) status.
        transport_class = reason in ("transport_error", "stream_aborted", "handler_exception")
        lvl = logging.ERROR if (transport_class or status is None or status >= 500) else logging.WARNING
        log.log(lvl, "req[%s] FORWARD FAILED reason=%s team=%s model=%s upstream=%s "
                     "path=/%s status=%s latency=%sms attempts=%s upstream_req=%s: %s",
                rid, reason, principal, model, upstream_id, path, status,
                latency_ms, attempts, up_rid, snippet)
        if sink is not None:
            try:
                row = {
                    "event": "llm_error", "request_id": rid, "principal": principal,
                    "client_ip": client_ip, "model": model, "upstream": upstream_id,
                    "status": status, "stream": stream, "path": path, "reason": reason,
                    "attempts": attempts, "upstream_request_id": up_rid,
                    "error": snippet, "latency_ms": latency_ms,
                }
                # A server-side cyber-policy refusal (OpenAI code=cyber_policy): tag the
                # row so the harvest can count it as a REFUSAL per team, symmetric with
                # the Anthropic-side AGENT_REFUSED path, instead of a generic error.
                if refusal_category:
                    row["refusal_category"] = refusal_category
                sink.record(row)
            except Exception:
                pass

    def _meter(*, rid, principal, client_ip, model, provider_model, upstream_id, status,
               latency_ms, stream, path, usage, attempts=1):
        # Only a response that actually reported usage is billed + rowed here;
        # every failure is handled by _record_error. A non-dict `usage` (a sloppy
        # reseller sending `"usage": 0`) would otherwise crash the handler into an
        # anonymous 500 — treat it as "no usage" and note it, don't die.
        if usage is None:
            return
        if not isinstance(usage, dict):
            log.warning("req[%s] upstream[%s] returned non-dict usage %r; not metered",
                        rid, upstream_id, type(usage).__name__)
            return
        u = extract_usage(usage)
        cost = cost_from_pricing(cfg.pricing, model or "", u["input_tokens"], u["output_tokens"],
                                  u["cache_read_input_tokens"], u["cache_creation_input_tokens"])
        if cost is None and model and model not in unpriced_seen:
            unpriced_seen.add(model)
            log.warning("req[%s] model %r absent from pricing table; cost recorded as null "
                        "(budget cap cannot fire for it)", rid, model)
        # accumulate usage (drives /usage report + the 402 pre-check)
        budget.record(principal, input_tokens=u["input_tokens"],
                      output_tokens=u["output_tokens"], cost=cost)
        if sink is not None:
            sink.record({
                "event": "llm", "request_id": rid, "principal": principal,
                "client_ip": client_ip, "model": model, "upstream": upstream_id,
                "provider_model": provider_model,
                "status": status, "stream": stream, "path": path, **u,
                # input_tokens is now plain uncached input, so add the cache buckets
                # back for a true grand total (else cache-heavy models undercount).
                "total_tokens": (u["input_tokens"] + u["output_tokens"]
                                 + u["cache_read_input_tokens"]
                                 + u["cache_creation_input_tokens"]),
                "cost_usd": cost,
                "latency_ms": latency_ms, "attempts": attempts,
            })

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/usage")
    async def usage_report():
        """Per-person spend (calls / tokens / cost / budget / remaining) — read
        this instead of grepping the JSONL log."""
        rows = budget.report()
        return {
            "principals": rows,
            "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        }

    @app.api_route("/{path:path}", methods=_PROXY_METHODS)
    async def proxy(path: str, request: Request):
        rid = _new_request_id()
        started = time.monotonic()
        latency_ms = lambda: round((time.monotonic() - started) * 1000.0, 1)  # noqa: E731
        client_ip = request.client.host if request.client else None
        principal = model = upstream_id = None
        upstream: Upstream | None = None
        try:
            body = await request.body()
            # Always: a `null` JSON-Schema keyword in a tool's parameters
            # (opencode's `required: null` for a no-arg MCP tool) is invalid for
            # every upstream and fatal on a strictly validating one.
            body, fixed_tools = normalize_tool_schemas(body)
            if fixed_tools:
                log.info("rid=%s dropped null schema keywords from tools %s",
                         rid, ",".join(fixed_tools))
            # #1/#3/#11: rewrite known-bad fields before forwarding.
            if (cfg.compat.drop_empty_content or cfg.compat.drop_params
                    or cfg.compat.clamp_max_tokens):
                body = sanitize_request_body(
                    body, drop_empty_content=cfg.compat.drop_empty_content,
                    drop_params=cfg.compat.drop_params,
                    clamp_max_tokens=cfg.compat.clamp_max_tokens,
                )
            model = _parse_model(body)
            # Route on the model the client asked for, then forward with THAT
            # upstream's key. A body with no model (or an unrouted one) hits routes.default.
            upstream_id, upstream = cfg.upstream_for(model)
            url = f"{upstream.base_url}/{path}"

            # Per-person attribution, resolved BEFORE the size/budget checks so
            # every pre-forward reject can still name the team. When principals are
            # configured, an unknown token is rejected — and NOT billed (no
            # token-probing oracle), but it IS recorded (client_ip + a token
            # fingerprint, never the token) so a 100%-failing agent isn't invisible.
            if cfg.token_to_principal:
                tok = _client_token(request)
                principal = cfg.principal_for(tok)
                if principal is None:
                    reason = "missing" if not tok else "unknown"
                    log.warning("req[%s] AUTH REJECT reason=%s client_ip=%s path=/%s token=%s",
                                rid, reason, client_ip, path, token_fingerprint(tok))
                    if sink is not None:
                        sink.record({"event": "auth_reject", "request_id": rid,
                                     "client_ip": client_ip, "path": path, "model": model,
                                     "upstream": upstream_id, "reason": reason,
                                     "token_fp": token_fingerprint(tok)})
                    return JSONResponse(status_code=401, content={
                        "error": {"type": "authentication_error",
                                  "message": "unknown or missing token", "request_id": rid}})

            # #5/#6: refuse oversize bodies up front. Record the SIZE + limit only —
            # the body is the agent's full prompt/context (and possibly a flag).
            if cfg.compat.max_request_bytes and len(body) > cfg.compat.max_request_bytes:
                _record_error(rid=rid, principal=principal, client_ip=client_ip, model=model,
                              upstream_id=upstream_id, status=413, latency_ms=latency_ms(),
                              stream=False, path=path, reason="oversize_request",
                              body=(f"request body {len(body)} bytes exceeds limit "
                                    f"{cfg.compat.max_request_bytes}"))
                return JSONResponse(status_code=413, content={"error": {
                    "type": "request_too_large", "request_id": rid,
                    "message": f"request body {len(body)} bytes exceeds limit "
                               f"{cfg.compat.max_request_bytes}"}})

            # Budget pre-check: reject once a principal has reached its USD cap.
            if budget.exceeded(principal):
                log.warning("req[%s] BUDGET BLOCK team=%s spent=%.6g limit=%.6g path=/%s",
                            rid, principal, budget.spent(principal), budget.limit(principal), path)
                if sink is not None:
                    sink.record({"event": "budget_block", "request_id": rid,
                                 "principal": principal, "client_ip": client_ip, "path": path,
                                 "model": model, "spent_usd": budget.spent(principal),
                                 "limit_usd": budget.limit(principal)})
                return JSONResponse(status_code=402, content={"error": {
                    "type": "budget_exceeded", "request_id": rid,
                    "message": (f"budget exceeded for {principal}: "
                                f"spent {budget.spent(principal):.6g} >= "
                                f"limit {budget.limit(principal):.6g} USD")}})

            try:
                up, attempts = await _forward(rid, upstream_id, request.method, url,
                                              request.query_params,
                                              _fwd_headers(request, upstream), body)
            except httpx.HTTPError as e:
                # DNS / connect refused / TLS / timeout, after retries exhausted.
                _record_error(rid=rid, principal=principal, client_ip=client_ip, model=model,
                              upstream_id=upstream_id, status=502, latency_ms=latency_ms(),
                              stream=False, path=path, reason="transport_error", exc=e,
                              attempts=cfg.compat.retry_on_5xx + 1, upstream_key=upstream.api_key)
                return Response(content=f"upstream error: {e}".encode()[:500],
                                status_code=502)

            ct = up.headers.get("content-type", "")
            headers = _resp_headers(up)
            # The upstream's OWN correlation id (Anthropic `request-id`, OpenAI
            # `x-request-id`): passed through today, captured here so a failure can
            # be taken back to the provider.
            up_rid = up.headers.get("request-id") or up.headers.get("x-request-id")

            if "text/event-stream" in ct:
                # A non-2xx delivered with an SSE content-type is an ERROR, not a
                # stream (also the shape of a CDN error page on a stream URL, and of
                # the eager response reconstructed for a non-matching json_schema
                # 400 — aiter_raw() would raise StreamConsumed on that one). Buffer
                # it (errors are small), record, return — do NOT take the streaming
                # path.
                if up.status_code >= 400:
                    raw = await up.aread()
                    await up.aclose()
                    _record_error(rid=rid, principal=principal, client_ip=client_ip, model=model,
                                  upstream_id=upstream_id, status=up.status_code,
                                  latency_ms=latency_ms(), stream=True, path=path,
                                  reason="upstream_error", body=raw, up_rid=up_rid,
                                  attempts=attempts, upstream_key=upstream.api_key)
                    return Response(content=raw, status_code=up.status_code, headers=headers,
                                    media_type=ct or None)

                # 2xx stream: tee chunks straight through (NEVER buffer a success —
                # that would add latency to every agent call). Metering + mid-stream
                # error detection happen at end-of-stream over the teed bytes.
                _key = upstream.api_key
                # A response that is already fully in hand (an eager/reconstructed
                # httpx.Response) can't be aiter_raw()'d — StreamConsumed. Only that
                # case reads whole + emits once; a live network stream
                # (is_stream_consumed False) still streams incrementally.
                _eager = up.is_stream_consumed

                async def gen():
                    buf = bytearray()
                    recorded = False
                    try:
                        if _eager:
                            chunk = await up.aread()
                            buf += chunk
                            yield chunk
                        else:
                            async for chunk in up.aiter_raw():
                                buf += chunk
                                yield chunk
                    except Exception as e:  # noqa: BLE001 — abort AFTER 200 headers went out
                        # Upstream reset / read timeout / RemoteProtocolError mid-stream.
                        # (CancelledError is BaseException, not caught → phase-end teardown
                        # stays silent, as it should.) Record, then end the stream.
                        _record_error(rid=rid, principal=principal, client_ip=client_ip,
                                      model=model, upstream_id=upstream_id, status=up.status_code,
                                      latency_ms=latency_ms(), stream=True, path=path,
                                      reason="stream_aborted", exc=e, up_rid=up_rid,
                                      attempts=attempts, upstream_key=_key)
                        recorded = True
                    finally:
                        await up.aclose()
                        if not recorded:
                            err_evt = _sse_error_event(bytes(buf))
                            if err_evt is not None:
                                # A 200 that carried an error event mid-stream: record the
                                # ERROR (its type/message only), and do NOT also write a
                                # misleading success row for the partial usage.
                                _record_error(rid=rid, principal=principal, client_ip=client_ip,
                                              model=model, upstream_id=upstream_id,
                                              status=up.status_code, latency_ms=latency_ms(),
                                              stream=True, path=path, reason="sse_error_event",
                                              body=json.dumps(err_evt), up_rid=up_rid,
                                              attempts=attempts, upstream_key=_key,
                                              refusal_category=err_evt.get("refusal_category"))
                            else:
                                usage, provider_model = _merge_stream_usage(bytes(buf))
                                _meter(rid=rid, principal=principal, client_ip=client_ip,
                                       model=model, provider_model=provider_model,
                                       upstream_id=upstream_id,
                                       status=up.status_code, latency_ms=latency_ms(),
                                       stream=True, path=path, attempts=attempts,
                                       usage=usage)
                return StreamingResponse(gen(), status_code=up.status_code,
                                         headers=headers, media_type=ct or None)

            # Non-streaming: buffer, then either record the error or repair+meter.
            raw = await up.aread()
            await up.aclose()
            if up.status_code >= 400:
                # THE HEADLINE CASE: Claude Code (Anthropic Messages) at an OpenAI-only
                # upstream 404s; also 401 (bad upstream key), 400 (bad body), 429, 5xx.
                # Return the body verbatim (no markdown repair on an error) + record it.
                _record_error(rid=rid, principal=principal, client_ip=client_ip, model=model,
                              upstream_id=upstream_id, status=up.status_code,
                              latency_ms=latency_ms(), stream=False, path=path,
                              reason="upstream_error", body=raw, up_rid=up_rid,
                              attempts=attempts, upstream_key=upstream.api_key)
                return Response(content=raw, status_code=up.status_code, headers=headers,
                                media_type=ct or None)

            fixed = unwrap_markdown_json(raw.decode("utf-8", "replace"))
            usage = None
            provider_model = None
            try:
                parsed = json.loads(fixed)
                if isinstance(parsed, dict):
                    usage = parsed.get("usage")
                    if usage is None:
                        usage = _usage_payload(parsed)
                    provider_model = _provider_model(parsed)
                    model = model or parsed.get("model")
            except Exception:
                pass
            _meter(rid=rid, principal=principal, client_ip=client_ip, model=model,
                   provider_model=provider_model,
                   upstream_id=upstream_id, status=up.status_code, latency_ms=latency_ms(),
                   stream=False, path=path, usage=usage, attempts=attempts)
            return Response(content=fixed.encode("utf-8"), status_code=up.status_code,
                            headers=headers, media_type=ct or None)

        except Exception as e:  # noqa: BLE001 — backstop so no failure is an anonymous 500
            # Reaches here on ClientDisconnect (agent hung up mid-upload) or any latent
            # bug. Log the type+message + rid (NOT a variable-rendering traceback, which
            # could render the forward headers holding the injected key).
            _record_error(rid=rid, principal=principal, client_ip=client_ip, model=model,
                          upstream_id=upstream_id, status=500, latency_ms=latency_ms(),
                          stream=False, path=path, reason="handler_exception", exc=e,
                          upstream_key=(upstream.api_key if upstream else None))
            return JSONResponse(status_code=500, content={
                "error": {"type": "router_error", "message": "internal router error",
                          "request_id": rid}})

    return app
