# Milestone 5 — Flag Plant + Submit Works — Execution Report

**Goal:** plant one flag per (team, challenge) at game start, and make `POST /game/flag/submit` a real four-class verifier (`ACCEPTED` / `UNKNOWN` / `OWN` / `DUPLICATE`) that emits a `FLAG_SUBMITTED` event whose payload never carries the flag string. End-to-end: a team-2 attacker pivoting through its own prod over `game_net` should be able to steal team-1's flag from `team1_prod` and submit it for credit.

**Status:** Complete. All M1+M2+M3+M4+M5 tests pass (33 / 33).

## What was built

| Path | Change |
|------|--------|
| `gamemgr/flag.py` | New: `new_flag()` (FLAG{<32 [A-Z0-9]>}, matches v1 shape), `SubmitResult` dataclass, `FlagStore` (planted dict + inverted-flag index + per-(submitter, victim, service) submission set) |
| `gamemgr/events.py` | Added `FLAG_PLANTED`, `FLAG_SUBMITTED` |
| `orchestrator/service.py` | Added `Service.plant_flag(container, flag)` — non-abstract default that raises `NotImplementedError`; participating challenges override |
| `challenges/notes.py` | `NotesService.plant_flag` — execs a python-socket PUT from inside the container, asserts `OK ` response; raises with a useful diagnostic on failure |
| `gamemgr/game_mgr.py` | `__init__` allocates `FlagStore` and `_service_by_owner` map; `deploy_challenges` populates the latter; new sync `plant_initial_flags()` walks (team, service) pairs and plants; `submit_flag` is now async, returns `SubmitResult`, emits `FLAG_SUBMITTED`; `start()` emits one `FLAG_PLANTED` per planted pair (flag string omitted) |
| `gamemgr/api.py` | `POST /game/flag/submit` awaits the new async `submit_flag` and returns `{accepted, reason}` |
| `main.py` | `start` subcommand calls `game.plant_initial_flags()` after `deploy_challenges()` and before the round loop |
| `tests/test_m5_flag.py` | 6 unit tests (flag shape, FlagStore acceptance classes, replant invalidates, different-victims independence) + 7 integration tests (planting populates store, flag visible inside prod, cross-team pivot steal, ACCEPTED event no-leak, OWN, UNKNOWN, DUPLICATE) |

## Architectural decisions

### One-shot planting per design §3.1 — no per-round rotation in M5

Our `RoundLoop` is DEFENSE/ATTACK two-phase with fixed `total_rounds`. `design_v1.md §3.1` explicitly shows a single `plant_initial_flags` step before the loop starts; rotating flags every round is v1 / FAUST territory, not ours yet. So one flag per (team, challenge) lives for the whole match. The `FlagStore.plant()` API already drops the prior flag from the inverted index on re-plant — that's the only API change M5.5 will need to add per-round rotation.

### `Service.plant_flag` is non-abstract with a `NotImplementedError` default

The alternative — making it `@abstractmethod` — would force every existing and future `Service` to implement it, including control-plane services that don't host flags at all (gateway, telemetry — not yet built but coming). Defaulting to `NotImplementedError` means M5 doesn't touch any code path beyond `NotesService`, and a missing implementation surfaces with a clear, single-line traceback the first time a config tries to plant on a non-participating service.

### Flag string never crosses the event bus

`FLAG_SUBMITTED` payload carries `{accepted, reason, submitter_team_id, victim_team_id, service, phase}` — deliberately no `flag` field. `FLAG_PLANTED` carries only `{service}` (and `team_id` at the event-envelope level). The bus is fanned out to (a) the JSONL file under `runs/<utc>/events.jsonl`, (b) the public `/events/stream` WebSocket consumed by `panel.html`, and (c) `GET /game/events`. Leaking flag strings there would give every observer — including the *attacker* — a free credential dump.

This is asserted by `test_submit_accepted_emits_event` which flatten-greps the entire payload values and refuses to find the flag.

### Stay v2-native, borrow only what plumbs

