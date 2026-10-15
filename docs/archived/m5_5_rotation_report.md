# Milestone 5.5 — Per-Round Rotation + Per-Team Token Auth — Execution Report

**Goal:** finish the two M5-deferred items that don't depend on missing prerequisites — re-plant flags at the start of every DEFENSE phase (so attacks scale across rounds), and require `X-Team-Token` on `POST /game/flag/submit` (so body `team_id` is no longer trustable). Agent-driven attack remains deferred; it needs a dev→own_prod pivot protocol that the design hasn't specified.

**Status:** Complete. All tests pass (41 / 41, including M1–M5 + M5.5 + the M4 live Claude smoke).

## What was built

| Path | Change |
|------|--------|
| `models/topology.py` | `Team.token: str = ""` added (default empty for backward compat with M1–M5 tests / unconfigured deployments) |
| `gamemgr/config.py` | `_from_dict` reads `teams[i].token` (falls back to `""`); `Team` round-trips through `${VAR}` expansion so production matches can keep tokens in `.env` |
| `config/game.yaml` | Each team gets `token: dev-teamN-token` plus a comment pointing at `.env`-based override for real matches |
| `gamemgr/flag.py` | `FlagStore.submissions` re-keyed from `set[(submitter, victim, service)]` (once-per-game) to `dict[submitter, set[flag_str]]` (once-per-flag — naturally allows re-attack each round since rotation issues a new flag string) |
| `gamemgr/game_mgr.py` | New `plant_flags(round_id)`; `plant_initial_flags()` is now a thin alias for `plant_flags(0)`. `start()` subscribes `_on_rotation_event` to the bus — fires on `PHASE_CHANGE → DEFENSE`, re-plants, emits one `FLAG_PLANTED` per (team, service) with the right `round_id`. Idempotency guard via `_last_planted_round` so a re-fired event can't double-plant. `_token_to_team` dict built from config at construction for O(1) HTTP token lookup |
| `gamemgr/api.py` | `POST /game/flag/submit` reads `X-Team-Token` header (FastAPI `Header` dependency); missing or unknown → `{accepted: false, reason: "BAD_TOKEN"}` with **no event emitted** (event stream would otherwise become a token-probing oracle). Body `team_id` is ignored entirely |
| `tests/test_m5_5_rotation.py` | 8 tests: 2 config round-trip + 1 unit-level rotation + 1 round-loop integration (asserts 6 `FLAG_PLANTED` = pre-game ×2 + rounds 1 & 2 ×2 each) + 4 HTTP auth (valid / wrong / missing token, body `team_id` ignored) |

## Architectural decisions

### Why submissions are now keyed on flag string, not on (submitter, victim, service)

The original M5 key forced "team2 can steal team1's notes at most once per *game*." With per-round rotation that semantics is wrong — each round issues a fresh flag, and a fresh flag should be claimable. Two ways to fix:

A. Add `round_id` to the key: `(submitter, victim, service, round_id)`. Means `plant()` must record what round a flag belongs to, which couples `FlagStore` to the round model.
B. Key on the flag string itself per submitter: `dict[submitter, set[flag_str]]`. Rotation drops the old flag from `by_flag`, so a retry of the old string returns `UNKNOWN` *before* the `DUPLICATE` check ever fires. A new round's flag has a new string — not in `submissions[submitter]` → ACCEPTED.

Option B is what landed: it keeps `FlagStore` ignorant of the round concept (single-responsibility), the UNKNOWN-before-DUPLICATE ordering makes the semantics correct without explicit cleanup, and multi-team attack against the same victim+service still works (different submitter dict entries).

`test_submit_different_victims_independent` and the M5 DUPLICATE / OWN / UNKNOWN cases all still pass — the new keying is a strict superset of the old behavior modulo the (correct) loosening for rotated flags.

### Rotation as a bus subscriber, not a `RoundLoop` callback

`RoundLoop` only knows about the bus and the network. Adding a `flag_rotator` callback to its constructor would force every test that constructs `RoundLoop` directly to also wire flags. Subscribing `GameMgr._on_rotation_event` to `bus` keeps `RoundLoop` ignorant of flag mechanics and matches the existing `FileSink` / WebSocket subscriber pattern.

`EventBus.publish` awaits subscribers sequentially, so the rotation handler completes (including the docker `exec_run` for `plant_flag`) before `RoundLoop._enter_phase` returns and the DEFENSE timer starts ticking. No race.

### BAD_TOKEN must not emit a FLAG_SUBMITTED event

The event bus fans out to a public WebSocket (`/events/stream`) and JSONL log. If every failed auth attempt published an event, an attacker observing the stream could:

1. Probe whether a guessed token is valid by counting BAD_TOKEN events (no event → valid)
2. Time-correlate their own probes against the event stream to confirm delivery
3. Use the event stream as a covert command channel between two parties who both have read access

Easier to just not emit. The API returns the JSON status to the original submitter and that's it. Uvicorn's access log still records 200-with-BAD_TOKEN bodies for operators who care.

### `plant_initial_flags()` kept as an alias for `plant_flags(0)`

The original M5 entry point (called from `main.py` *before* `start()`) had no `round_id` concept. Removing it would break M5 tests that invoke it directly without driving the round loop. Keeping it as a one-line wrapper is zero cost and lets pre-game + per-round share a single implementation.

The brief window between pre-game plant (round_id=0) and round-1 DEFENSE plant (round_id=1) — a few milliseconds in practice — does produce a "ghost" generation of flags. Harmless: nobody can submit them in that window because the round loop hasn't started yet, and rotation immediately replaces them.

