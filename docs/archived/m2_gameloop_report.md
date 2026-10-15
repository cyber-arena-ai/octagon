# Milestone 2 — Game Loop Runs — Execution Report

**Goal:** `GameMgr` deploys the notes challenge on every prod container, drives a `DEFENSE ⇄ ATTACK` round loop, and emits events on a bus that is broadcast to WebSocket clients, written to an append-only JSONL file, and exposed via REST.

**Status:** Complete. All verification steps in [m2_gameloop_plan.md](m2_gameloop_plan.md) pass.

## What was built

| Path | Purpose |
|------|---------|
| `challenges/notes.py` | First concrete `Service` adapter — ports the reference repo's notes daemon; ships server.py via `put_archive`, runs as a `nohup python3` background process (no systemd) |
| `challenges/registry.py` | `type` -> `Service` class mapping; adding a new vulnerable service = one entry here + one adapter file |
| `orchestrator/service.py` | `Service` ABC updated — `is_alive` is now non-abstract with a generic docker-exec socket-connect default; adapters override for protocol checks |
| `gamemgr/config.py` | Loader for the full `game.yaml` — wraps `TopologyGraph` plus `ChallengeConfig` and `RoundConfig` |
| `gamemgr/events.py` | `Event` dataclass, `EventType` enum, `EventBus` (async pub-sub + in-memory history), `FileSink` (JSONL writer), `new_run_dir` (utc-timestamped artifact directory) |
| `gamemgr/round.py` | `Phase` enum + `RoundLoop` async state machine driving DEFENSE ⇄ ATTACK with phase-entry network hooks |
| `gamemgr/game_mgr.py` | `GameMgr` orchestrates challenge deploy, wires the file sink, runs the round loop, exposes `status()` / `submit_flag()` |
| `gamemgr/api.py` | FastAPI app — `GET /game/status`, `GET /game/rounds`, `GET /game/events`, `POST /game/start`, `POST /game/stop`, `POST /game/flag/submit` (M5 skeleton), `WS /events/stream` |
| `main.py` | New `start` subcommand (blocking: setup -> deploy -> round loop + uvicorn, with SIGINT / `POST /game/stop` graceful shutdown). `setup` / `teardown` preserved |
| `config/game.yaml` | New `challenges:` and `round_config:` sections; node ports updated to 8001:5000 / 8002:5000 |
| `tests/test_m2_gameloop.py` | Integration test: config parse + full lifecycle (setup -> deploy -> 2 rounds -> assert events + JSONL file -> teardown) |
| `requirements.txt` | Added `fastapi`, `uvicorn[standard]`, `httpx` |
| `.gitignore` | Added `runs/` |

## Architectural decisions

- **`challenges/` directory**, not `services/`. `Service` ABC is broader (could cover control-plane gateway / telemetry later); CTF jargon for vulnerable scoring targets is "challenges", which matches the reference repo and stays clear in code.
- **Challenge port + healthcheck declared per-adapter, not per-framework.** `Service.is_alive` default = `docker exec python3 -c 'socket.connect(("127.0.0.1", self.port))'`. NotesService overrides to send `LIST` and verify `COUNT ` response.
- **No systemd inside containers.** Challenge `start()` uses `nohup python3 ... &` via `exec_run(detach=True)`; `stop()` uses `pkill -f`. Keeps prod-base minimal.
- **Event Bus is multi-subscriber.** WS connections, internal history list, and `FileSink` are all just subscribers. New sinks (Slack, OTel, …) plug in without touching round logic.
- **Per-run artifact directory**: `runs/<utc-ts>/events.jsonl`, mirroring the reference repo's match-artifact pattern. `runs/` is gitignored.
- **Network switching stays no-op in M2.** `RoundLoop._enter_phase` calls `orchestrator.network.set_cross_team_network(allow)` and swallows `NotImplementedError`; M3 fills in the real attach/detach.

## Verification

All commands run from repo root inside the project venv.

### M1 regression

```
$ pytest tests/test_m1_topology.py -v
tests/test_m1_topology.py::test_topology_parse        PASSED
tests/test_m1_topology.py::test_setup_then_teardown   PASSED
2 passed in 13.88s
```

### M2 integration test

```
$ pytest tests/test_m2_gameloop.py -v
tests/test_m2_gameloop.py::test_config_loads_challenges_and_rounds PASSED
tests/test_m2_gameloop.py::test_full_lifecycle                     PASSED
2 passed in 17.87s
```

The `test_full_lifecycle` test runs 2 rounds at 1s/phase, asserts `SERVICE_DEPLOYED`, `ROUND_START`, `PHASE_CHANGE`, `ROUND_STOP` all appear in `bus.history()` and in the persisted `events.jsonl`.

### End-to-end (live)

```
python3 main.py start &
```

Log excerpt:
```
orchestrator setup: 3 networks, 4 nodes
created container team1_prod (image=cyberarena/prod-base)
deploying challenge notes -> team1_prod (port 5000)
deploying challenge notes -> team2_prod (port 5000)
game start; events -> runs/20260522T215343Z/events.jsonl
round loop start: total_rounds=3 defense=10s attack=10s
Uvicorn running on http://0.0.0.0:8000
```

```
$ curl -s :8000/game/status
{"phase":"ATTACK","round":1,"total_rounds":3}
```

Notes healthcheck through the docker DNS path an agent would actually use:
```
$ docker exec team1_dev bash -c 'echo LIST | nc -q1 team1_prod 5000'
COUNT 0
```

Events stream (truncated):
```
$ curl -s :8000/game/events | jq '.[].type'
"SERVICE_DEPLOYED"
"SERVICE_DEPLOYED"
"ROUND_START"
"PHASE_CHANGE"   // DEFENSE
"PHASE_CHANGE"   // ATTACK
...
```

Graceful shutdown via API:
```
$ curl -s -X POST :8000/game/stop
{"ok":true}
```

Shutdown log:
```
round loop stopped at round=2 phase=STOPPED
Application shutdown complete.
orchestrator teardown
removed container team1_dev
removed container team1_prod
removed container team2_dev
removed container team2_prod
removed network team1_net
removed network team2_net
removed network game_net
orchestrator teardown complete
```

Artifacts on disk:
```
$ ls runs/
20260522T215343Z

$ head -5 runs/20260522T215343Z/events.jsonl
{"id": "d84ad22597d6", "ts": ..., "type": "SERVICE_DEPLOYED", ...}
{"id": "99797d27f2e7", "ts": ..., "type": "SERVICE_DEPLOYED", ...}
{"id": "15e75ddd03a3", "ts": ..., "type": "ROUND_START", "round_id": 1, ...}
{"id": "5de934d8034a", "ts": ..., "type": "PHASE_CHANGE", "round_id": 1, "phase": "DEFENSE", ...}
{"id": "519021871384", "ts": ..., "type": "PHASE_CHANGE", "round_id": 1, "phase": "ATTACK", ...}
```

## Deferred to later milestones

| Item | Milestone |
|------|-----------|
| Agent install + `deploy_complete` reporting (drives real defense duration via `last_deploy_time`) | M4 |
| `POST /game/flag/submit` real verification + `FLAG_SUBMITTED` event | M5 |
| Browser panel consuming `/events/stream` | M6 |
| Poller / Healthcheck / Scoreboard implementations | M2+ as needed |