From `../cyberarena/src/adapters/game_simple/challenge_simple.py`: borrowed the `FLAG{<32 A-Z0-9>}` shape and the notes PUT/LIST/GET pattern (we already had the protocol from M2). Deliberately did **not** borrow:

- `SimpleChecker` three-tuple (`put_flag` / `get_flag` / `havoc`) — serves v1's per-round SLA verification, irrelevant to our DEFENSE/ATTACK split
- `SimpleGameServer._tick_loop` continuous-round flag rotation
- `SimpleGameServer` standalone `ThreadingHTTPServer` for `PUT /flags` — we already have FastAPI on 8000
- `X-Team-Token` header auth — body `team_id` is fine for dev; M5.5 will harden it
- `ServiceScore` / SLA arithmetic — `Scoreboard` is `⬜ stub` in design §5.2 and stays that way until its own milestone

### Attacker pivot model

`design_v1.md` and M3 invariants together force the attacker's only path to be `dev → own_prod (via team_net) → opponent_prod (via game_net, ATTACK only)` — dev is structurally isolated from any opposing container. The integration test honors this by running the steal from `team2_prod`, not `team2_dev`. Future M5.5 + agent work will have the agent exec into its own prod (or run cleanly-scoped tooling there) to perform the real attack; M5 just proves the network + protocol + submit chain end to end.

## Verification

### Full suite

```
$ pytest tests/ -v
tests/test_m1_topology.py::test_topology_parse                                  PASSED
tests/test_m1_topology.py::test_setup_then_teardown                             PASSED
tests/test_m2_gameloop.py::test_config_loads_challenges_and_rounds              PASSED
tests/test_m2_gameloop.py::test_full_lifecycle                                  PASSED
tests/test_m3_network.py::test_default_attached_prod_reachable                  PASSED
tests/test_m3_network.py::test_disconnect_blocks_cross_team_prod                PASSED
tests/test_m3_network.py::test_reconnect_restores_cross_team_prod               PASSED
tests/test_m3_network.py::test_idempotent_disconnect                            PASSED
tests/test_m3_network.py::test_idempotent_connect                               PASSED
tests/test_m3_network.py::test_cross_team_dev_blocked_in_attack                 PASSED
tests/test_m3_network.py::test_cross_team_dev_blocked_in_defense                PASSED
tests/test_m3_network.py::test_intra_team_dev_to_prod_unaffected_by_phase       PASSED
tests/test_m4_agent.py::test_expand_env_vars_substitutes                        PASSED
tests/test_m4_agent.py::test_expand_env_vars_missing_fails_fast                 PASSED
tests/test_m4_agent.py::test_expand_env_vars_cycle_detected                     PASSED
tests/test_m4_agent.py::test_load_game_config_parses_agents                     PASSED
tests/test_m4_agent.py::test_registry_dispatches_sleep_to_claude_manager        PASSED
tests/test_m4_agent.py::test_install_status_kill_lifecycle                      PASSED
tests/test_m4_agent.py::test_install_is_idempotent                              PASSED
tests/test_m4_agent.py::test_live_hello_world                                   PASSED
tests/test_m5_flag.py::test_new_flag_shape                                      PASSED
tests/test_m5_flag.py::test_submit_accepts_then_rejects_duplicate               PASSED
tests/test_m5_flag.py::test_submit_rejects_own_flag                             PASSED
tests/test_m5_flag.py::test_submit_rejects_unknown_flag                         PASSED
tests/test_m5_flag.py::test_replant_invalidates_old_flag                        PASSED
tests/test_m5_flag.py::test_submit_different_victims_independent                PASSED
tests/test_m5_flag.py::test_plant_populates_flagstore                           PASSED
tests/test_m5_flag.py::test_flag_actually_present_on_prod                       PASSED
tests/test_m5_flag.py::test_cross_team_steal_via_prod_pivot                     PASSED
tests/test_m5_flag.py::test_submit_accepted_emits_event                         PASSED
tests/test_m5_flag.py::test_submit_own_flag_rejected                            PASSED
tests/test_m5_flag.py::test_submit_unknown_flag_rejected                        PASSED
tests/test_m5_flag.py::test_submit_duplicate_rejected                           PASSED
33 passed in 129.75s
```

