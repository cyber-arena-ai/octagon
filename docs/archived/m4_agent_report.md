# Milestone 4 — Agent Install & Launch — Execution Report

**Goal:** install and launch an LLM agent CLI (default: Claude Code) inside every team's dev container, authenticated through a third-party Anthropic-compatible router. The agent process must be query-able, killable cleanly with no orphaned children, and survive `orchestrator.teardown` without leaving stragglers.

**Status:** Complete. All M1 + M2 + M3 + M4 tests pass (20 / 20, including a live API smoke that drives a real Claude through the ppapi.ai router to create and execute a hello-world script).

## What was built

| Path | Change |
|------|--------|
| `models/agent.py` | New: `AgentConfig` (cli / model / api_key / base_url / system_prompt), `AgentStatus` enum (NOT_INSTALLED / RUNNING / COMPLETED / TERMINATED / CRASHED), `AgentState`, `AgentLogs` |
| `agents/manager.py` | New: `AgentManager` ABC — `install` / `kill` / `status` / `fetch_logs` |
| `agents/claude.py` | New: `ClaudeAgentManager` — supervisor-bash launch via `setsid`, pid + wrapper-pid + exit_code files under `/var/{lib,log,run}/cyberarena`, child-first SIGTERM + group-sweep kill (avoids reaping race), idempotent re-install |
| `agents/registry.py` | New: cli-flavor dispatch (`claude` → `ClaudeAgentManager`; defaults to Claude for `sleep`-style test overrides) |
| `gamemgr/config.py` | Extended: `load_dotenv()` at config load; recursive `${VAR}` expansion with bounded depth + cycle detection; new `agents: dict[team_id, AgentConfig]` field |
| `gamemgr/events.py` | Added `EventType.AGENT_INSTALLED` |
| `config/game.yaml` | Each team gets an `agent:` block — `cli`, `model`, `api_key`, `base_url` (with `${API_ROUTER_*}` placeholders) |
| `config/docker/Dockerfile.dev` | Added `nodejs npm` apt packages; dev-base now 994 MB (was 665 MB) |
| `.env.example` | New: documents the third-party-router default (`API_ROUTER_BASE` / `API_ROUTER_KEY`), with per-team-key opt-in noted |
| `tests/test_m4_agent.py` | 4 unit tests (env expansion happy / missing / cycle / yaml round-trip) + 1 registry test + 2 integration tests (install→status→kill lifecycle, re-install idempotency) + 1 live smoke (env-gated) |
| `tests/conftest.py` | Loads `.env` before falling back to placeholder env vars, so live tests pick up real router credentials |
| `requirements.txt` | Added `python-dotenv` |

## Architectural decisions

- **Single supervisor pattern, regardless of CLI.** `ClaudeAgentManager` is the only AgentManager implementation. The registry routes `cli: codex …` / `cli: sleep …` to the same class — only the prerequisite-install step (npm install of `@anthropic-ai/claude-code`) is flavor-gated. Adding Codex later is a few lines, not a new adapter.
- **Why supervisor-bash + `setsid`.** Two needs: (1) the launching `docker exec` channel must close immediately so the orchestrator can move on; (2) the agent's exit code must be observable for crash classification. A backgrounded `setsid bash -c '…' </dev/null >/dev/null 2>&1 &` satisfies both — `setsid` makes the wrapper a new session leader (immune to SIGHUP when exec returns) and a new process-group leader (the target of `killpg`).
- **Kill in two steps, not one.** The plan suggested `kill -TERM -- -<pgid>` alone, but that sends SIGTERM to the supervisor bash itself before it can `echo $? > exit_code`. Implementation now: SIGTERM the tracked child first, give the supervisor a ~2 s budget to record `exit_code`, then sweep the whole pgid as a safety net for orphaned helpers (node workers, fs watchers). Production keeps the kill-group guarantee; we also get an observable exit code for the test.
- **Env-var expansion is tree-recursive over yaml, bounded-iterative over substituted strings.** Recursion walks `dict / list / str` nodes. Within a string, substitution loops up to 8 times to allow env values that themselves contain `${...}`, with a `visited` set to detect cycles. Missing vars are fail-fast — a missing key produces a `ValueError("env var X referenced in game.yaml but not set")` at load time, not an empty string that surfaces as a confusing 401 from the provider.
- **Per-config system prompt.** `AgentConfig.system_prompt` lets the smoke test inject the hello-world task without monkeypatching module-level constants; the M5 prompt-builder will populate it from the round / phase / team context.
- **No `Orchestrator` integration yet.** `GameMgr.deploy_agents()` / `teardown_agents()` and the `main.py start` wiring are intentionally deferred (plan tasks 11, 13). M4 proves the agent lifecycle in isolation; M4.5 will wire it into the round loop along with the reverse channel (heartbeat / `deploy_complete`).

## Findings worth recording

