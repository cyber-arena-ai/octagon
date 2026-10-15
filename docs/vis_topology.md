# Realtime topology visualizer (`vis/topology.py`)

A standalone, **read-only** view of the arena's network/container topology:
one box per docker network, container nodes inside, edges for multi-homed nodes,
each node labeled with its image + `:port→host (app)` and colored by live status.
Decoupled from the game — run it during setup, mid-match, or while debugging
teardown. It never mutates docker or game state.

## Run

```bash
# live server (self-refreshing, polls every 2s)
python -m vis.topology -c config/game_ico_v3_isolated_async_mixed.yaml
#   -> http://localhost:8050   (needs docker access; e.g. `sg docker -c '…'`)

# one-shot static snapshot (self-contained HTML, data inlined, no server)
python -m vis.topology -c config/<game>.yaml --once
#   -> runs/<latest>/topology.html   (or ./topology.html if no runs/; override with --out PATH)
```

Flags: `-c/--config` (intended-topology overlay), `--host`/`--port` (server),
`--once` + `--out` (static snapshot).

## Data model — hybrid (config ⊕ live docker)

Overlays **live docker state** onto the config's **intended topology**, so nodes
and networks that are in the config but not (yet / any longer) running still
appear, greyed (`absent`). This makes bring-up and teardown observable. With no
`--config` it degrades to live-docker-only (arena nets detected by name).

- Networks from `orchestrator.topology.load(cfg)` + `docker networks.list()`;
  `internal: true` nets are flagged (red dashed border + 🔒 — no egress).
- Nodes from config + any live container attached to a relevant net; live status,
  per-network IP, health, and **published/exposed ports** come from container attrs.
- "What app at which port" = container port bindings + the image name + a small
  `PORT_APP` hint dict in `vis/topology.py` (extend it as configs add services).
- **Namespaced runs (single-host concurrency):** `build_topology(cfg, ns="s{k}")`
  overlays only THIS run's docker resources. Live names are prefixed `{ns}-`
  (see `orchestrator.naming.qualify`) while the config uses bare names, so the ns
  is stripped before matching (and attached-net names mapped back to logical),
  giving an exact overlay for one job even when several run co-located — other
  namespaces' containers are excluded. GameMgr's `/topology` passes its own
  `game.ns`; `ns=""` is unchanged. Without this the overlay would find no live
  containers and degrade to intended-only.

## Rendering

`vis/topology.html` renders the graph with **cytoscape.js** (a graph-viz library,
loaded from CDN — the host browser needs internet; the arena containers stay
isolated). The server exposes `GET /` (page) and `GET /api/topology` (JSON); the
page re-runs layout only when the node/edge set changes, otherwise refreshes
status/labels in place. `--once` inlines the JSON as `window.__SNAPSHOT__` so the
file renders once with no server.

## Why standalone (not bolted onto the GameMgr panel on :8000)

It works regardless of game state and touches nothing in `gamemgr/`. For an
embedded-in-`/panel`, push-via-the-event-WS variant, see the alternatives weighed
when this was built; the standalone server was chosen for decoupling.
