# Facility containerization + isolated-agent reachability — Plan v2

> **Exec boundary superseded.** Part A relay + B-0 ports + B-1 flag-service +
> B-2 health-poller all shipped (host-relay model). The **remote-exec boundary**
> below (host plant + flag push, the structured `/facility/health-check` verb,
> the `RemoteHealthPollerHandle` relay) is **replaced** by
> [facility_direct_exec_plan_v3.md](facility_direct_exec_plan_v3.md): facilities
> SSH into prod directly, GameMgr out of the exec hot path. The networks, HTTP
> ports (`StatusPort`/`EventSink`), facility token, and two-level audit carry
> forward; read v3 for the live exec direction.

**Status:** planned, not yet shipped (authored 2026-06-17; hardened after review).
**Builds on:** [facility_abstraction_plan_v1.md](facility_abstraction_plan_v1.md)
(Phase 1 shipped 2026-06-09: `FlagServer`/`HealthPoller` talk to `GameMgr`
through `StatusPort`/`EventSink`/`ExecPort` in a `FacilityContext`; Phases 2/3
were designed there, not built). This plan is the realization of that Phase 2/3,
driven by a concrete need: **running the game with the agent network isolated.**
Live design once shipped: [docs/UML/facility.md](../UML/facility.md).

## Problem (verified this cycle)

In "isolated agent" mode (PR #17: agent dev nets are `internal: true`, the only
egress is via `cyberarena/api-router`), a smoke test showed:

| Check | Result |
|---|---|
| Direct internet egress (no proxy) | HTTP 000 — blocked ✅ |
| Via api-router proxy → ppapi (allow-listed) | 307 ✅ |
| Via proxy → github (not allowed) | 403 ✅ |
| api-router gateway (LLM) intra-net | 200 ✅ |
| **Gameserver MCP `host.docker.internal:8000`** | **no route / 000 — UNREACHABLE** ❌ |

The isolation primitive works, but it severs the **agent → gameserver MCP**
channel. The agent needs MCP for everything it does (`submit_flag`,
`get_game_state`, `get_attacks_received`, `restart_service`, `report_*`). Root
cause: `GameMgr` + the in-process MCP server run **on the host** (uvicorn
`0.0.0.0:8000`); agents reach it via `host.docker.internal`, which an `internal`
network has no route to (no default gateway).

Two goals:
- **A. Make MCP reachable by the isolated agent** (near-term, unblocks play).
- **B. Containerize the `FlagServer` and `HealthPoller` facilities** so flag /
  health logic can run *in the agent network* (off the host process), realizing
  facility-plan-v1 Phase 2/3.

## Hard constraints (from the current code)

- The **MCP server is in-process with `GameMgr`** — `build_mcp_app(game)` holds a
  live reference; every tool calls `game.*` directly
  ([gamemgr/mcp_server.py](../../gamemgr/mcp_server.py)). The agent↔MCP boundary
  is *already* HTTP, but MCP↔GameMgr is in-memory.
- `FlagServer` and `HealthPoller` are **already port-decoupled** via
  `FacilityContext` (`Local*` adapters today) — the swap surface for HTTP exists
  ([facility/ports.py](../../facility/ports.py)).
- **`restart_service` + container lifecycle need Docker** (rebuild the prod
  container via the orchestrator + a restart-job on `GameMgr`). This MUST stay
  with `GameMgr` on the host.
- **`GameMgr` owns the round loop** (phase / round / flag-rotation truth), the
  **single timeline**, and **Docker**. Facilities orbit it.
- **Two distinct auth principals.** The agent's `X-Team-Token` is a *player*
  credential. Facility↔host service-to-service calls (event-ingest, exec,
  rotate) are *infra* and must use a **separate facility token**, never the
  player token. An agent must never be able to call an infra endpoint.
- **Security invariant (load-bearing): never put `docker.sock` — or any path to
  the Docker daemon — in a container the agent can reach.** The agent has *root
  in its own prod box*; that is contained (non-privileged container, no sock).
  But agent-reachable `docker.sock` = host root = total compromise. Docker stays
  host-side; facilities reach it only through the constrained exec endpoint
  below — which must not itself become a back-door to that power.

---