### `ANTHROPIC_BASE_URL` is the root, not the `/v1` path
Claude Code appends `/v1/messages` to whatever you set as `ANTHROPIC_BASE_URL`. The ppapi.ai router's `OpenAI`-style listing shows `https://app-us.ppapi.ai/v1` as the "base URL", but that's the OpenAI convention. For Anthropic-compatible clients, the URL must be `https://app-us.ppapi.ai` (no `/v1`) — otherwise Claude Code requests `/v1/v1/messages` and the router returns 404.

This is documented in `.env.example` so the next person doesn't lose 20 minutes to it.

### `claude` headless invocation needs `-p`
`--output-format` (and friends like `--max-budget-usd`, `--include-partial-messages`) are documented as "only works with `--print`". Without `-p`, claude tries to start its interactive TUI, fails on the non-TTY stdin, and exits 1 with empty stderr. The cli in `game.yaml` and the smoke test both use `-p --output-format stream-json --verbose` per the reference repo's pattern.

### Container PID 1 is `sleep infinity`, so wrappers exit as zombies
Dev-base's CMD is `sleep infinity`, which doesn't reap children. The supervisor bash's death therefore leaves a zombie PID-entry that's still "in" its process group, so `kill -0 -<pgid>` always returns 0 after kill. The test probes the tracked child PID instead (`kill -0 <pid>`), which is the actually-meaningful check. A future tini-as-PID-1 change in `Dockerfile.dev` would fix the zombie hygiene if it ever bites us — for M4 it doesn't.

## Verification

### Live smoke against the real router

```
$ pytest tests/test_m4_agent.py::test_live_hello_world -v -s
tests/test_m4_agent.py::test_live_hello_world
── claude exit: AgentState(status=<AgentStatus.COMPLETED: 'COMPLETED'>, pid=79, wrapper_pid=78, exit_code=0)
── /tmp/hello.py contents: "print('hello from claude')\n"
── stderr tail (last 500 chars):
PASSED
1 passed in 56.43s
```

The test:
1. Brings up `team1_dev` from the rebuilt dev-base (nodejs+npm baked in).
2. `ClaudeAgentManager.install()` runs `npm install -g @anthropic-ai/claude-code`, writes the hello-world prompt to `/var/lib/cyberarena/prompt.txt`, exports `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `IS_SANDBOX=1` into `agent.env`, and launches `claude -p --dangerously-skip-permissions --output-format stream-json --verbose --model claude-sonnet-4-6 < prompt.txt > stdout 2> stderr` under the supervisor-bash.
3. Claude routes through `https://app-us.ppapi.ai/v1/messages`, decides to `Write` `/tmp/hello.py`, then `Bash` `python3 /tmp/hello.py`, then exit normally.
4. Test polls until `agent.exit_code` appears (≤ 180 s), asserts `COMPLETED`, asserts `/tmp/hello.py` contains `hello from claude`, and asserts the stream-json transcript echoes that output.

End-to-end wall time: 56 s (npm install ~5 s, claude install + run ~30 s, docker setup/teardown ~20 s). Cost: a few cents at sonnet pricing.

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
20 passed in 120.86s
```

### What each M4 test asserts

| Test | Assertion |
|------|-----------|
| `expand_env_vars_substitutes` | `${FOO}` in dict / list / nested str all expand to env value |
| `expand_env_vars_missing_fails_fast` | unset var raises `ValueError`, mentions the var name |
| `expand_env_vars_cycle_detected` | mutually-referencing env vars raise rather than loop forever |
| `load_game_config_parses_agents` | `teams[].agent:` block round-trips into `cfg.agents[team_id]` with all four scalar fields resolved |
| `registry_dispatches_sleep_to_claude_manager` | `cli: sleep 60` resolves to `ClaudeAgentManager` (since the supervisor mechanics don't care which binary they wrap) |
| `install_status_kill_lifecycle` | install → status RUNNING (pid + wrapper-pid files non-empty, no exit_code yet) → kill → status TERMINATED/COMPLETED (exit_code recorded within 2 s) → tracked child PID is dead (idempotent second kill doesn't raise) |
| `install_is_idempotent` | second install replaces the prior supervisor (new pid different from old) and ends in RUNNING |
| `live_hello_world` (env-gated) | real Claude through the ppapi.ai router actually creates and runs `/tmp/hello.py` and exits 0 |

## Deferred to later milestones

| Item | Milestone |
|------|-----------|
| `GameMgr.deploy_agents()` / `teardown_agents()` + `main.py start` wiring | M4.5 (or rolled into M5) |
| Agent → GameMgr WebSocket reverse channel (`heartbeat`, `deploy_complete`) | M4.5 |
| `last_deploy_time`-driven defense duration | M4.5 |
| Real per-round prompts (M4 uses a placeholder / hello-world) | M5 (prompt-builder) |
| `arena-deploy` in-container CLI command | M4.5 |
| LLM gateway with token / dollar metering | future |
| README update with `.env` setup walkthrough | next docs pass |
| `tini` as PID 1 in dev-base (eliminates zombie wrappers) | nice-to-have |