### Why agent-driven attack is still deferred

Per design `§6.2`, the agent runs in `dev`. Per M3, `dev` has no network path to opposing `prod`. The only working attack path is `dev → own_prod (team_net) → opposing_prod (game_net)` — which requires a pivot mechanism from `dev` into `own_prod`. Three options:

1. SSH from dev to own prod (needs sshd config + key/password provisioning in prod-base)
2. A small RPC proxy on own prod that dev talks to (needs a new daemon + protocol)
3. Co-locate agent+service on a single container (changes the security boundary in §6.2)

Each is a non-trivial milestone of its own and `design_v1.md` hasn't specified which path to take. M5.5 ships the surface area the agent will need (per-team token) so that once the pivot decision lands, agent-to-API auth is already in place.

## Verification

### Full suite

```
$ pytest tests/ -v
... (41 tests) ...
======================== 41 passed in 235.17s (0:03:55) ========================
```

### What each M5.5 test asserts

| Test | Assertion |
|------|-----------|
| `test_team_token_round_trips_through_yaml` | `Team.token` parses from yaml; missing field defaults to `""` |
| `test_game_yaml_carries_real_tokens` | committed `config/game.yaml` has both teams' tokens populated |
| `test_plant_flags_rotates` | calling `plant_flags(round_id=99)` on a primed FlagStore: every (team, service) gets a new flag string, every old flag becomes `UNKNOWN`, every new flag is `ACCEPTED` by another team |
| `test_round_loop_triggers_rotation_via_phase_change_subscriber` | full async round loop driven through 2 rounds; total of 6 `FLAG_PLANTED` events emitted (one per (team, service) at round_id ∈ {0, 1, 2}); each round has both teams' plants present |
| `test_submit_with_valid_token_accepted` | `X-Team-Token: dev-team2-token` + team1's flag → `{accepted: true, reason: "ACCEPTED"}` |
| `test_submit_with_wrong_token_rejected` | bad token → `BAD_TOKEN`; **no** new `FLAG_SUBMITTED` event lands on `game.bus` |
| `test_submit_without_token_rejected` | no header → `BAD_TOKEN` |
| `test_submit_body_team_id_is_ignored` | body `{team_id: "team1"}` + header `dev-team2-token` → derived submitter is team2, not team1; submit succeeds (would be OWN if body were honored) |

### Manual demo (rotation + auth end-to-end)

```bash
$ python main.py setup
$ python main.py start &
$ sleep 5

# steal team1's CURRENT flag from team2_prod
$ FLAG=$(docker exec team2_prod python3 -c "
import socket
s = socket.socket(); s.connect(('team1_prod', 5000))
s.sendall(b'GET 0\n')
print(s.recv(128).decode().strip())
")

# submit WITH token → ACCEPTED
$ curl -s -X POST :8000/game/flag/submit \
    -H 'X-Team-Token: dev-team2-token' \
    -H 'content-type: application/json' \
    -d "{\"flag\":\"$FLAG\"}"
{"accepted":true,"reason":"ACCEPTED"}

# same flag, WITHOUT token → BAD_TOKEN
$ curl -s -X POST :8000/game/flag/submit \
    -H 'content-type: application/json' \
    -d "{\"flag\":\"$FLAG\"}"
{"accepted":false,"reason":"BAD_TOKEN"}

# wait for next DEFENSE (rotation happens) — flag rotates, old becomes UNKNOWN
$ sleep 12
$ curl -s -X POST :8000/game/flag/submit \
    -H 'X-Team-Token: dev-team2-token' \
    -d "{\"flag\":\"$FLAG\"}"
{"accepted":false,"reason":"UNKNOWN"}

# steal the NEW flag and submit — ACCEPTED again (different flag string, same victim+service)
$ NEW_FLAG=$(docker exec team2_prod python3 -c "
import socket
s = socket.socket(); s.connect(('team1_prod', 5000))
s.sendall(b'LIST\n')
n = int(s.recv(64).decode().split()[1]); s.close()
s = socket.socket(); s.connect(('team1_prod', 5000))
s.sendall(f'GET {n-1}\n'.encode())
print(s.recv(128).decode().strip())
")
$ curl -s -X POST :8000/game/flag/submit \
    -H 'X-Team-Token: dev-team2-token' \
    -d "{\"flag\":\"$NEW_FLAG\"}"
{"accepted":true,"reason":"ACCEPTED"}
```

## Deferred to later milestones

| Item | Milestone |
|------|-----------|
| Agent autonomously running the attack loop (pivot, steal, submit) | needs dev→own_prod pivot protocol decision; then agent-attack milestone |
| Agent receives its own `team.token` in `agent.env` via `ClaudeAgentManager` | rolls into the agent-attack milestone |
| `Scoreboard` consumes `FlagStore.submissions` and the multi-round time series | Scoreboard milestone |
| `SCORE_UPDATE` event + `/game/scoreboard` endpoint | Scoreboard milestone |
| Multi-round flag lifetime (FAUST-style: keep flags valid for K rounds before expiry) | not in current design; add only if a Scoreboard milestone requests it |
| Phase-gated submissions (reject during DEFENSE) | not in design; current behavior tags `phase` on the event and lets consumers weight it |
| M4.5 reverse channel (heartbeat / deploy_complete / `last_deploy_time`-driven defense duration / `arena-deploy` CLI) | independent milestone |
