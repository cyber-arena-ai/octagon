# Milestone 6 — Observation Panel — Execution Report

**Goal:** single-file `panel/panel.html` served at `GET /panel`, vanilla JS, no build step, renders live `round` / `phase` / per-team event timeline from the existing event bus. Verified with two screenshots from a real M5.6-style attack (real Claude through ppapi.ai router).

**Status:** Complete. All 46 tests pass (incl. M4 hello-world live + M5.6 live attack + 4 new M6 panel smokes). Live demo captured the panel after `plant_initial_flags()` and after the agent landed an `ACCEPTED` submission.

## Screenshots

### After plant (panel just loaded, agent not yet installed)

![panel after plant](screenshots/m5_6_attack/01_after_plant.png)

- Header: `Cyber Arena — Live Panel`, round `0 / 3`, phase `INIT`
- Two team lanes show `▸ deploy notes:5000` and `🚩 plant notes r0` chips
- Empty stats (`0 accepted · 0 rejected`)
- Footer: scoreboard placeholder; "connecting…" because chrome's render budget ended before the WS handshake completed (cosmetic — the chips already came in via the HTTP backfill `GET /game/events`)

### After live attack — both agents running (real Claude on both teams)

![panel after attack](screenshots/m5_6_attack/02_after_attack.png)

Symmetric attack: both teams install attack agents, both succeed in stealing
the opposing flag.

- **team1**: `◆ agent installed +32.0s`, **`✓ submit ACCEPTED ← team2/notes +47.0s`** (green), `✗ submit DUPLICATE ← team2/notes +63.4s` (red retry) — stats `1 accepted · 1 rejected`
- **team2**: `◆ agent installed +37.7s`, **`✓ submit ACCEPTED ← team1/notes +52.1s`** (green), then four `✗ DUPLICATE` retries at +61.5, +66.7, +72.0, +77.2s (without rotation enabled, the second submit always fails — visible as a row of red chips) — stats `1 accepted · 4 rejected`
- Footer dot has gone green (`connected`) — the WS handshake completed during the longer wait window
- Both agents landed `ACCEPTED` within **15 s** of being installed; both running entirely independently of each other and of the orchestrator

Both screenshots live in `docs/screenshots/m5_6_attack/`, version-controlled so reports stay self-contained.

## What was built

| Path | Change |
|------|--------|
| `panel/panel.html` | Single file (~330 lines incl. CSS): header / phase-timeline strip / per-team lane rows / footer. Vanilla JS, dark theme borrowed from v1 `vis/topology.py` (`#0f0f1a` bg, JetBrains Mono + Inter, purple/yellow/teal/red palette). WS to `/events/stream` with exponential-backoff reconnect; HTTP backfill via `GET /game/events` on load so a freshly-opened panel isn't blank |
| `gamemgr/api.py` | New `GET /panel` route serves `panel/panel.html` via `FileResponse`. Same-origin with the WS endpoint → no CORS / mixed-content edge cases |
| `gamemgr/game_mgr.py` | Factored `emit_setup_events()` out of `start()`. Same per-`SERVICE_DEPLOYED` + per-`FLAG_PLANTED(round_id=0)` emission, now callable from demos that don't drive the round loop. `start()` just `await`s it |
| `tests/test_m6_panel.py` | Four smokes: file-existence + required hooks, `GET /panel` returns the HTML, `GET /game/events` returns a JSON array, real `google-chrome --headless` renders the served page into a non-trivial PNG |
| `scripts/m6_screenshot_demo.py` | Live orchestrated demo: bring up arena + uvicorn + plant, screenshot, install the team2 attack agent (M5.6 pipeline), poll until ≥1 `ACCEPTED`, screenshot. Writes to `docs/screenshots/m5_6_attack/` |
| `docs/screenshots/m5_6_attack/{01_after_plant,02_after_attack}.png` | Captured artifacts referenced from this report |

## Architectural decisions

### Single HTML file, no framework, no CDN

The panel is ~330 lines including CSS. Pulling in React/Vue/D3 would mean a build step, which means more ways for the panel to break at the worst possible moment (live demo). Vanilla JS + a single `<script>` block is enough for the layout the design called for. The whole file is checked into the repo and travels with the commit; no network round-trip on load.

### Served from `GET /panel`, not `file://`

