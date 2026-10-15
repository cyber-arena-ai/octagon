# Facility direct-exec (SSH) + MCP containerization — plan v3

**Status:** BUILT + unit-tested + **LIVE-VERIFIED** (isolated ico run, 2026-06-18).
Live run confirmed: orchestrator launches the facility containers; facility SSH
key provisioned (coexists with the agent pivot key); flag-service plants over SSH
(FLAG_PLANTED ×4); health-poller probes over SSH (SERVICE_HEALTH_CHECK ×24);
agents connect to the mcp-gateway (POST /mcp/ 200, DRY_RUN_OK) and tools forward
to GameMgr REST (status, report_ready). The run also surfaced + fixed two bugs:
the gateway DNS-rebinding allow-list (agents reach it by container name) and the
flag-service rotate timeout (SSH plant > 10s). `submit_flag` via gateway →
flag-service was verified separately with a real MCP client (ACCEPTED). Remaining:
step 5 (remove the in-process path) is now unblocked.

Done: `SshExecPort`; flag-service plants over SSH
(`/flags/configure` + `/flags/rotate`); health-poller probes over SSH
(`/poller/configure` + control endpoints); GameMgr configure/rotate-signal +
`handle.configure`; mcp-gateway (live-verified) + GameMgr REST mirrors + agent
`mcp_endpoint`→gateway config; all three images build (ssh + challenge code).
**Pending (deployment / live-verify — see "Deployment checklist" at the end):**
facility SSH-key provisioning, `facility_net`, the full isolated game run, and —
*after* that proves out — removing the in-process path (step 5).

Two moves in one direction — **services run
as their own on-net containers and reach what they need directly; GameMgr becomes
a backend that executes/serves, not a relay/chokepoint.** Each removes a host-relay
hop shipped or assumed in v2:

1. **Facility direct-exec.** Facilities (flag-service, health-poller) reach prod
   **directly over SSH** instead of through the v2 host-relay (B-1 flag push +
   forward, B-2 `/facility/health-check` verb + `RemoteHealthPollerHandle`).
   **Supersedes the remote-exec boundary of
   [facility_containerization_plan_v2.md](facility_containerization_plan_v2.md).**
2. **MCP containerization.** The MCP server moves into its own on-net container
   so agents reach it **directly, with no api-router relay** — it forwards tool
   calls to GameMgr (which still executes privileged ops like restart).
   **Supersedes v2's "MCP routing (why the host MCP stays)" section.**

GameMgr keeps the round loop, the timeline, and Docker orchestration throughout.

## Containerized-only (no in-process fallback) — SHIPPED

v3 makes the containerized facilities **the only path** — the in-process facility
version that B-0/B-1/B-2 kept as the default is **removed**, not retained as a
fallback. Done (`f07c0d9`): dropped GameMgr's in-process FlagServer/
HealthChecker/HealthPoller + `game.flags`, the `enabled` toggles, and the host
in-process MCP (`build_mcp_app`/`GameMgrBackend`); submit always forwards, plant/
rotate always signal the service. Caveats vs the original sketch: (a) the `Local*`
adapters survive as in-memory **test doubles** for the facility-class unit tests,
and `LocalExecPort` also backs GameMgr's own host docker-exec (deploy / restart /
target_for — not a facility); (b) the dead v2 host-relay code (the
`/facility/health-check` verb, `RemoteHealthChecker`) was removed in `b76c971`.
Concretely:

- **Drop** the `Local*` adapters (`LocalStatusPort` / `LocalEventSink` /
  `LocalExecPort`) and the `flag_service.enabled` / `health_poller.enabled`
  toggles — facilities are *always* the containers, so there's no enable/disable.
- **Drop** GameMgr's in-process `FlagServer` / `HealthChecker` / `HealthPoller`
  construction. GameMgr no longer owns flag logic or runs probes; it signals the
  containers (rotate-now, attack-enter/exit) and serves the round/phase/timeline
  backend they call.
- **Drop** the host in-process MCP (`build_mcp_app` mounted on the host app); the
  `mcp-gateway` container is the only MCP server. The tool *definitions* are
  reused, bound to HTTP backends instead of `game.*`.

Trade-off accepted: running the game now *requires* the facility + gateway
containers up (no single-process local mode). Tests still unit-test the pieces
in-process (the apps are plain FastAPI/ASGI), but the deployed system is
containers-only.

## Motivation

In v2, every privileged operation a facility needed (plant a flag, run a
functionality test) was relayed through GameMgr: the host held the docker-exec
chokepoint and facilities asked it over HTTP. That keeps the host on the hot
path and makes facilities not-really-autonomous.