### What each M5 test asserts

| Test | Assertion |
|------|-----------|
| `new_flag_shape` | 20 generated flags all match `FLAG\{[A-Z0-9]{32}\}` |
| `submit_accepts_then_rejects_duplicate` | first foreign-flag submit returns `ACCEPTED`; second identical submit returns `DUPLICATE` |
| `submit_rejects_own_flag` | submitter == owner returns `OWN`, no credit |
| `submit_rejects_unknown_flag` | flag string not in any `(team, service)` row returns `UNKNOWN` |
| `replant_invalidates_old_flag` | calling `FlagStore.plant()` on an existing `(team, service)` makes the old flag `UNKNOWN`; foundation for M5.5 rotation |
| `submit_different_victims_independent` | the same submitter can claim flags from multiple distinct victims |
| `plant_populates_flagstore` | after `plant_initial_flags()`, the store has exactly `{(team1,notes), (team2,notes)}` keys with shape-valid flags |
| `flag_actually_present_on_prod` | reading the notes store inside `team1_prod` returns the same flag that's in the FlagStore — proves the planted flag landed in the right place |
| `cross_team_steal_via_prod_pivot` | from inside `team2_prod` (on `game_net`), socket-stealing from `team1_prod:5000` yields team1's flag — proves M3 network + M5 plant compose correctly |
| `submit_accepted_emits_event` | the awaited `submit_flag` returns the right `SubmitResult`, exactly one `FLAG_SUBMITTED` event lands on the bus, and the flag string does not appear anywhere in the event payload |
| `submit_own_flag_rejected` | end-to-end through `GameMgr.submit_flag`, `team1` submitting its own flag → `OWN` |
| `submit_unknown_flag_rejected` | submitting a syntactically-valid-but-never-planted flag → `UNKNOWN` |
| `submit_duplicate_rejected` | depends on the earlier ACCEPTED test running first; second submit by the same (submitter, victim, service) → `DUPLICATE` |

## Manual demo (end-to-end through HTTP)

```bash
$ python main.py setup
$ python main.py start &
$ sleep 5

# steal team1's flag from inside team2_prod (cross-team via game_net)
$ FLAG=$(docker exec team2_prod python3 -c "
import socket
s = socket.socket(); s.connect(('team1_prod', 5000))
s.sendall(b'GET 0\n')
print(s.recv(128).decode().strip())
")
$ echo $FLAG
FLAG{...32 chars...}

# submit via the public HTTP endpoint
$ curl -s -X POST :8000/game/flag/submit \
    -H 'content-type: application/json' \
    -d "{\"team_id\":\"team2\",\"flag\":\"$FLAG\"}"
{"accepted":true,"reason":"ACCEPTED"}

# event on the bus (flag string omitted)
$ curl -s :8000/game/events | python3 -c "
import json, sys
for e in json.load(sys.stdin):
    if e['type'] == 'FLAG_SUBMITTED':
        print(e['payload'])
"
{'accepted': True, 'reason': 'ACCEPTED', 'submitter_team_id': 'team2',
 'victim_team_id': 'team1', 'service': 'notes', 'phase': 'ATTACK'}
```

## Deferred to later milestones

| Item | Milestone |
|------|-----------|
| Per-round flag rotation (re-plant at start of each DEFENSE) | M5.5 |
| `X-Team-Token` header auth on `/game/flag/submit` (per-team tokens in `game.yaml`) | M5.5 |
| Agent autonomously completing the full attack loop (pivot, steal, submit) | M5.5 (depends on M4.5 reverse channel) |
| `Scoreboard` consumes `FlagStore.submissions` for live scores | Scoreboard milestone |
| `SCORE_UPDATE` event + `/game/scoreboard` endpoint | Scoreboard milestone |
| `Poller` / `Healthcheck` integration (would also tag `FLAG_SUBMITTED` with SLA status) | future |
| Phase-gating submissions (reject during DEFENSE) | not currently in design; M5 just tags `phase` on the event for downstream weighting |
