# API Router — design + usage (v1)

A self-contained **egress gateway + LLM metering proxy** for the arena's
container networks. Lives in [apirouter/](../apirouter/) with its own
Dockerfile + `requirements.txt` and imports nothing from `gamemgr` /
`orchestrator` / `models`. Runs standalone (`python -m apirouter` or
`docker run cyberarena/api-router`).

## Quick start

```bash
# 1. build
docker build -t cyberarena/api-router apirouter/

# 2. config + logs live in ONE dir; only the real upstream key goes via -e
mkdir -p apirouter-data
cp apirouter/config.example.yaml apirouter-data/config.yaml   # edit tokens / pricing / budgets
docker run -d --name api-router -p 8080:8080 -p 3128:3128 \
  -e UPSTREAM_API_KEY=sk-real \
  -v "$PWD/apirouter-data:/data" \
  cyberarena/api-router

# 3. point a client at base_url=http://localhost:8080, then read per-person spend
curl -s localhost:8080/usage | jq
```

**No key handy? Run the offline self-test** (built-in mock upstream, no network,
no key) — it boots the router and verifies passthrough, markdown repair,
`/usage`, budget 402, 401, and the firewall:

```bash
pip install -r apirouter/requirements.txt
python apirouter/selftest.py            # → 7/7 checks passed, exit 0
```

**Local (no Docker):** `cp apirouter/config.example.yaml apirouter-data/config.yaml`,
then `APIROUTER_CONFIG=apirouter-data/config.yaml python -m apirouter`. The
relative `usage.jsonl` path resolves next to the config, so config + log stay
together in `apirouter-data/`.

## Why

The arena's container traffic should pass through one gateway so we can:

1. **Meter** every LLM call — input/output tokens, cost, latency, model.
2. **Firewall** all other container egress — allow/deny by domain/IP.
3. Optionally keep the **real upstream key out of the (adversarial) containers**.

`config/game.yaml` already points each agent's `base_url` at
`${API_ROUTER_BASE}` and design v1.7 reserved the `API_LOG` artifact kind.

## What it is NOT

It does **not** translate API formats. It is a **verbatim reverse passthrough**:
whatever the client sends (OpenAI `/chat/completions`, Anthropic `/v1/messages`,
anything) is forwarded byte-for-byte to the upstream, and the upstream's
response is returned unchanged. No litellm, no SDKs, no model mapping. The
client must speak whatever format the upstream accepts.

## Two listeners, one process

```
                       ┌──────────────── apirouter ────────────────┐
 agent / judge         │  :8080  reverse-passthrough gateway        │
 base_url=           ──►│   ANY /<path>  ─ forward verbatim ─►       │──► upstream
  http://router:8080   │     base_url + <path> (+ query, headers,   │   (real proxy,
                       │     body). Optional: inject real api_key.  │◄── e.g. ppapi)
                       │   ◄─ return response verbatim              │
                       │     • non-stream JSON: repair ```json fence│
                       │     • if body has `usage`: meter → JSONL   │
                       │     • stream (SSE): pass through + tee+meter│
 any container         │  :3128  forward proxy (egress firewall)    │
 HTTP_PROXY=         ──►│   GET http://h/..  → firewall.decide(h)    │──► allowed
  http://router:3128   │   CONNECT h:443    → firewall.decide(h)     │    internet
                       │   (egress allow/deny → JSONL)              │
                       └────────────────────────────────────────────┘
