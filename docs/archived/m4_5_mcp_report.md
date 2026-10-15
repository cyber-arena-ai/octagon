# Milestone 4.5 — MCP Agent Channel — Execution Report

**Goal:** swap the agent ↔ host reverse channel from curl-REST to an
**MCP server** mounted on FastAPI; land the deferred reverse channel
in the same change (`AGENT_INSTALLED` / `AGENT_LAUNCHED` host emits,
`AGENT_READY` and `DEPLOY_COMPLETE` from MCP tools). No agent-side WS.

**Status:** Complete. 16 new tests pass + 1 env-gated Claude CLI smoke
skipped without an API key. All 63 existing tests still collect; the
26 docker-independent tests all pass.

## What was built

| Path | Change |
|------|--------|
| `requirements.txt` | pinned `mcp==1.27.1` |
| `gamemgr/events.py` | added `AGENT_LAUNCHED` / `AGENT_READY` / `DEPLOY_COMPLETE`; `AGENT_INSTALLED` now means "prep done, **not** launched" |
| `gamemgr/mcp_server.py` (new) | `FastMCP("cyberarena")` with three tools; `MCP_MOUNT_PATH` + `MCP_PUBLIC_ENDPOINT` constants; per-tool `X-Team-Token` auth returning generic `Unauthorized` |
| `gamemgr/api.py` | FastAPI lifespan runs `mcp.session_manager.run()` (without it tools 500); mount + startup log |
| `gamemgr/game_mgr.py` | `deploy_reports`, `is_known_service()`, four async `record_*` methods, `agent_config_for()` uses `canonical_endpoint_url()` |
| `models/agent.py` | `AgentConfig.mcp_endpoint: str = ""` (empty = rollback path, no behavior change for non-MCP tests) |
| `agents/claude.py` | `install()` accepts `on_installed` / `on_launched` callbacks at the prep/launch boundary; writes `mcp.json` + chmod 600 when MCP enabled; adds `--mcp-config` to cli |
| `agents/prompts.py` | attack prompt switches MCP vs curl based on `cfg.mcp_endpoint`; curl one-liner kept as fallback |
| `tests/test_m4_5_mcp_server.py` | 13 tests: mount-path probe, 3 tools, unknown-service rejection, parametrized bad-token oracle hardening, 4 MCP↔REST parity outcomes, both-silent on bad-token |
| `tests/test_m4_5_install_emits.py` | 3 tests against a mock docker client: ordering, fault-injection (only INSTALLED emits when `_launch` raises), empty-team_id skip |
| `tests/test_m4_5_claude_mcp_smoke.py` | env-gated live: `subprocess.run(["claude", "-p", "--mcp-config", ...])` against in-process uvicorn; asserts `AGENT_READY` lands |
| `tests/test_m5_6_agent_attack.py` | wired callbacks (sync→async via `asyncio.run`); added `AGENT_INSTALLED → AGENT_LAUNCHED` ordering assertion |
| `docs/design_v1.6.md` | event table + deferred table updated; added concise §14 linking back to the plan |

## Architectural decisions

- **MCP, not WS — and agent-side WS removed entirely.** Claude Code speaks MCP natively; layering a WS daemon would be a translation hop with no benefit. The "no agent-side WS, ever" stance is a hard rule, not a deferred choice.
- **Three-event lifecycle (`INSTALLED` → `LAUNCHED` → `READY`)** instead of one. Mid-install failures are now observable from the event stream alone — fault-injection test pins this.
- **URL constants over auto-detection.** `MCP_MOUNT_PATH` and `MCP_PUBLIC_ENDPOINT` are explicit; `FastMCP.streamable_http_path` set to `"/"` so the wire URL is exactly `<MCP_PUBLIC_ENDPOINT>`. If a future SDK moves its internal path, edit one constant and re-run the Claude CLI smoke.
- **Token in `mcp.json` headers, not as a tool arg.** LLM never sees the token in any tool surface — defends against stream-json transcript leaks.
- **Bad-token asymmetry between MCP and REST is intentional.** MCP returns generic `Unauthorized`; REST keeps `BAD_TOKEN`. The parity invariant that holds: **neither emits `FLAG_SUBMITTED`** (the event stream as token oracle was the real hazard).
- **FastAPI lifespan dance is required.** Without `async with mcp.session_manager.run()` in the outer app's lifespan, the sub-app starts but every tool call 500s with "Task group is not initialized". Documented in `build_app()`.
- **Sync `install()` + sync callbacks + async `record_*`.** Caller bridges with `asyncio.run` (test) or `run_coroutine_threadsafe` (prod main.py). Avoids async-ifying install() and its many call sites.

## Verification

```
$ pytest tests/test_m4_5_mcp_server.py tests/test_m4_5_install_emits.py \
         tests/test_m4_5_claude_mcp_smoke.py -v
...
16 passed, 1 skipped in 7.65s

$ pytest tests/ --collect-only -q | tail -1
63 tests collected in 0.32s
```

The 1 skipped is the live Claude CLI smoke (no API key in sandbox).
Docker-dependent tests (M1/M2/M3/M4 integration, M5 cross-team, M5.5
rotation, M5.6 attack, M6 panel) collect cleanly but weren't run here
— no docker daemon. None of the M4.5 changes touch those code paths.

## Deviations from the plan

- **MCP wire error code `-32001` doesn't surface.** FastMCP wraps server-raised `McpError` from inside a tool body as an `isError=true` text content result, not as a JSON-RPC error code. The plan said wire response carries `code=-32001`; reality is `isError=True` + `"Unauthorized"` in the text. The test pins the *message surface* (no `BAD_TOKEN` / `token` / team-id leaks) instead of the code. Oracle-hardening invariant still holds.
- **Raw httpx parity test downgraded to a health probe.** The plan flagged raw httpx as optional sanity; in practice MCP Streamable HTTP's session handshake is not worth reimplementing. Wire compatibility is fully owned by `test_m4_5_claude_mcp_smoke.py`.

## Deferred

| Item | Owning milestone |
|------|------------------|
| Adaptive defense duration (reads `deploy_reports[*][*]["duration_ms"]`) | Adaptive duration milestone |
| `AGENT_CONNECTED` / `AGENT_DISCONNECTED` from MCP session lifecycle | Panel-liveness milestone |
| Heartbeat tool / panel grey-dot | Same |
| MCP resources (read-only game state) | Only if a use case shows up |
| Multi-flavor CLI (codex / gpt) | `AgentManager` subclass writes its own client config |
| `arena-deploy` in-container CLI calling `report_deploy_complete` | Defense workflow milestone |
| Tighten REST `/game/flag/submit` to also return generic `Unauthorized` | Out of scope — asymmetry is intentional |
