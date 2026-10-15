# Milestone 3 — Network Switching Works — Execution Report

**Goal:** `orchestrator.network.set_cross_team_network(allow)` actually flips `game_net` attachment for prod containers. DEFENSE blocks cross-team prod ↔ prod; ATTACK restores it. RoundLoop no longer swallows `NotImplementedError`.

**Status:** Complete. All M1 + M2 + M3 tests pass (12 / 12).

## What was built

| Path | Change |
|------|--------|
| `orchestrator/infra/network.py` | `set_cross_team_network` implemented via `docker network connect / disconnect` on the registered service containers; idempotent (reloads container state, skips if already in target attachment). NetworkManager now takes an optional `graph` and derives the service-container list as `[n.id for n in graph.nodes if "game_net" in n.networks]` — dev containers are excluded by construction |
| `orchestrator/orchestrator.py` | passes `graph` into `NetworkManager(graph=graph)` so it knows which containers to switch |
| `gamemgr/round.py` | removed `try/except NotImplementedError`; phase transitions now hit the real backend and let errors propagate |
| `tests/test_m3_network.py` | 8 integration assertions across cross-team / intra-team / idempotency / structural isolation |
| `docs/m2_gameloop_report.md` | removed the "M3 deferred" row |

## Architectural decisions reaffirmed

- **Only `game_net` is touched per phase**; `team{N}_net` is never disconnected. `set_cross_team_network` walks only containers whose `networks` list includes `game_net` (the two prods). Dev containers are structurally excluded.
- **`docker network` backend, not iptables.** A future iptables backend (no reconnect interruption) could swap in behind the same `set_cross_team_network` interface.
- **TCP reset on disconnect is accepted.** M3 only guarantees reachability policy for new connections; existing TCP connections may be reset when an endpoint is detached. Agent code must treat reset / EPIPE as expected.
- **Cross-team dev ↔ anything is always blocked** — by construction: dev containers are only on their `team_net`, never on `game_net`. Asserted under both ATTACK and DEFENSE.
- **Host port mapping side-channel closed in M2/M3 prep.** Prods bind on `127.0.0.1:8001:5000` / `127.0.0.1:8002:5000` so a dev container cannot reach an opponent prod via the host gateway IP.

## Verification

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
12 passed in 47.26s
```

### What each M3 test asserts

| Test | Assertion |
|------|-----------|
| `default_attached_prod_reachable` | post-setup state has both prods on `game_net`; `team1_prod -> team2_prod:5000` connects in both directions |
| `disconnect_blocks_cross_team_prod` | after `set_cross_team_network(False)`, neither direction connects |
| `reconnect_restores_cross_team_prod` | after toggling back to True, connectivity returns (with a brief poll for the reconnect to settle) |
| `idempotent_disconnect` / `idempotent_connect` | calling the same direction twice does not raise and ends in the expected state |
| `cross_team_dev_blocked_in_attack` | `team1_dev -> team2_prod`, `team2_dev -> team1_prod` fail even in ATTACK (no shared docker network → no DNS resolution) |
| `cross_team_dev_blocked_in_defense` | same, in DEFENSE |
| `intra_team_dev_to_prod_unaffected_by_phase` | `team1_dev -> team1_prod` succeeds under both phases |

The probe (`tests/test_m3_network.py:_can_connect`) is a `docker exec` into the source container running `socket.connect((target, port))` — so it walks through the container's actual network namespace, not the host's.

### Live round loop now drives real switching

The M2 lifecycle test (`test_full_lifecycle`) was unchanged but now exercises the real backend: each `_enter_phase` calls `NetworkManager.set_cross_team_network`, which performs real `docker network connect / disconnect`. The test still passes — proving the round loop is wired end-to-end through real network changes.

## Deferred to later milestones

| Item | Milestone |
|------|-----------|
| `iptables` backend (Linux optimization, no reconnect interruption) | future |
| Static IP assignment so reconnect preserves IPs | future |
| Agent install + `deploy_complete` reporting (drives real defense duration via `last_deploy_time`) | M4 |
| `POST /game/flag/submit` real verification + `FLAG_SUBMITTED` event | M5 |
| Browser panel consuming `/events/stream` | M6 |