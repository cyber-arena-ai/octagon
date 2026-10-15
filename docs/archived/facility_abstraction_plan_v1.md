# Facility abstraction — plan v1

**Status:** Phase 1 shipped 2026-06-09. Phases 2–3 designed here, not yet built.

## Problem

`FlagServer` and `HealthPoller`/`HealthChecker` are already independent,
dependency-injected facilities — but they reach into `GameMgr`'s *process*
through three in-memory couplings:

| Coupling | Pre-refactor | Crosses a process boundary if the facility is containerized? |
|---|---|---|
| round / phase | `status_resolver=self.status` (a Python call) | yes |
| event recording | `self.timeline.record(...)` (append to an in-process list) | yes |
| docker exec into prod | `VulboxTarget.meta["exec_in_container"] = self._exec_in_container` | yes |

To make a facility containerizable later, those three must become a
*named boundary* that can swap an in-process implementation for an
HTTP one — **without touching facility logic or challenge code.**

## Decision (chosen with the user)

- **Runtime:** container-*ready*, still in-process now. Define the
  boundary + in-process adapters; do not ship container scaffolding.
- **Event flow (when containerized):** GameMgr owns a single timeline;
  facilities POST events back to a GameMgr ingest endpoint.
- **Exec (when containerized):** GameMgr owns docker; facilities call a
  GameMgr exec endpoint **addressed by identity** `(team_id, role)` — never
  a raw container name — so an untrusted caller can't exec into anything.

## The boundary — three ports

`facility/ports.py` (stdlib-only, no `gamemgr`/`flag`/`health` imports):

```
StatusPort.status() -> {round, phase, total_rounds}
EventSink.record(type_, *, team_id, round_id, phase, payload)   # mirrors Timeline.record
ExecPort.exec(team_id, role, cmd, *, timeout_s=120.0) -> ExecResult
ExecResult(exit_code, stdout, stderr="", timed_out=False)
```

`facility/base.py`:

- `FacilityContext(status, events, exec=None)` — the environment bundle a
  facility receives. `exec` is optional: a facility whose targets already
  carry an exec callable (FlagServer, HealthChecker) doesn't read it; the
  *target builder* does.
- `Facility(ABC)` — deliberately thin: `name` (abstract) + `start()`/`stop()`
  (concrete async no-ops). It does **not** impose a daemon shape. FlagServer
  overrides neither; HealthPoller overrides `stop()`.

### Why ExecPort is distinct from `exec_in_container`

Challenge handlers consume `target.meta["exec_in_container"]`, contracted as
`(container_name, cmd) -> (rc, output)` in
[`challenges/interface/probe.py`](../UML/challenges.md). That's a public
challenge-author interface and is byte-frozen. So `ExecPort` is a *separate*
boundary primitive; `GameMgr.target_for` injects a thin shim that adapts
`ExecPort` → the tuple callable:

```python
def _exec_via_port(_container_name, cmd):      # container_name ignored:
    r = exec_port.exec(team_id, "prod", cmd)   # this is the prod target,
    return r.exit_code, r.stdout               # so identity == (team, prod)
```

This is what lets the exec transport change (in-process → HTTP) with **zero**
change to challenge code.

## In-process adapters (the default wiring)

`facility/adapters_local.py`, built once in `GameMgr.__init__`:

- `LocalStatusPort` → `GameMgr.status`
- `LocalEventSink` → `GameMgr.timeline.record` (duck-typed; no gamemgr import)
- `LocalExecPort` → `GameMgr._resolve_container` + a late-bound call to
  `GameMgr._exec_in_container`. Runs the blocking docker exec on a **daemon
  thread** with `join(timeout_s)`, so a hung exec releases the caller (the
  daemon dies with the process and never blocks interpreter exit). Errors
  from the docker layer propagate unchanged.

### Timeout note

The default `timeout_s` is a generous **120s** — a hang safety net, not a
tight budget — because the restart-handler compile path legitimately runs
for tens of seconds and the challenge-facing callable carries no per-call
timeout. Per-operation budgets (short for health probes, long for restart)
are a Phase 2 refinement once the boundary carries operation context.

## Round/phase exposure

Already served by `GET /game/status` (`{phase, round, total_rounds}`). That
is the canonical state endpoint for both facilities and the agent. An MCP
`get_game_state` tool for the agent is **deferred to Phase 2** (the agent
has no need to poll phase yet).

---

## Phase 2 — GameMgr API surface (designed, NOT built)

Two authenticated endpoints turn the in-process couplings into wire
protocols so a facility can run out-of-process:

- `POST /facility/events` — server side of `EventSink`. Validates the
  `EventType`, **stamps `source` server-side** (derived from the facility
  token / endpoint, never trusted from the body) so a facility can't spoof
  another's events. Refuses flag-bearing payloads.
- `POST /facility/exec` — server side of `ExecPort`. Body
  `{team_id, role, cmd, timeout_s}`; **GameMgr resolves the container** from
  `(team_id, role)`. Returns `{exit_code, stdout, stderr, timed_out}`.

### Security (load-bearing)

`/facility/exec` is arbitrary command execution into prod containers and
`/facility/events` can write the public timeline. Both:

- require a **separate `facility_token`** (not a team token), and
- must bind to an internal interface **unreachable by team/agent
  containers**. The agent's `get_game_state` is read-only and must never be
  able to reach exec/events.

### Submit path

`POST /game/flag/submit` delegates to the in-process `FlagStore` today. When
the flag facility is containerized, the `FlagStore` moves with it, so this
endpoint **proxies** to the flag facility's own `submit`. Not built until the
flag facility actually runs out-of-process.

## Phase 3 — HTTP adapters + entrypoint + Dockerfile (DROPPED for now)

Deliberately not written: shipping unused container scaffolding (HTTP
adapter classes, a `run.py` entrypoint, a Dockerfile, a deployment manifest)
would be untested code debt. It lands only when a facility is actually
flipped out-of-process — at which point it implements exactly the Phase-2
endpoints above.

## File map

| Path | Role |
|---|---|
| `facility/ports.py` | `StatusPort` / `EventSink` / `ExecPort` Protocols + `ExecResult` |
| `facility/base.py` | `Facility` ABC + `FacilityContext` |
| `facility/adapters_local.py` | `LocalStatusPort` / `LocalEventSink` / `LocalExecPort` |
| `facility/__init__.py` | package re-exports |

See [UML/facility.md](../UML/facility.md) for the live class/sequence diagrams.