## Part A — Isolated-agent MCP reachability (api-router relay)

`api-router` already straddles the `internal` team net **and** the NAT'd
`egress_net`, so it can reach the host. Reuse it as the in-network entry point to
the host gameserver.

**Design:** add a **narrow reverse-proxy listener** on api-router (e.g. `:8000`)
that forwards to the **host GameMgr HTTP app only**. Point the agent's
`CYBERARENA_PUBLIC_BASE_URL` (→ `mcp_endpoint` + `submit_url`, set in
[gamemgr/game_mgr.py](../../gamemgr/game_mgr.py)) at a **logical endpoint** —
`http://mcp.cyberarena.internal` — never the relay's concrete `host:port`.

- **Logical endpoint, not a hard-coded implementation.** `CYBERARENA_PUBLIC_BASE_URL`
  (agent config / `game.yaml`) should point at a **stable logical endpoint**
  (e.g. `http://mcp.cyberarena.internal`), not the concrete relay `host:port`, so
  the relay implementation can change without touching agent config. *How that
  name resolves to the relay is a deployment detail, decided at build time.*
- **The relay is NOT a general host gateway.** It must reverse-proxy to exactly
  one upstream — the GameMgr app at `host.docker.internal:8000` — and expose only
  its routes (MCP `/mcp/`, REST `/game/*`, panel reads). It must **not** be a
  generic `host.docker.internal:*` / arbitrary-host forward. (Easy to get wrong:
  "just forward whatever host:port" turns the relay into a host-egress hole.)
- **Why a reverse proxy, not the `NO_PROXY` drop / forward-proxy hack:** routing
  MCP through the `:3128` forward proxy depends on the MCP client honoring
  `HTTPS_PROXY` for a plaintext non-public host — fragile. A dedicated reverse
  listener is a plain L7 forward that doesn't care about client proxy behavior.
- **Auth unchanged:** `X-Team-Token` is end-to-end; the relay just passes
  headers (transparent). The token→team mapping still happens in the host MCP.
- **Result:** `GameMgr` + MCP stay on the host unchanged; the isolated agent
  reaches MCP. Smallest change that unblocks isolated full-game play.

**Tasks:** add the single-upstream reverse-proxy route to `apirouter/` (or ship a
tiny sidecar relay image); point `CYBERARENA_PUBLIC_BASE_URL` at the logical MCP
endpoint per-deployment; verify from an agent container that the endpoint reaches
MCP while direct `host.docker.internal` (and any other host port) stays
unreachable.

---

## MCP routing (why the host MCP stays — read before Part B)