v3 makes each facility own its full job. The `ExecPort` was always "run a
command inside a team's container, addressed by identity `(team_id, role)`";
v2 realized its transport as HTTP-to-host, v3 realizes it as **SSH facility→prod**.
GameMgr keeps the round loop, the timeline, and Docker orchestration — but it is
no longer in the plant / probe path.

## Trust model (the explicit tradeoff)

This **widens the facility blast radius on purpose.** v2's rule was "raw exec is
the highest-risk surface; keep it host-side behind structured verbs." v3 instead
trusts the facility containers as **infra** (not agent-controlled, not on the
agent network) and hands them an SSH key into prod. If a facility is compromised,
the attacker gets what that key grants on prod — directly, not filtered through a
verb. We accept this because:

- Facility images are ours, built from this repo; agents never run in them and
  cannot reach them (they sit off the agent net — see networks below).
- The key is injected at runtime via **secret/env** (like the facility token and
  the upstream API key) — never in the repo, run artifacts, or events.
- Optional hardening (below) can narrow what the key can do without going back
  to a host relay.

If that trust model is ever wrong for a deployment, v2's host-relay path remains
the fallback design.

## Networks

Facilities must reach prod **without** being on the agent net (agents must not
reach facilities) and **without** giving prod new internet egress (prod stays
isolated in isolated-mode play).

- **`facility_net` — `internal` bridge.** Members: `flag-service`,
  `health-poller`, and **every prod container** (prod becomes multi-homed:
  `team_net` for the agent + `facility_net` for facilities). No agent, no NAT —
  so facilities ↔ prod only, and prod gains no egress.
- Facilities are **also** host-reachable (a second NIC on a normal bridge +
  `extra_hosts: host.docker.internal`) for their host calls: `StatusPort`
  (round/phase), `EventSink` (telemetry), and the rotation signal. This mirrors
  how api-router is multi-homed (internal team nets + egress_net).
- `facility_net` is always-on (not phase-gated): plant/probe happen in both
  phases as the round loop dictates. It is not a cross-team attack net.

## SSH key provisioning

- Generate one **facility keypair** at deploy time (ed25519), analogous to the
  per-team agent dev→prod pivot in `agents/_provisioning.py`.
- Add the facility **pubkey** to **every prod** box's `authorized_keys`
  (`root`, reusing the existing `PermitRootLogin prohibit-password` + sshd-HUP
  path) — or a dedicated restricted user (see hardening).
- Inject the **privkey** into the facility containers via secret/env only.
- Prod already runs sshd (the agent pivot depends on it); no new daemon.

## ExecPort realization: `SshExecPort`

A new adapter alongside `LocalExecPort` (docker-exec) and the v2 HTTP path:

```
SshExecPort.exec(team_id, role, cmd, timeout_s) -> ExecResult
  1. role allowlist: only role == "prod" (never "dev"); reject otherwise.
  2. resolve (team_id, "prod") -> prod hostname via an identity map the
     facility holds (pushed in config / on a host control call) — the facility
     addresses by identity, never a caller-supplied container name.
  3. ssh -i <facility_key> -o BatchMode=yes root@<prod> -- bash -c <cmd>
     (bounded by timeout_s; merge/stream as ExecResult).
```

The challenge-facing `target.meta["exec_in_container"](host, cmd) -> (rc, out)`
contract is unchanged — `target_for` (or its facility-side equivalent) just
binds it to `SshExecPort` instead of docker-exec/HTTP. So `FlagHandler.plant`
and `FunctionalityTest.run` work verbatim; only the transport moved.

## Per-facility changes

### flag-service
- **Plants/rotates via SSH directly.** `FlagServer.plant_flags` →
  `challenge.flag_handler.plant(target, flag)` with `target` backed by
  `SshExecPort`. No more host plant + `/flags/set` push.
- **Rotation clock stays with GameMgr** (it owns round/phase truth). GameMgr
  signals "rotate now" — a thin control call `POST /flags/rotate {round_id}` (or
  the service self-gates by polling `StatusPort` for a round change). The service
  then plants fresh flags over SSH. GameMgr pushes a *command*, never the flags.
- Submit validation unchanged: the service holds the `FlagStore` it just planted,
  so it knows the live flags; the host MCP still forwards submits to it.

### health-poller
- **Runs the functionality test directly via SSH.** `exec_in_container` →
  `SshExecPort`; the poller probes prod itself. The v2 host verb
  (`/facility/health-check`, `GameMgr.run_health_check`) and the
  `RemoteHealthPollerHandle` host relay are **removed**.
- The poller keeps its scheduler (the tested grid) and records
  `SERVICE_HEALTH_CHECK` via `EventSink`. Phase transitions: GameMgr drives the
  existing control endpoints (`/poller/attack-enter|exit|trigger-now`) **or** the
  poller self-gates via `StatusPort`. (Decision below.)

## Audit (moves to the facility)