```

The gateway is where LLM clients point their `base_url` (plain HTTP inside the
docker net; the router makes the TLS connection to the real upstream — so the
bodies are readable without any MITM). The forward proxy is the `HTTP_PROXY`
target for everything else, filtered by the firewall (CONNECT host only, no TLS
interception).

## Gateway internals

- **Forward** (`gateway.py`): a catch-all route reads the request, forwards it
  to `upstream.base_url + <path>` with the same method/query/body and headers
  (minus hop-by-hop), and returns the upstream response. `Accept-Encoding:
  identity` is forced so the body is plain text.
- **Markdown repair** (`sanitize.py`): the proxy sometimes wraps its JSON body
  in a ```` ```json … ``` ```` fence; non-streaming JSON bodies are unwrapped so
  the client gets clean JSON. Conservative — only unwraps content that looks
  like JSON; never corrupts a genuinely-unfenced body.
- **Metering** (`metering.py`): if the (repaired) response carries a `usage`
  object, `extract_usage` reads it **format-agnostically** — OpenAI
  (`prompt_tokens`/`completion_tokens`) and Anthropic
  (`input_tokens`/`output_tokens`) alike — the model comes from the request
  body, cost from the `pricing` table, and a JSONL row is appended. Streaming
  responses pass through verbatim and are teed; usage is pulled from the SSE
  events at the end. Non-API traffic passes through unmetered.
- **Optional key injection**: if `upstream.api_key` is set, the proxy overwrites
  the client's `Authorization`/`x-api-key` with it before forwarding — so the
  real key lives only in the router. Empty → the client's own auth is forwarded
  verbatim.

## Metering output — JSONL

One JSON object per LLM request, appended to `metering.jsonl_path` and/or stdout:

```json
{"ts": 1.7e9, "event": "llm", "principal": "alice", "client_ip": "10.1.0.2",
 "model": "deepseek-v4-flash", "status": 200, "stream": false,
 "path": "v1/chat/completions", "input_tokens": 12, "output_tokens": 3,
 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
 "total_tokens": 15, "cost_usd": 6.54e-06, "latency_ms": 412.0}
```

Egress decisions share the sink with `"event": "egress"` (host, ip, allowed,
reason, method, port). No orchestrator coupling, no Prometheus — by design.

### Per-person attribution

Each metering row carries who made the call so you can split spend by person:

- **`principal`** — set when `principals` is configured (below). Each person
  uses their own token as the API key; the router maps token → id, tags the row,
  injects the real upstream key (so a person only ever holds their own token),
  and **rejects an unknown token with 401** (not metered — no token oracle).
- **`client_ip`** — always recorded; the fallback when no `principals` are set
  (works if each person = one container).

For a live per-person bill you don't need to parse the log at all — **`GET
/usage`** returns the aggregate straight from the in-memory ledger:

```json
{"principals": [
   {"principal": "alice", "calls": 12, "input_tokens": 4100, "output_tokens": 900,
    "cost_usd": 0.0123, "budget_usd": 5.0, "remaining_usd": 4.9877}],
 "total_cost_usd": 0.0123}
```

The JSONL is the durable audit trail; `/usage` is the at-a-glance view. (Note:
`/usage` reflects the current process — it resets on restart, like budgets.)

### Per-person budgets (spend caps)

Give a principal a `budget_usd` and the router **rejects further requests with
402** once the cap is reached. Because cost is only known after a call returns,
it's a soft cap: the pre-check compares accumulated spend against the cap, so the
request that crosses the line still completes; the next one is blocked. A blocked
request is recorded as `{"event": "budget_block", "principal", "spent_usd",
"limit_usd"}`. Spend is in-memory (per process), so it resets on restart, and a
principal with no `budget_usd` is unlimited. Budgets need a non-zero `pricing`
entry for the model — otherwise every call costs 0 and nothing accrues.

## Compat — third-party-proxy workarounds

"OpenAI-compatible" proxies are only strict on common OpenAI models; once they
bridge to Anthropic/DeepSeek/etc backends, protocol features go missing. Rather
than patch every client, the gateway applies opt-in fixes centrally (config
`compat`, all default-off → strict verbatim when unset). **Enabling any of these
means the proxy is no longer byte-exact.**

| Quirk | `compat` knob | What the gateway does |
|---|---|---|
| #1 assistant `content:""` + `tool_calls` → upstream 400 | `drop_empty_content` | drops the empty `content` before forwarding |
| #3 unsupported param (e.g. `reasoning_effort`) → 400 | `drop_params: [...]` | strips the named top-level params |
| #4 strict `response_format: json_schema` rejected | `json_schema_fallback` | on a matching 400, rewrites to `json_object` and retries once |
| #10 transient `503`/`429`/transport error | `retry_on_5xx` (+`retry_backoff_ms`) | retries with exponential backoff |
| #5/#6 body over the proxy's ~6 MiB / 1M-token cap | `max_request_bytes` | rejects oversize bodies up front with `413` (no blind truncation — that stays client-side) |

Explicitly **not** handled (can't fix at the gateway): content-moderation
rejects (#7, proxy policy), unknown model names (#8 — we forward whatever model
the client sends, by design), key validity/region (#9). And #2 (`get_model_info`
raising) simply doesn't apply — the router never consults litellm's model table.

## Configuration

See [apirouter/config.example.yaml](../apirouter/config.example.yaml). `${ENV}`
placeholders expand from the process environment. Knobs:

- `upstream.base_url` — the upstream **host root**; requests go to
  `base_url + <incoming path>`. Set the client's `base_url` so its path lands
  right (e.g. client `http://api-router:8080/v1` → path `/v1/chat/completions`
  → `https://app-us.ppapi.ai/v1/chat/completions`).