The WS endpoint lives on `:8000`. If the panel were loaded with `file://`, the browser would treat it as origin `null` and the mixed origin/protocol rules around `ws://localhost:8000/...` are inconsistent across browser releases. Same-origin serving is five lines (`FileResponse(PANEL_HTML_PATH)`) and sidesteps the whole class of issues.

### Visual style borrowed from v1 `vis/topology.py`

v1's diagram tool was made for the same product, by the same person, with the same audience. Copying its palette (purple accent, yellow team1, teal team2, red attack, JetBrains Mono / Inter fonts) means tools across both repos look like one family — and it's a calibrated palette that already reads well on dark backgrounds.

### `emit_setup_events()` extracted from `start()`

The very first demo run had a beautiful empty panel: `SERVICE_DEPLOYED` and `FLAG_PLANTED` events are only emitted when `start()` runs, but the screenshot demo never calls `start()` (the round loop would burn the agent's attack window). Pulling the emission into `emit_setup_events()` keeps `start()`'s behavior identical while making the events available for any caller that wants them — demos, tests, future scripts.

### `google-chrome --headless`, not playwright

`/usr/bin/google-chrome` was already on the host. `google-chrome --headless --screenshot=PATH --window-size=W,H URL` is one line; playwright adds a ~100 MB browser download plus a Python API to learn. For "take three PNGs" that's overkill.

### `--virtual-time-budget=8000` (not 3000)

First run used 3000 ms and the WS handshake hadn't completed when chrome ended the render — though the chips drew because the HTTP backfill *had* finished. Bumping to 8000 gave the WS time to connect for the second screenshot. Still cosmetic: the actual data layer is the HTTP backfill, the WS is for new events arriving after load.

### `AGENT_INSTALLED` emitted from the demo, not from `ClaudeAgentManager`

The `AGENT_INSTALLED` event type has been in the enum since M4, but `ClaudeAgentManager.install()` doesn't actually publish it — the manager has no reference to the bus and was kept independent of `GameMgr`. The demo emits the event manually after `install()` returns. M4.5 (reverse channel) is the natural place to wire this in for real — that milestone gives the agent a way to call back, and at the same time `GameMgr.deploy_agents()` becomes the canonical install-and-announce path.

## Verification

### Smoke suite (no API spend)

```
$ pytest tests/test_m6_panel.py -v
tests/test_m6_panel.py::test_panel_html_exists                       PASSED
tests/test_m6_panel.py::test_panel_route_serves_html                 PASSED
tests/test_m6_panel.py::test_events_route_is_json_array              PASSED
tests/test_m6_panel.py::test_panel_renders_in_headless_chrome        PASSED
4 passed in 33.40s
```

### Live demo (real Claude on both teams, ~20-60¢)

```
$ python scripts/m6_screenshot_demo.py
  📸 screenshot after plant -> docs/screenshots/m5_6_attack/01_after_plant.png
  🤖 installing attack agent on team1_dev …
  🤖 installing attack agent on team2_dev …
  ...waiting (~0s elapsed, accepted_by=[])
  ✅ both teams landed ACCEPTED after 15s
  📸 screenshot after attack -> docs/screenshots/m5_6_attack/02_after_attack.png
```

Both agents installed concurrently, each pivoting through its own prod and
attacking the opposing team. **15 seconds** total from second `install()`
return to the second `ACCEPTED` landing — agents are completely independent
of each other.

### Full regression

```
$ pytest tests/ -v
... (46 tests, all green) ...
46 passed in 210.68s
```

Distribution: M1×2, M2×2, M3×8, M4×8 (incl. live hello-world), M5×13, M5.5×8, M5.6×1 (live attack), M6×4.

## Deferred to later milestones

| Item | Milestone |
|------|-----------|
| `AGENT_INSTALLED` emitted by `ClaudeAgentManager` directly | M4.5 (reverse channel) |
| Live agent connected/disconnected dot in footer | M4.5 (depends on heartbeat) |
| Scoreboard table with per-team running score | Scoreboard milestone |
| `SCORE_UPDATE` event handler in panel | Scoreboard milestone |
| Service health badge per team row (`SERVICE_UP` / `SERVICE_DOWN`) | depends on Poller, which design §5.2 still has as ⬜ stub |
| File-based replay mode (load `runs/<ts>/events.jsonl` and replay with a time scrubber) | future tooling |
| Phase-timeline bands (only present once `start()` is called — demo's screenshot shows the strip empty because the round loop wasn't driven) | not a milestone deliverable; show up automatically in real game runs |
