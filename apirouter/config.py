"""Router configuration — YAML load + ``${ENV}`` expansion.

Self-contained loader (no imports from the arena). See config.example.yaml for
the schema. The router is a verbatim reverse passthrough + egress firewall — no
format translation — so the config is small: where to forward, how to price,
what to allow out, where to log.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .firewall import Firewall


@dataclass(frozen=True)
class Listen:
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8080
    proxy_host: str = "0.0.0.0"
    proxy_port: int = 3128


@dataclass(frozen=True)
class Relay:
    """Optional narrow reverse-proxy to the host gameserver, so an isolated
    agent (internal net, no host route) can reach MCP/REST through the router.
    Disabled unless `base_url` is set. Forwards verbatim to that ONE upstream —
    not a general host gateway."""
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    base_url: str = ""


@dataclass(frozen=True)
class Upstream:
    # Requests are forwarded verbatim to `base_url + <incoming path>` (+ query).
    base_url: str
    # REQUIRED. The router overwrites the client's auth with this key
    # (Authorization: Bearer + x-api-key) before forwarding, so the real key lives
    # only in the router and never inside a container.
    #
    # There is no "forward the client's own key" mode. What a client presents is a
    # principal token the router itself minted (per-team, for billing) — not a
    # provider credential — so forwarding it verbatim would just earn a 401. An
    # upstream without a key has nothing valid to forward WITH, which is a
    # misconfiguration, not a mode; the loader rejects it.
    api_key: str


@dataclass(frozen=True)
class Routes:
    """Which upstream serves a given request — resolved per request from the
    model name the CLIENT sent (there is no translation, so it is whatever the
    client put in the body). `by_model` globs are tried in declaration order and
    the first match wins; a request whose model matches nothing — or that carries
    no model at all (e.g. a GET) — goes to `default`."""
    default: str
    by_model: tuple[tuple[str, str], ...] = ()   # ordered (glob, upstream id)

    def pick(self, model: str | None) -> str:
        if model:
            for pattern, upstream_id in self.by_model:
                if fnmatch.fnmatchcase(model, pattern):
                    return upstream_id
        return self.default


@dataclass(frozen=True)
class Metering:
    jsonl_path: str | None = None
    stdout: bool = True


@dataclass(frozen=True)
class Compat:
    """Defensive workarounds for flaky 'OpenAI-compatible' upstreams. All opt-in
    — every field off/empty ⇒ strict verbatim passthrough."""
    drop_empty_content: bool = False          # #1
    drop_params: tuple[str, ...] = ()          # #3 (e.g. ["reasoning_effort"])
    json_schema_fallback: bool = False         # #4
    retry_on_5xx: int = 0                       # #10 — retries on 429/502/503/504 + transport errors
    retry_backoff_ms: int = 500                 # base backoff (exponential)
    max_request_bytes: int = 0                  # #5/#6 — 0 = off; else reject larger bodies with 413
    clamp_max_tokens: dict[str, int] = field(default_factory=dict)  # #11 — per-model max_tokens ceiling


@dataclass(frozen=True)
class RouterConfig:
    listen: Listen
    # id -> upstream. A single-upstream `upstream:` block desugars to {"default": ...}.
    upstreams: dict[str, Upstream]
    # Which upstream a request goes to, by the client's model name.
    routes: Routes
    firewall: Firewall
    metering: Metering
    relay: Relay = field(default_factory=Relay)
    compat: Compat = field(default_factory=Compat)
    # USD per 1M tokens, keyed by the model name the client sends:
    # {model: {input, output, cache_read, cache_write}}. `cache_read` and
    # `cache_write` are optional — a rate that isn't set defaults to 0.0, so an
    # {input, output}-only entry keeps costing exactly what it did before.
    # `cache_read` prices `cache_read_input_tokens` (a cache HIT — usually much
    # cheaper than a fresh input token); `cache_write` prices
    # `cache_creation_input_tokens` (a cache MISS that populates the cache —
    # usually pricier than a fresh input token). The only cost source (no litellm).
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    # Optional per-person token -> id, for cost attribution. When non-empty the
    # gateway tags each metering row with `principal` and rejects unknown tokens
    # with 401 (so every metered request is attributable).
    token_to_principal: dict[str, str] = field(default_factory=dict)
    # Optional per-principal USD spend cap (principal id -> cap). Absent ⇒
    # unlimited. The gateway rejects further requests with 402 once reached.
    budgets: dict[str, float] = field(default_factory=dict)

    @property
    def upstream(self) -> Upstream:
        """The default upstream — what a single-upstream deployment calls *the*
        upstream. Kept so callers that predate routing keep reading naturally."""
        return self.upstreams[self.routes.default]

    def upstream_for(self, model: str | None) -> tuple[str, Upstream]:
        upstream_id = self.routes.pick(model)
        return upstream_id, self.upstreams[upstream_id]

    def principal_for(self, token: str | None) -> str | None:
        if not token:
            return None
        return self.token_to_principal.get(token)


_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_MAX_EXPAND_DEPTH = 8


def _expand(node):
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v) for v in node]
    if isinstance(node, str):
        return _expand_str(node)
    return node


def _expand_str(s: str) -> str:
    visited: set[str] = set()
    for _ in range(_MAX_EXPAND_DEPTH):
        matches = _VAR_RE.findall(s)
        if not matches:
            return s
        for var in matches:
            if var in visited:
                raise ValueError(f"cyclic env var reference involving ${{{var}}} in {s!r}")
            if var not in os.environ:
                raise ValueError(f"env var {var} referenced in router config but not set")
        visited.update(matches)
        s = _VAR_RE.sub(lambda m: os.environ[m.group(1)], s)
    raise ValueError(f"env var expansion exceeded depth {_MAX_EXPAND_DEPTH} on {s!r}")


def _split_hostport(spec: str, default_port: int) -> tuple[str, int]:
    spec = str(spec)
    if ":" in spec:
        host, _, port = spec.rpartition(":")
        return host or "0.0.0.0", int(port)
    return spec, default_port


def _load_routing(raw: dict) -> tuple[dict[str, Upstream], Routes]:
    """Build the upstream table + routing rules.

    Two spellings, never both. `upstream:` (singular) is the shorthand for one
    upstream and desugars to `upstreams: {default: ...}`; `upstreams:` + `routes:`
    is the general form. Declaring the shorthand alongside either half of the
    general form is rejected rather than silently resolved in someone's favour —
    a config where the answer to "which upstream did this go to?" depends on a
    precedence rule is a config that will mislead whoever edits it next.
    """
    single = raw.get("upstream") or {}
    multi = raw.get("upstreams") or {}
    routes_raw = raw.get("routes") or {}

    if single and (multi or routes_raw):
        raise ValueError(
            "config.upstream (single-upstream shorthand) cannot be combined with "
            "config.upstreams / config.routes. Drop `upstream:`, declare every "
            "upstream under `upstreams:`, and name the fallback in `routes.default`."
        )

    if not multi:
        if not single.get("base_url"):
            raise ValueError("config.upstream.base_url is required")
        if not single.get("api_key"):
            raise ValueError(
                "config.upstream.api_key is required — the router forwards with THIS "
                "key. A client's own credential is a principal token the router minted "
                "for billing, not a provider key, so there is nothing else to forward.")
        one = Upstream(base_url=str(single["base_url"]).rstrip("/"),
                       api_key=single["api_key"])
        return {"default": one}, Routes(default="default")

    upstreams: dict[str, Upstream] = {}
    for upstream_id, spec in multi.items():
        spec = spec or {}
        if not spec.get("base_url"):
            raise ValueError(f"config.upstreams.{upstream_id}.base_url is required")
        if not spec.get("api_key"):
            raise ValueError(
                f"config.upstreams.{upstream_id}.api_key is required — every upstream "
                "must carry the key the router forwards with.")
        upstreams[str(upstream_id)] = Upstream(
            base_url=str(spec["base_url"]).rstrip("/"), api_key=spec["api_key"])

    default = str(routes_raw.get("default", "") or "")
    if not default:
        if len(upstreams) > 1:
            raise ValueError(
                "config.routes.default is required when more than one upstream is "
                f"declared (upstreams: {sorted(upstreams)}) — it is where a request "
                "whose model matches no by_model rule goes.")
        default = next(iter(upstreams))

    # dicts preserve YAML declaration order, so first-match-wins is the order read.
    by_model = tuple(
        (str(pattern), str(upstream_id))
        for pattern, upstream_id in (routes_raw.get("by_model") or {}).items()
    )
    for upstream_id in (default, *(u for _, u in by_model)):
        if upstream_id not in upstreams:
            raise ValueError(
                f"config.routes points at unknown upstream {upstream_id!r}; "
                f"declared upstreams: {sorted(upstreams)}")
    return upstreams, Routes(default=default, by_model=by_model)


def from_dict(raw: dict, *, base_dir: str | Path | None = None) -> RouterConfig:
    raw = _expand(raw)

    listen_raw = raw.get("listen", {}) or {}
    g_host, g_port = _split_hostport(listen_raw.get("gateway", "0.0.0.0:8080"), 8080)
    p_host, p_port = _split_hostport(listen_raw.get("proxy", "0.0.0.0:3128"), 3128)
    listen = Listen(gateway_host=g_host, gateway_port=g_port, proxy_host=p_host, proxy_port=p_port)

    upstreams, routes = _load_routing(raw)

    rl = raw.get("relay", {}) or {}
    relay_base = str(rl.get("base_url", "") or "")
    r_host, r_port = _split_hostport(rl.get("listen", "0.0.0.0:8000"), 8000)
    relay = Relay(enabled=bool(relay_base), host=r_host, port=r_port, base_url=relay_base)

    fw = raw.get("firewall", {}) or {}
    allow = fw.get("allow", {}) or {}
    deny = fw.get("deny", {}) or {}
    firewall = Firewall(
        default=str(fw.get("default", "deny")),
        allow_domains=list(allow.get("domains", [])),
        allow_cidrs=list(allow.get("cidrs", [])),
        deny_domains=list(deny.get("domains", [])),
        deny_cidrs=list(deny.get("cidrs", [])),
    )

    met = raw.get("metering", {}) or {}
    jsonl_path = met.get("jsonl_path")
    # A relative jsonl_path is resolved next to the config file, so config + log
    # land in the same dir (e.g. /data) regardless of the process CWD.
    if jsonl_path and base_dir is not None and not Path(jsonl_path).is_absolute():
        jsonl_path = str(Path(base_dir) / jsonl_path)
    metering = Metering(jsonl_path=jsonl_path, stdout=bool(met.get("stdout", True)))

    cm = raw.get("compat", {}) or {}
    compat = Compat(
        drop_empty_content=bool(cm.get("drop_empty_content", False)),
        drop_params=tuple(cm.get("drop_params", []) or []),
        json_schema_fallback=bool(cm.get("json_schema_fallback", False)),
        retry_on_5xx=int(cm.get("retry_on_5xx", 0)),
        retry_backoff_ms=int(cm.get("retry_backoff_ms", 500)),
        max_request_bytes=int(cm.get("max_request_bytes", 0)),
        clamp_max_tokens={
            str(k): int(v) for k, v in (cm.get("clamp_max_tokens", {}) or {}).items()
        },
    )

    pricing = {k: dict(v) for k, v in (raw.get("pricing", {}) or {}).items()}

    token_to_principal: dict[str, str] = {}
    budgets: dict[str, float] = {}
    for p in raw.get("principals", []) or []:
        tok, pid = p.get("token"), p.get("id")
        if tok and pid:
            token_to_principal[tok] = pid
            if p.get("budget_usd") is not None:
                budgets[pid] = float(p["budget_usd"])

    return RouterConfig(
        listen=listen, upstreams=upstreams, routes=routes, firewall=firewall,
        metering=metering, relay=relay, pricing=pricing,
        token_to_principal=token_to_principal, budgets=budgets, compat=compat,
    )


def load(path: str | Path) -> RouterConfig:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return from_dict(raw, base_dir=Path(path).parent)