> **Superseded by [facility_direct_exec_plan_v3.md](facility_direct_exec_plan_v3.md)
> § "MCP server containerization".** The argument below (host MCP stays;
> `restart_service` can't move) is reversed there: the MCP server moves into an
> on-net `mcp-gateway` container that **forwards** privileged ops (restart) to
> GameMgr, which still executes them — so Docker stays host-side while agents
> reach MCP directly without the relay. The tool→backend mapping below is
> otherwise accurate and carries into v3.

The MCP server's tools have **opposite** coupling, so we do **not** containerize
MCP wholesale. After Part A (and through Part B) the routing is:

- **Stay on the host MCP (reached via the Part A relay):** `get_game_state`
  (game clock), `restart_service` (Docker — host only), `report_ready` /
  `report_deploy_complete` (round-loop / dry-run state). These are intrinsically
  GameMgr-coupled; moving them buys nothing and `restart_service` *can't* move
  (it drives Docker).
- **Served by the flag service (after B-1):** `submit_flag` and (optionally)
  `get_attacks_received`. Two ways, pick by need:
  - **(i)** the host MCP's `submit_flag` tool calls the flag service over HTTP —
    minimal agent-config change, still one host round-trip. *Start here.*
  - **(ii)** the agent submits directly to the flag service — point the existing
    separate `submit_url` cfg field at the flag service; truly off-host hot path.
    *Move here only if the round-trip matters.*

This is why "containerize MCP" is the wrong framing: the agent↔MCP seam is
already HTTP; what we relocate is **facility logic** (flag/health), not the MCP
transport, and only the tools that don't need GameMgr/Docker.

---

## Part B — Containerize FlagServer + HealthPoller

Realize the three ports as **HTTP adapters** (facility-plan-v1 Phase 2/3 already
specified the event/exec semantics; this builds them):

| Port | In-process today | HTTP adapter (containerized) |
|---|---|---|
| `StatusPort.status()` | `game.status()` | GET host `…/game/status` |
| `EventSink.record(...)` | `game.timeline.record(...)` | POST a **new GameMgr event-ingest endpoint** (timeline stays single + central) |
| `ExecPort.exec(...)` | docker-exec on host | POST a **new GameMgr exec endpoint**, constrained (see below) |

### The remote exec boundary (highest-risk surface — make it narrow)

"No `docker.sock`" is necessary but not sufficient: a powerful exec endpoint is
itself an indirect high-privilege channel. The remote exec endpoint must be
*much* narrower than the in-process `ExecPort` (which today takes a raw
`bash -c` string). Requirements:

1. **Role allowlist:** only `role=prod` is execable. Never `dev` (the agent's own
   box) or an arbitrary/empty role. Identity is `(team_id, role)`; the host
   resolves it to a container — callers never name a container.
2. **Structured operations, not raw shell.** The endpoint exposes a fixed verb
   set — e.g. `plant_flag(team_id, flag)`, `retrieve_flag(team_id, handle)`,
   `run_health_check(team_id, service)` — **not** an arbitrary `cmd`. The
   challenge-specific command string is built **host-side** from the verb +
   validated args, so a compromised facility can't run anything it likes inside
   a team box. (The in-process `ExecPort` may stay raw for co-located use; the
   *remote* boundary does not.)
3. **Audit every call — two levels (never put secrets in `events.jsonl`):**
   - **Timeline audit (redacted)** → the central timeline → `runs/<ts>/events.jsonl`:
     `caller`, `team_id`, `role`, `op`, `exit_code`, `duration_ms`, `request_id`.
     **No flag, no raw command, no secret.** This is the panel/debug-visible
     layer — `events.jsonl` is consumed by the panel, the end-of-run snapshot,
     shared results, and debug UIs, so it must stay secret-free.
   - **Raw audit (full detail)** → a **private** artifact, e.g.
     `runs/<ts>/private/facility_exec_audit.jsonl`: the full request / built
     command / args, correlated to the timeline row by `request_id`. Infra/debug
     only — **not** embedded into the panel snapshot, **not** exposed to agents /
     evaluator / frontend (and `runs/<ts>/private/` should be gitignored +
     access-controlled).
   The `request_id` joins the two, so a debugger can pull the full raw record for
   a given redacted timeline row.
4. **Facility-token auth** (not the player token; see auth boundary below).

### EventSink semantics (so a flaky container net can't corrupt the timeline)

The central timeline (panel + scoring read it) must tolerate retries/reorder:

- Each event carries an **`event_id` / idempotency key** minted by the facility.
- The GameMgr **ingest endpoint dedups** on that key (at-least-once delivery is
  fine; duplicates collapse).
- **Ordering is GameMgr's ingest sequence**, not facility wall-clock. The
  facility timestamp is metadata only. So container network jitter can't produce
  duplicate or out-of-order rows in the panel/scoring view.

### B-1. Flag service container

Holds the `FlagStore` (planted / by_flag / submissions) + submit-validation
logic. **One** service holds every team's flags + the full token map (so it can
validate any submitter against any victim). Because routing-(i) was chosen
(host MCP forwards), **only the host ever talks to it** — so it is **NOT on any
agent network**: it sits on a host-reachable facility network with a published
port, and agents cannot reach it at all. Endpoint split (defense-in-depth, since
the service isn't agent-reachable):

- **Player-facing:** `submit_flag` only. **Token-auth (`X-Team-Token`)** — the
  host forwards each submit as the submitting team.
- **Infra-facing (`/flags/set` = seed+rotate):** **facility token**, rejects any
  player `X-Team-Token`. The **primary security boundary is facility
  authentication**; keeping the service off the agent net is the second layer.
- **Rotation ownership stays with GameMgr.** The flag service *stores* current
  flags but is **not** the rotation clock: it never decides when to rotate — it
  only applies rotation **commands pushed by GameMgr** (whose round loop remains
  the sole source of round/phase truth). Phase gating reads `StatusPort` (HTTP).
  **Planting stays host-side** (GameMgr execs the flag into prod, then PUSHES the
  resulting flag set to the service), so the flag service needs **no exec endpoint
  at all** — a strictly smaller attack surface than the original "plant via
  constrained exec" sketch.

### B-2. Health poller container

Runs the periodic checker loop off the host process: `ExecPort` (HTTP, structured
`run_health_check`) to probe prod, `StatusPort` for attack-phase gating,
`EventSink` to post `SERVICE_HEALTH_CHECK`. The per-challenge intrinsic poll
interval (the `health_interval_secs` work) is unaffected — it's data on the
challenge. No player-facing endpoints at all.

### Auth boundary (decide explicitly)

Three trust zones, three credentials:

| Caller → callee | Credential | Notes |
|---|---|---|
| agent → host MCP / flag-submit | `X-Team-Token` (player) | relay is transparent, passes header |
| facility → GameMgr (event-ingest / exec / status) | **facility token** (infra) | never the player token |
| GameMgr → flag service (rotate / admin) | facility token (infra) | token is the boundary; control net optional |

The relay (Part A) is **transparent for player traffic only**. Infra
service-to-service endpoints (`event-ingest`, `exec`, `rotate`) require the
facility token and are **not reachable with a player `X-Team-Token`**, so an
agent that reaches a facility container can at most call the player-facing
`submit_flag`.

**Facility-token storage:** injected at runtime via **env / secret** (like the
upstream API key) — **never committed to the repo, never written to run
artifacts (`events.jsonl` / snapshots), never put in an event payload or
exec-audit field.** This is the same security class as raw-audit redaction:
secrets (tokens, flags, raw commands) must never land in observable output.

### Stays on host (non-negotiable)

Docker / orchestrator, the round loop (phase/round/rotation truth), the single
timeline, and the `restart_service` job machinery. Facilities call back via the
HTTP ports; `docker.sock` never reaches an agent-visible container.

---

## Security invariants (carry through both parts)

- No `docker.sock` / Docker-daemon route in any agent-reachable container.
- The relay is a **narrow single-upstream reverse proxy** to the GameMgr app,
  not a generic host gateway.
- The remote exec endpoint is **role-allowlisted (`prod` only) + structured-verb
  (no raw shell) + audited + facility-token-auth** — narrow enough that it isn't
  a back-door to host power. Audit is **two-level**: redacted fields to
  `runs/<ts>/events.jsonl`; full request/command detail only to a **private**
  `runs/<ts>/private/facility_exec_audit.jsonl` — never in the panel snapshot,
  never agent-visible. `events.jsonl` (and anything that consumes it) stays
  secret-free: no flags, no raw commands, no tokens.
- Flag service: **player-facing = `submit_flag` only (token-auth, own-team
  view); plant/rotate/admin are infra-only, gated by facility-token auth** (a
  separate control net is optional defense-in-depth, not the boundary).
- **Player token ≠ facility token.** An `X-Team-Token` can never call
  event-ingest / exec / rotate / admin.
- **Secrets never land in observable output.** The facility token (and the
  upstream API key) are injected via env/secret — never committed to the repo,
  written to `events.jsonl` / snapshots, or placed in any event / exec-audit
  payload. Same rule as raw-audit redaction (no flags, no raw commands, no
  tokens in `events.jsonl`).
- `internal: true` nets + the api-router firewall keep everything else
  unreachable.

## Phasing

1. **Part A — relay** → isolated full game runnable. *(small; do first)*
2. **B-0 — HTTP port boundary (status + events) — SHIPPED (`ea58e41`).**
   `facility/adapters_http.py` (`HttpStatusPort` + `HttpEventSink`) + GameMgr
   **idempotent event-ingest** (`POST /facility/events`) + the **facility token**
   (separate infra credential, fail-closed). Still in-process; no behavior
   change. *The constrained **exec** endpoint is deferred to B-1 — its structured
   verb set is defined by the first facility that containerizes, so it's built
   with its consumer rather than guessed.*
3. **B-1 — FlagServer container — SHIPPED (`5b8910a` app + tests; orchestrator/
   GameMgr wiring this cycle).** `flag/service_app.py` (`build_flag_service_app`)
   reuses `FlagServer` + `FlagStore` behind the B-0 HTTP ports;
   `dockerfiles/flag-service/` builds `cyberarena/flag-service`. **One** service,
   **off the agent net** (host-reachable published port only — agents never touch
   it under routing-(i)). **PUSH model (not exec):** GameMgr plants host-side and
   PUSHES the full flag set + token map to the service (`POST /flags/set`,
   facility token) on each rotation — `store.plant()` drops the prior flag per
   key, so it seeds + rotates with no cache-coherence window. The flag service
   therefore needs **no exec endpoint** (plant stays host-side) — *the constrained
   exec endpoint moves to B-2, built with HealthPoller, its first real consumer.*
   Submit via **routing-(i)**: the host MCP `submit_flag` FORWARDS to the service
   as the submitting team (`GameMgr._forward_submit`, `X-Team-Token`); the service
   validates + records `FLAG_SUBMITTED` via the HTTP EventSink. Forward failure
   returns `ERROR` (retry hint) — never a local accept, which could double-score.
   Config: `flag_service.{enabled, base_url}` in `game.yaml`.
4. **B-2 — HealthPoller container — SHIPPED (`7d2e3c6` verb+audit; container+wiring
   this cycle; live-verified).** Realized as the secure **Option X**: every bundled
   `functionality_test` runs *arbitrary* commands via privileged docker exec, so
   raw exec can't be exposed — the constrained endpoint is therefore the **single
   structured verb** `POST /facility/health-check {team_id, service}` that runs the
   whole test **host-side** (`GameMgr.run_health_check`; privileged exec never
   leaves the host) and returns the CheckResult tree, with **two-level audit**
   (redacted `FACILITY_EXEC` on the timeline + full result in
   `runs/<ts>/private/facility_exec_audit.jsonl`, joined by `request_id`). The
   container (`cyberarena/health-poller`, `health/service_app.py`) is the
   **scheduler + recorder**: it reuses the tested `HealthPoller` grid with a
   `RemoteHealthChecker` (calls the verb, records `SERVICE_HEALTH_CHECK` via the
   EventSink) and exposes infra control endpoints (`/poller/attack-enter|exit|
   trigger-now`). GameMgr drives it via `RemoteHealthPollerHandle` (duck-types the
   in-process poller; phase callbacks unchanged). No player-facing surface; not on
   the agent net (probe + record both go host-ward). Config:
   `health_poller.{enabled, base_url}`.
5. *(optional)* routing-(ii): submit hot path direct to the flag service.

## Open questions / risks

- **Auth boundary shape:** confirm relay stays transparent for player traffic
  only, and that all infra (event-ingest / exec / rotate / admin) endpoints
  require a separate facility token unreachable with a player `X-Team-Token`.
- **MCP client proxy behavior** — handled by choosing a reverse proxy (Part A),
  not the `NO_PROXY` hack.
- **Host reachability from api-router on Linux/EC2 vs Docker Desktop** — verify
  the relay can reach `host.docker.internal:8000` on the target host (Desktop
  routes through a VM; Linux via the bridge gateway). Same smoke as this cycle.
- **EventSink over HTTP** — idempotency-key dedup + ingest-sequence ordering must
  be in place before the `SERVICE_HEALTH_CHECK` volume hits it; the central
  timeline stays the single source for panel + scoring.
- **Confirm flag/health containers never need Docker** — they must only ever use
  the constrained exec endpoint; if any path reaches for `docker.sock`, stop.

## Anticipated files

`apirouter/` (narrow relay route or sidecar), `gamemgr/api.py` (idempotent
event-ingest + constrained exec endpoints + facility-token auth),
`gamemgr/game_mgr.py` (`CYBERARENA_PUBLIC_BASE_URL` → the logical MCP endpoint;
push rotation), `facility/adapters_http.py` (new), `dockerfiles/` (flag-service,
health-poller
images), the redacted-vs-raw audit split (timeline event type + a private
`runs/<ts>/private/facility_exec_audit.jsonl` writer; add `runs/*/private/` to
`.gitignore`), [docs/UML/facility.md](../UML/facility.md) + this plan.