- `upstream.api_key` — optional injected key (else client's auth is forwarded).
- `pricing` — USD per 1M tokens, keyed by the **model the client sends**. Only
  cost source; absent model → `cost_usd` is null (request still works). litellm
  is not used, so models it doesn't know (e.g. `deepseek-v4-flash`) are fine.
- `principals` — optional list of `{id, token, budget_usd?}` for per-person cost
  attribution + spend caps (see above). Omit to attribute by `client_ip` only.
- `firewall.{default,allow,deny}` — domain globs + CIDRs for the forward proxy.
- `compat.*` — opt-in third-party-proxy workarounds (see §Compat). Off ⇒ verbatim.

## Running standalone

Everything lives in one mounted dir, **`/data`**: drop `config.yaml` there and
the router writes `usage.jsonl` next to it. Only the real upstream key comes via
`-e`.

```bash
docker build -t cyberarena/api-router apirouter/        # or: scripts/build_images.sh
mkdir -p apirouter-data
cp apirouter/config.example.yaml apirouter-data/config.yaml   # then edit tokens/pricing/budgets
docker run -d --name api-router -p 8080:8080 -p 3128:3128 \
  -e UPSTREAM_API_KEY=sk-real \
  -v "$PWD/apirouter-data:/data" \
  cyberarena/api-router
```
Config + log are both in `./apirouter-data/`. Point any client (a litellm/OpenAI
agent, the deepseek judge, Claude Code) at `http://api-router:8080` as its
base_url — the router is format-neutral, so they all work as long as the upstream
accepts their format. Per-person spend: **`curl localhost:8080/usage`**.

## Wiring into the arena (opt-in — enforcement left to the operator)

The router's function is independent of how containers are forced through it:

- **Soft** — set each team's `agent.base_url → http://api-router:8080`; set
  `HTTP_PROXY`/`HTTPS_PROXY` on containers via `Node.env` →
  `http://api-router:3128`. Add an `api-router` node on each `team{N}_net`.
- **Hard** — additionally put team containers on docker `internal` networks
  with the router as the only egress-capable node.

## Tests

`python apirouter/selftest.py` is the one-command end-to-end check: it starts a
real `python -m apirouter` against a built-in mock upstream and asserts the whole
feature set (✓/✗ checklist, exits non-zero on failure) — no key, no network.

Unit tests: `tests/test_apirouter_*.py`, all litellm-free:
- `sanitize` — fence stripping.
- `firewall` — allow/deny by domain glob + CIDR + default; deny-wins.
- `config` — load + `${ENV}` expansion + schema.
- `metering` — `extract_usage` (OpenAI + Anthropic shapes), cost calc, sink.
- `gateway` — verbatim forward, markdown repair, key injection, metering,
  streaming, non-API passthrough (upstream is a `httpx.MockTransport`).
- `compat` — request sanitize (drop empty content / params), json_schema
  downgrade, 503 retry, oversize 413.
- `proxy` — forward proxy allow/deny + CONNECT tunnel (local sockets).

## Deferred

TLS-MITM inspection for the forward proxy; pushing usage onto the live panel;
capturing request/response bodies into the run's `API_LOG` artifact; persisting
per-principal spend across restarts (budgets are in-memory today).
