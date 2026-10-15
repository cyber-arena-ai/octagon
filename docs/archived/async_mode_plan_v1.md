# Async Mode Plan (v1)

Status: **shipped 2026-06-12** (commit `873e970`). Adds a second game mode
alongside the existing "sync" mode, selectable via `round_config.mode: sync|async`.
Live design: `docs/UML/game_manager.md` + `docs/UML/overview.md`. Offline tests
green (tests/test_dcf_async_mode.py + full suite); the real Docker run
(`python main.py start -c config/game-async.yaml`) is the remaining end-to-end check.

## Context

Today the arena runs one mode ("sync"): each round is a strict DEFENSE→ATTACK
sequence, and **a fresh `claude -p` process is spawned per team per phase** then
killed at phase end. The network *is* the phase — DEFENSE physically disconnects
cross-team networks, ATTACK reconnects them (`round.py:_enter_defense/_enter_attack`).

"Async" mode instead runs, per team, **one long-lived agent** prompted **once** at
start with a combined attack+defense briefing; it self-directs by polling
`get_game_state` over MCP instead of receiving a new prompt per phase. Attack and
defense happen **simultaneously** — networks stay connected the whole game (no
protected patch window). A **round tick still exists** purely to rotate flags every
`round_secs`; scoring and health checks run continuously. The agent is
**supervised**: on an ACCIDENTAL exit before game-over (crash / non-zero / timeout)
the orchestrator re-sends the SAME briefing with `--continue` (memory preserved); a
clean exit (exit 0 = agent reported completion) is left final, no restart. At game
end live agents are killed. **Sync behavior must not change.**

### Decisions (user-confirmed)
1. Always-on networks in async (no safe window).
2. Periodic flag rotation (round tick = rotation interval); submit/health continuous.
3. Supervised respawn with `--continue` — only on accidental exit; the agent is
   given the same briefing once at start and re-sent it on restart (no resume nudge).
4. Async artifacts are flat under `artifacts/<team>/` (no `round-N` subdir).

## The elegant part: a `LIVE` phase value

Async runs at `Phase.LIVE` the whole game (STOPPED at end). The three existing
phase gates then behave correctly with **zero changes**:
- `FlagServer.submit` rejects only `phase==DEFENSE` (`flag/server.py`) → LIVE accepts.
- `restart_service` rejects only `phase==ATTACK` (`game_mgr.py`) → LIVE allows.
- `record_deploy_complete` early-end couples only when `phase==DEFENSE` → LIVE
  records but never calls `end_phase_early`.

Adding `LIVE` to the enum changes no `== DEFENSE` / `== ATTACK` outcome.

## Architecture: separate `AsyncLoop`, supervisor in `main.py`

A parallel `AsyncLoop` driver (not a branch inside `RoundLoop`) keeps the tight
sync state machine untouched. The agent supervisor lives in `main.py` (where the
transport/manager/handle machinery already is); GameMgr never owns the transport,
matching the sync wiring (`_wire_phase_kickoffs`). GameMgr exposes registration
hooks; the loop + main.py supply the bodies — symmetric with sync.

## Files

### Create — `gamemgr/async_loop.py` (`class AsyncLoop`)
Mirror `RoundLoop`'s constructor `(game, network, config)` and surface so
`GameMgr.status()`/`stop()` work unchanged: `.phase` (starts `LIVE`), `.round`,
`.run()`, `.stop()`, `.end_phase_early()` (no-op). `run()`:
1. `network.set_cross_team_network(True)` **once**; phase = LIVE.
2. `game.start_async_agents()` (per-team supervisors as tracked tasks).
3. `game.health_poller.on_attack_enter(0)` **once** (continuous; no `on_attack_exit`).
4. loop `round = 1..total_rounds`: `on_round_start` → `flag_server.rotate(round)` →
   `_wait(round_secs)` → `_persist_service_logs(round)`.
5. finally: phase = STOPPED, set `game._game_over`, stop poller, `on_game_end`.

### Modify
- `gamemgr/config.py` — `RoundConfig`: `mode: str = "sync"`, `round_secs: int = 60`;
  parse + validate.
- `gamemgr/round.py` — add `Phase.LIVE`; no other change.
- `gamemgr/game_mgr.py` — `_game_over` Event + async handler slots; `start()` mode
  branch (AsyncLoop vs RoundLoop, sync arm byte-identical); `register_async_supervisor_handler`,
  `register_kill_all_agents`, `start_async_agents()`; `record_async_chunk_started/done`
  (writes `stream-async-<n>.jsonl` / `agent-async-<n>.log` / `metadata-async-<n>.json`
  via `team_path_for` — flat under `artifacts/<team>/`, no `round-N`);
  `is_game_over()`; `stop()` async branch
  (set `_game_over` + `kill_all_agents` before drain); `status()` adds `mode` +
  async `round_secs`/`seconds_to_rotation`; `async_agent_config_for(team_id)`.