With exec direct, the **facility** owns the audit the host used to write:
- **Redacted** → `EventSink` → central timeline (`FACILITY_EXEC`: op, team_id,
  role, exit_code, duration_ms, request_id) — secret-free, as in v2.
- **Raw** (full command + output) → the facility's own private log, or a host
  `POST /facility/exec-audit` private sink, joined by `request_id`. **Never** in
  `events.jsonl`, panel snapshots, or anything agent-visible.

## Optional hardening (narrow the key without a host relay)

- Dedicated `facility` SSH user on prod (not root) with only the perms plant +
  probe need.
- A **forced command** / `command=` in `authorized_keys` restricting the key to
  a fixed wrapper, approximating v2's structured-verb narrowing while keeping the
  call facility→prod.
- Per-prod keys instead of one shared facility key (limits lateral movement).

## MCP server containerization (agents reach MCP on-net, no relay)

v2 argued the host MCP stays put ("the agent↔MCP seam is already HTTP;
containerizing buys nothing; `restart_service` can't move — it drives Docker").
v3 reverses that on one observation: **the MCP container doesn't execute the
privileged ops — it forwards them to GameMgr, which executes.** Docker stays on
the host; only the MCP transport relocates.

**Shape.** A dedicated `mcp-gateway` container runs the FastMCP app (the same
tool set as `build_mcp_app`), but each tool is a thin HTTP client to GameMgr's
REST/control API instead of an in-process `game.*` call. It holds **no game
state** and **no token map** — it forwards the agent's `X-Team-Token` to GameMgr,
which resolves identity authoritatively.

| MCP tool | forwards to |
|---|---|
| `submit_flag` | `POST {flag-service}/game/flag/submit` (X-Team-Token) — **direct to the flag-service; GameMgr not in the path** (routing-(ii), see below) |
| `get_game_state` | `GET {gm}/game/status` |
| `get_attacks_received` | `GET {gm}/game/attacks` (own-team view, token-scoped) |
| `restart_service` | `POST {gm}/game/restart` (X-Team-Token) → **GameMgr executes the docker restart** |
| `report_ready` / `report_deploy_complete` | `POST {gm}/game/report/*` (X-Team-Token) |

**Submit goes direct to the flag-service (routing-(ii), finally).** The
flag-service already exposes a *player-facing* `/game/flag/submit` (X-Team-Token,
maps token→team via the pushed token map, validates locally, records
`FLAG_SUBMITTED` via its EventSink) — it was built to be called by a token-holder
directly. So the gateway just passes the agent's `X-Team-Token` through to the
flag-service; **GameMgr leaves the submit hot path entirely** (`_forward_submit`
is unused in the gateway path). v2 picked routing-(i) (host forwards) only because
the agent used the *host* MCP; containerizing MCP unlocks the direct path we
deferred. Requirement: the gateway can reach the flag-service — both are off-agent
infra, so they share the facility/host-reachable net. Identity still resolves at
the flag-service (it holds the token map); GameMgr is never consulted for a submit.

**Network.** The mcp-gateway is multi-homed like api-router: on the agent net(s)
(agents reach it directly — **no api-router MCP relay**) + a host-reachable net
(to forward to GameMgr). It is token-scoped and never forwards agent↔agent
traffic — it only makes backend calls — so one multi-homed container can serve
all teams without bridging their isolated nets.

**Auth/identity.** Player `X-Team-Token` flows agent → mcp-gateway → GameMgr;
GameMgr resolves token→team and authorizes every privileged effect (restart only
your own service, etc.). The gateway is effectively untrusted: even if reached
directly, nothing executes without GameMgr's token check.

**GameMgr side.** Add player-token REST endpoints for the GameMgr-coupled tools
that aren't REST yet — `restart_service` and `report_*` are MCP-only today
(`submit`/`status` already exist). GameMgr keeps executing privileged ops; Docker
stays host-only — **never `docker.sock` in the gateway.**

**What it removes.** The Part A api-router **MCP relay** leaves the agent path
(api-router stays as the LLM gateway + egress proxy). The agent's `mcp_endpoint`
points at the mcp-gateway, not the relay.

## Open decisions

**Facility exec:**
1. **Health phase-gating:** GameMgr-driven control endpoints (keep round-loop as
   the clock, explicit) vs. poller self-gates via `StatusPort` (fewer host calls,
   but the poller now interprets phase). *Lean: keep GameMgr-driven for one clock.*
2. **Rotation signal shape:** `POST /flags/rotate {round_id}` push vs. service
   polls `StatusPort`. *Lean: push, mirrors the health control endpoints.*
3. **SSH user/privilege:** root (simplest, matches the agent pivot) vs. restricted
   facility user + forced command (more setup, smaller blast radius).
4. **Raw-audit sink:** facility-local file vs. host private endpoint.

**MCP gateway:**
5. **submit routing from the gateway:** ~~via GameMgr~~ → **DECIDED: direct to the
   flag-service** (routing-(ii); GameMgr out of the submit path — see table above).
6. **Host MCP fate:** ~~keep in-process for local runs~~ → **DECIDED: drop it**
   (see "Containerized-only" below — no in-process fallback). The gateway reuses
   the `build_mcp_app` tool *definitions*, bound to HTTP backends.
7. **One multi-homed gateway vs. per-team gateway containers.**

## Migration from v2 (what gets removed when this ships)

- `GameMgr._push_flags_to_service` + `_forward_submit`'s dependency on a pushed
  set → flag-service plants its own.
- `GameMgr.run_health_check` + `POST /facility/health-check` + `RemoteHealthChecker`
  + `RemoteHealthPollerHandle` → poller probes prod over SSH.
- Add: `facility/adapters_ssh.py` (`SshExecPort`), facility key provisioning,
  `facility_net`, the rotation-signal control call.
- Keep: `HttpStatusPort`, `HttpEventSink`, the facility token, the player/infra
  split, the redacted+private two-level audit (now emitted by the facility).

MCP containerization:
- Add: `mcp-gateway` container + image (FastMCP tools bound to GameMgr REST);
  GameMgr player-token REST endpoints `POST /game/restart`, `POST /game/report/*`,
  `GET /game/attacks`; point the agent's `mcp_endpoint` at the gateway.
- Remove from the agent path: the api-router **MCP relay** (`apirouter/relay.py`
  may stay for any non-MCP host passthrough, but MCP no longer uses it).
- GameMgr's in-process MCP (`build_mcp_app`) either retires or stays for
  non-isolated/local runs (decision 6).

## Deployment checklist (live-verify — not yet done)

The repo code is in; these are the deployment steps to actually run v3 and the
order to verify in. None are unit-testable — they need real containers + prod +
a game.

1. **Build the three images** (from repo root):
   - `cyberarena/flag-service`, `cyberarena/health-poller` (now ssh + challenge
     code), `cyberarena/mcp-gateway`. Add any deployed challenge's heavy deps to
     the facility `requirements.txt` (e.g. pwntools for treasury).
2. **Launch + SSH-key provisioning — WIRED (code).** The facility containers are
   ordinary `nodes:` entries, so `orchestrator.setup()` launches them at the
   beginning like any node. Set `container:` on the `flag_service` /
   `health_poller` config to the node id; `GameMgr.provision_facilities()` (called
   from `main.py` after `deploy_challenges`, before `plant_initial_flags`) then
   `provision_facility_ssh`: generates one facility ed25519 key, **appends** the
   pubkey to every prod's root `authorized_keys`, and drops the privkey into the
   named facility containers at `CYBERARENA_SSH_KEY_PATH` (default `/keys/facility`).
   `provision_ssh_pivot` (agent) now also **appends**, so the agent + facility keys
   coexist regardless of order. (Optional hardening: restricted user + forced
   command — not yet coded.)
3. **`facility_net`**: an `internal` bridge that the flag-service, health-poller,
   and every prod container join (prod multi-homes: `team_net` + `facility_net`);
   agents are NOT on it. Facilities also need host reachability (Status/Events) —
   give them a second NIC + `extra_hosts: host.docker.internal` like api-router.
4. **Config** (`game.yaml`): enable the three and point them at host-reachable URLs:
   ```yaml
   flag_service:  { enabled: true, base_url: http://127.0.0.1:9001 }
   health_poller: { enabled: true, base_url: http://127.0.0.1:9201 }
   mcp_gateway:   { enabled: true, agent_url: http://mcp-gateway:8080 }
   ```
   Set `CYBERARENA_FACILITY_TOKEN` on the host + all facility containers.
5. **Drop the MCP relay** from the api-router config (agents now reach MCP via the
   gateway). api-router stays as the LLM gateway + egress proxy.
6. **Live verify (order):** (a) flag-service `/flags/rotate` plants a flag into
   prod over SSH (check the prod box); (b) an agent submits via the gateway →
   flag-service direct → ACCEPTED + FLAG_SUBMITTED on the host timeline;
   (c) health-poller probes over SSH → SERVICE_HEALTH_CHECK on the timeline;
   (d) restart_service via gateway → GameMgr executes. Then run a full isolated game.
7. **Step 5 — remove the in-process path** ONLY after (6) proves out: drop the
   `Local*` adapters, the `enabled` toggles, GameMgr's in-process FlagServer/
   HealthChecker/HealthPoller, the host in-process MCP, and the now-dead
   `RemoteHealthChecker`. This is a large refactor (touches ~hundreds of tests
   that build in-process facilities) — sequence it last so there's always a
   working path until v3 is proven.