- `agents/prompts/` — `build_async_prompt(cfg, *, service_name, port, defendable)`:
  the agent's ONLY prompt, given once at start and re-sent verbatim on restart (no
  separate resume prompt). Frames the SAME per-challenge attack body + the SAME
  per-challenge defense body as sync under one continuous loop — the defense body
  comes from `build_defense_prompt(..., phase_gated=False)`, which drops only the
  WRONG_PHASE block (keeping source layout / vuln hotspots / mitigations). A
  `phase_gated` flag was threaded through the defense dispatcher → per-challenge
  override (nautilus_ico) + `generic_defense_prompt`. Non-defendable services
  (attack-only, e.g. notes) get the `NO_DEFENDABLE_SERVICE` one-liner instead.
- `main.py` — `_wire_async_supervisor(game)` (parallel to `_wire_phase_kickoffs`,
  reusing `backend`/`_mgr_for`/`handle_for`): per-team supervisor + `kill_all_agents`
  callback. Branch in `_run_start` on `cfg.rounds.mode`. Constants
  `ASYNC_CHUNK_TIMEOUT` (bounded — **not** whole-game, so end-of-game kill works),
  `ASYNC_MIN_HEALTHY_SECS`, `ASYNC_MAX_FAST_EXITS`. Supervisor (`_run_async_agent`):
  loop run_phase → record chunk → break if game over → break if clean exit (exit 0)
  → else accidental exit: crash-loop backoff/giveup → re-send same briefing.
- `gamemgr/mcp_server.py` — `get_game_state` docstring: add `LIVE` + async fields
  (logic unchanged; returns `game.status()`).
- `gamemgr/transcripts.py` — find async files flat under `artifacts/<team>/`
  (round_id=None) as well as sync `round-N/`; order `stream-async-<n>.jsonl` by
  chunk number.
- `config/game-async.yaml` — new sample (`mode: async`, small `round_secs`); leave
  the default sync `config/game.yaml` untouched.
- Docs — `docs/RUN_ARTIFACT_SPEC.md` (async subsection), `docs/UML/game_manager.md`,
  `docs/UML/overview.md`.

## Risks (designed-for)
- **Context-window growth**: one session whole-game via repeated `--continue`.
  Mitigate with bounded chunk timeout + modest
  `round_secs*total_rounds`. Biggest behavioral risk.
- **Crash-loop respawn**: `ASYNC_MIN_HEALTHY_SECS` backoff + `ASYNC_MAX_FAST_EXITS`
  giveup (distinct event so the panel shows the death).
- **Flag rotation vs in-flight submit**: single-threaded loop ⇒ no interleave; a
  just-too-late submit is correctly judged stale. No change.
- **Health checks during restart downtime**: poller records the failing window —
  desired telemetry, same as sync attack-phase restarts.

## Sync-regression gate
Only shared edits are additive: `Phase.LIVE`, `RoundConfig` defaulted fields,
`status()` extra key, `start()`/`stop()` mode branch (sync arm unchanged). Do
**not** touch `_enter_defense/_enter_attack/_wait/enter_*_phase/record_phase_done/
FlagServer.submit/restart_service`.

## Verification
- Offline tests (no Docker; handler-less GameMgr, match `test_dcf_*`/`test_m*`):
  config parse/validate; **regression gate** (sync `RoundLoop` tests unchanged with
  `LIVE` added); AsyncLoop side-effects (network on once/never off, no
  enter_*_phase, rotate × total_rounds, on_attack_enter once, LIVE→STOPPED,
  `_game_over` set); supervisor backoff/giveup + nudge + chunk artifacts; phase
  gates under LIVE; `status()`/`get_game_state` fields; async `stop()` kills before
  drain.
- Real run (Docker + claude): `python main.py start -c config/game-async.yaml` —
  panel shows LIVE; flags rotate ~`round_secs`; `FLAG_SUBMITTED accepted=true` +
  `SERVICE_RESTART_*` mid-game concurrently; continuous `SERVICE_HEALTH_CHECK`;
  `stream-async-*.jsonl` accrue and render in `/game/agents`; at end phase STOPPED,
  GAME_END, no live `claude`, clean teardown. Negative: break one team's `api_key`
  → that supervisor backs off & gives up; the other team keeps playing.
