# Persistent-Memory Agent — Plan v1

## What we're building

**Install once per team. Per phase, the orchestrator runs
`claude -p --continue` on the agent host through a pluggable
`AgentTransport`. Memory carries across phases via Claude CLI's
session store on disk.**

No in-container daemon. No file IPC for control signals. The
orchestrator is the controller; Claude is a short-lived process
spawned once per phase.

> **The persistent component is Claude CLI's session store on disk,
> not any process.** Claude exits between phases. `--continue`
> reloads the session under
> `/root/.claude/projects/<workdir-cwd>/<id>.jsonl` on the next
> invocation.

> **Assumption — Claude CLI session selection is cwd-scoped.** We
> pin every invocation to `/tmp/agent/workdir/` so `--continue` only
> sees this arena's sessions. If a future Claude release changes the
> cwd → session mapping, switch to explicit `--resume <id>`.

---

## Architecture

```mermaid
graph TD
  subgraph ORCH["Host orchestrator"]
    Mgr(["ClaudeAgentManager"])
    subgraph TX["AgentTransport · pluggable"]
      Docker["DockerExecBackend<br/>(v1)"]:::active
      SSH["SSHBackend<br/>(deferred)"]:::stub
    end
    Mgr --> TX
  end

  subgraph HOST["Agent host · container today · VM tomorrow"]
    direction TB
    Static["/tmp/agent/<br/>agent.env · mcp.json · workdir/<br/><i>static · install once</i>"]
    Phase["/tmp/agent/<br/>prompt.txt · phase_context.json<br/><i>overwritten per phase</i>"]
    Session["~/.claude/projects/&lt;workdir-cwd&gt;/&lt;id&gt;.jsonl<br/><i>Claude session store · grows turn-by-turn</i>"]
  end

  TX -->|run_command · write_file · kill_process| HOST

classDef stub stroke-dasharray:4 4,opacity:0.5
classDef active stroke-width:2px
```

**Per round, sync mode** — each phase is one `run_phase` invocation:

| Phase | Action |
|---|---|
| R1 DEFENSE | stage prompt → `claude -p --continue` → artifacts |
| R1 ATTACK | same |
| R2 DEFENSE | same |
| … | … |

Each invocation is a fresh process; `--continue` makes it one logical
conversation.

### Why no in-container daemon

An earlier draft put a Python daemon (`ClaudeAgentRunner`) inside the
container, fed by file IPC (`cue`, `kill`, `shutdown`, `done.json`).
Redundant in both modes:

| Daemon's job | Where it actually lives |
|---|---|
| Stable PID across phases | Container/host IS the stable identity |
| Cue / done file IPC | `transport.run_command` returns directly |
| SIGTERM → SIGKILL on timeout | Inside the transport's `run_command` |
| Atomic `done.json` | Result is in memory on the host |
| Transcript append | Host writes artifacts directly |
| `--continue` memory | Works regardless of invoker |

In **sync mode** the orchestrator controls phases. In **async mode**
the agent self-controls via MCP. A daemon between them has nothing to
decide. Drop permanently.

---

## Two abstractions × one runner

The agent layer has two orthogonal interfaces plus a tiny free
function that composes them. Keeping them separate means we can swap
CLIs (Claude / Codex / Gemini / …) without touching the host
transport, and swap host transports (docker → SSH) without touching
CLI-specific install + invocation logic.

### `AgentManager` (ABC) — data-only CLI recipe

CLI-flavor-specific. One concrete manager per agent CLI. The contract
is intentionally minimal — just **what the CLI is + how to invoke it**:

| Member | Kind | Purpose |
|---|---|---|
| `PROMPT_PATH` | class constant | where the per-phase prompt is staged on the agent host |
| `PHASE_KILL_PATTERN` | class constant | `pkill -f <pattern>` used after a `run_command` timeout |
| `install(container, cfg, *, on_installed=None)` | method | one-shot CLI install + SSH pivot + env / mcp.json. No process launched. |
| `phase_command(*, model=None) → list[str]` | method | shell argv that invokes the CLI for one phase, reading prompt from `PROMPT_PATH`, resuming the prior session via the CLI's own `--continue` / `--resume` flag |

Adding a new CLI = subclass with these two methods + two constants,
~50–80 LOC. `status` / `kill` / `fetch_logs` from the M4-era
observability surface are NOT on the ABC — concrete subclasses can
keep them for legacy live-smoke tests, but new CLIs need not implement.

### `agents.runner.run_phase_via_transport` — the 3-line dance

One async helper that composes a manager + a transport: write prompt,
run the CLI with timeout, pkill on timeout. Returns the raw
`CommandResult`. **Caller** persists artifacts and emits events
(`GameMgr.record_phase_done` is the canonical caller).

This is a free function, not a method — splitting it out keeps
`AgentManager` data-only and prevents the "method that does five
different things" smell.

### `AgentTransport` (Protocol) — host execution

Host-flavor-specific. One concrete backend per host kind:

| Backend | Status | Handle |
|---|---|---|
| `DockerExecBackend` | v1 | container name |
| `SSHBackend` | deferred | host / user / key |

Surface: `connect`, `run_command` (async; SIGTERM → SIGKILL on timeout),
`write_file`, `read_file`, `kill_process`. `AgentHandle` is opaque to
the manager; the transport interprets it.

The split means adding a new CLI = one new `AgentManager` subclass;
adding a new host = one new `AgentTransport`. Neither change touches
the other axis.

### Provider dispatch

`AgentConfig.provider` (from `game.yaml`'s `agent.provider:`) picks
the right `AgentManager` subclass via `agents.registry.build(cfg)`.
Falls back to the first token of `cfg.cli` when `provider` is unset
(M4-era yaml back-compat). Future CLIs (codex / gemini / openai_compat)
register by adding one line to `agents.registry.REGISTRY`.

---

## Lifecycle

### 1. Game start — `install()` once per team

1. `transport.run_command(["npm", "install", "-g", "@anthropic-ai/claude-code"])`
2. `_provision_ssh_pivot` (existing M5.6 SSH key push, now via transport)
3. `transport.write_file` → `/tmp/agent/mcp.json` and `agent.env`
4. `transport.run_command(["mkdir", "-p", "/tmp/agent/workdir"])`
5. Emit `AGENT_INSTALLED`

No process launches here. The next phase's `run_phase` is what first
starts Claude.

### 2. Each phase — `run_phase(...) → DoneInfo`

The kickoff subscriber awaits one `run_phase` per (team, phase):

1. Stage `prompt.txt` + `phase_context.json` via the transport.
2. Emit `AGENT_PROMPT_SENT`.
3. Run `claude -p --continue` on the agent host through the transport,
   bounded by the phase timer.
4. On timeout: belt-and-suspenders `pkill` inside the agent host
   (defends against `docker exec` swallowing the SIGTERM).
5. Persist `stream-{phase}.jsonl` / `agent-{phase}.log` /
   `metadata-{phase}.json` to the host artifact dir.
6. Emit `AGENT_COMPLETED` / `AGENT_TIMEOUT` / `AGENT_FAILED`.

### 3. Game end

Nothing to tear down inside the agent host. `Orchestrator.teardown()`
reaps the dev containers as today; the session store dies with them.

---

## What `run_phase_via_transport` returns

The runner returns `AgentTransport.CommandResult` verbatim — no
wrapping dataclass. Caller reads:

| Field | Meaning |
|---|---|
| `exit_code` | CLI process exit (signal-exits non-zero on timeout) |
| `timed_out` | `true` iff the transport had to escalate SIGTERM → SIGKILL |
| `stdout`, `stderr` | raw bytes captured from the CLI process |

`GameMgr.record_phase_done(team_id, round_id, phase, prompt, result,
started_at, ended_at)` reads these, writes the three sibling artifact
files (`stream-{phase}.jsonl` / `agent-{phase}.log` /
`metadata-{phase}.json`), and emits one of `AGENT_COMPLETED` /
`AGENT_TIMEOUT` / `AGENT_FAILED` based on `result.timed_out` +
`result.exit_code`.

Behavior the runner guarantees:
- Honors `timeout` and escalates SIGTERM → SIGKILL on expiry; sets
  `timed_out=True`.
- Captures stdout/stderr bytes even on timeout (drain-into-buffer
  tasks complete only when proc EOFs).
- Never raises on agent-side failures — surfaces them via the result.

Subprocess management, signal escalation, and transport-specific
quirks live entirely behind `AgentTransport`. Unit-test the runner
against an in-memory `FakeTransport`.

---

## File layout

In the agent host (written by transport; read by Claude or for
provenance):

| Path | Lifecycle |
|---|---|
| `/tmp/agent/agent.env`, `mcp.json` | static; install once |
| `/tmp/agent/workdir/` | static dir; claude's cwd for `--continue` isolation |
| `/tmp/agent/prompt.txt` | overwritten per phase; Claude reads via stdin redirect |
| `/tmp/agent/phase_context.json` | overwritten per phase; provenance only |
| `/root/.claude/projects/<workdir-cwd>/<id>.jsonl` | Claude CLI session store; grows turn-by-turn |

On the orchestrator host, per phase (written by `run_phase`):

| Artifact path | What |
|---|---|
| `runs/<ts>/artifacts/team{N}/round-{N}/stream-{phase}.jsonl` | Claude stdout (stream-json transcript) |
| `runs/<ts>/artifacts/team{N}/round-{N}/agent-{phase}.log` | Claude stderr |
| `runs/<ts>/artifacts/team{N}/round-{N}/metadata-{phase}.json` | `{round_id, phase, exit_code, timed_out, duration_ms, prompt_first_line, started_at, ended_at}` |

No `cue` / `done.json` / `runner.pid` / `kill` / `shutdown` — none
exist in this design.

---

## End-to-end interaction

```mermaid
sequenceDiagram
    autonumber
    participant Game as GameMgr
    participant Run as runner<br/>(run_phase_via_transport)
    participant Mgr as AgentManager<br/>(Claude / Codex / …)
    participant Tx as AgentTransport<br/>(DockerExec / SSH)
    participant Host as Agent host<br/>(container / VM)
    participant Bus as EventBus

    Note over Game,Host: install() — once per team at game start
    Game->>Mgr: install(handle, cfg, on_installed)
    Mgr->>Tx: run_command(npm/pip install …)
    Tx->>Host: provision CLI
    Mgr->>Tx: write_file(agent.env, mcp config, workdir/)
    Tx->>Host: stage static files
    Mgr-->>Game: on_installed(team_id)
    Game->>Bus: AGENT_INSTALLED

    Note over Game,Host: per phase
    Game->>Bus: AGENT_PROMPT_SENT (record_phase_started)
    Game->>Run: run_phase_via_transport(mgr, transport, handle, prompt, timeout)
    Run->>Tx: write_file(mgr.PROMPT_PATH)
    Tx->>Host: drop prompt.txt
    Run->>Tx: run_command(mgr.phase_command(), timeout)
    Tx->>Host: spawn CLI
    Note over Host: CLI reads + appends<br/>session store on disk (--continue / --resume)
    Host-->>Tx: stdout / stderr / exit_code
    Tx-->>Run: CommandResult
    alt timed_out
        Run->>Tx: kill_process(mgr.PHASE_KILL_PATTERN)
        Tx->>Host: pkill -KILL -f
    end
    Run-->>Game: CommandResult
    Note over Game: persist 3 artifacts (record_phase_done)
    Game->>Bus: AGENT_COMPLETED · TIMEOUT · FAILED
```

No async handoff; the kickoff subscriber awaits the runner inline.
The agent's MCP tool calls flow back over the existing FastMCP
transport — they're independent of the subprocess.

---

## Events

Reuse: `AGENT_INSTALLED` (once per team), `AGENT_READY` (MCP).

New (per phase):
- `AGENT_PROMPT_SENT` — before subprocess starts. Payload: `{phase, round_id, prompt_first_line}`.
- `AGENT_COMPLETED` — clean exit (exit_code 0). Payload: `{exit_code, duration_ms, phase, round_id}`.
- `AGENT_TIMEOUT` — phase timer expired; subprocess killed. Same shape.
- `AGENT_FAILED` — non-zero exit, not from timeout. Same shape + `error_excerpt`.

Drop or keep-for-back-compat: `AGENT_LAUNCHED` (no persistent launch
anymore; decide during implementation).

---

## Code changes

| File | Change |
|---|---|
| `agents/manager.py` | `AgentManager` ABC: `install` + `phase_command` + `PROMPT_PATH` + `PHASE_KILL_PATTERN`. Nothing else abstract. |
| `agents/runner.py` (new) | Free function `run_phase_via_transport(mgr, transport, handle, prompt, *, timeout, model)` — the 3-line dance. |
| `agents/transport.py` (new) | `AgentTransport` Protocol + `CommandResult` + `AgentHandle`. Pure interface. |
| `agents/transport_docker.py` (new) | `DockerExecBackend` — `asyncio.create_subprocess_exec("docker", "exec", ...)`. |
| `agents/transport_ssh.py` (deferred) | `SSHBackend` sibling for remote VMs. Out of scope for v1. |
| `agents/claude.py` | `ClaudeAgentManager`: `install` (one-shot), `phase_command` returning shell argv, `PROMPT_PATH` / `PHASE_KILL_PATTERN`. `status/kill/fetch_logs` kept concrete-only for legacy live smoke tests. |
| `agents/registry.py` | `build(cfg)` dispatches on `cfg.provider` (falls back to first cli token for M4-era yaml back-compat). |
| `models/agent.py` | `AgentConfig.provider: str = "claude"`. |
| `gamemgr/game_mgr.py` | Pre-loop install hook (parallel via `asyncio.gather`). Kickoff handlers call runner instead of running CLI directly. `record_phase_started` + `record_phase_done` own AGENT_* event emission + 3-file artifact persistence. `_agent_briefed: set[str]` so the long briefing only goes out once. |
| `gamemgr/events.py` | Add `AGENT_PROMPT_SENT`, `AGENT_COMPLETED`, `AGENT_TIMEOUT`, `AGENT_FAILED`. |
| `gamemgr/config.py` | Parse `agent.provider:` from yaml. |
| `agents/prompts/*` | First phase = full briefing + first task. Subsequent = 2–3 lines. |
| `panel/panel.html` | Render new event chips. |
| `main.py` | Use `agents.registry.build` per-team + `runner.run_phase_via_transport`. Kickoff handler shrinks to ~10 lines. |
| `tests/test_dcf_transport_docker.py` (new) | Mock `asyncio.create_subprocess_exec`; verify timeout escalation, write/read/kill paths. |
| `tests/test_dcf_phase_invocation.py` (new) | Three layers: `phase_command` argv, runner dance against `FakeTransport`, `record_phase_done` artifacts + events. |

**Existing tests** (M4 / M4.5 / M5.6) each call `install()` once in
isolation. `install()`'s public shape is preserved; only internals
change.

---

## Risks

| Risk | Mitigation |
|---|---|
| Signals don't propagate through `docker exec` | `transport.kill_process` (pkill) after `run_command` times out |
| `--continue` resumes a wrong session | Dedicated `workdir/` cwd isolates this arena's sessions |
| Container exits mid-phase | `run_command` fails immediately → `AGENT_FAILED` |
| MCP transport hiccups while `run_phase` is awaiting | `run_phase` is async; FastAPI / FastMCP keep responding |
| Conversation tokens grow unbounded | Deferred: `--fork-session` at round boundary or compress-history MCP tool |
| Stale session store from a prior run | Container is recreated by `Orchestrator.setup` each game |

---

## Multi-provider parking lot

Target CLIs beyond Claude Code, in rough priority:

| CLI | Persistent memory | Install | Status |
|---|---|---|---|
| Claude Code | `--continue` (cwd-scoped session.jsonl) | `npm i -g @anthropic-ai/claude-code` | ✅ v1 |
| Codex CLI (OpenAI) | `--resume <id>` + `~/.codex/sessions/` | `npm i -g @openai/codex` | subclass; not yet written |
| Gemini CLI (Google) | session resume + `.gemini/` | `npm i -g @google/gemini-cli` | subclass; not yet written |
| Kimi (Moonshot) | no native agentic CLI | route via litellm/openrouter to codex-or-claude OR use an OpenAI-compat harness | needs provider+wrapper decision |
| Qwen Coder (Alibaba) | same | same | same |

Each first-class CLI = one new `AgentManager` subclass (~50–80 LOC):
implement `install` + `phase_command` + the two class constants.
Register one line in `agents/registry.py`. No copy-pasting host
plumbing — `AgentTransport` + `agents/runner.py` handle that.

Kimi / Qwen don't have native agentic CLIs. Two viable paths:
1. **Route via OpenAI-compat proxy** (litellm / openrouter) so the
   model surfaces as if it were an OpenAI / Anthropic endpoint; reuse
   `CodexAgentManager` or `ClaudeAgentManager` pointing `OPENAI_BASE_URL`
   / `ANTHROPIC_BASE_URL` at the proxy. Lowest effort. Agent behavior
   keeps the wrapping CLI's tool surface.
2. **Pick a multi-provider agentic harness** (opencode, aider) and
   write a `OpenAICompatAgentManager` for it. More work, more native
   model behavior. Patch the harness for MCP if needed.

Pick when we're ready to actually run these — not v1 scope.

## Async mode (parking lot)

For autonomous agents (decide what to do, defend + attack in parallel,
query round state via MCP):
- Add MCP tools: `get_round_state`, `list_opposing_prods`, `list_active_flags`.
- Replace per-phase `run_phase` with one long-running `run_command` at
  game start carrying a "you drive yourself" prompt.
- Agent loops internally via MCP; orchestrator owns timers + flag
  rotation + network switching.

v1 constraints to keep async reachable:
- ❌ Don't bake phase-time-budgets into per-challenge prompts.
- ✅ Keep MCP tools additive; never reshape existing tools.

---

## Decisions (resolved)

| # | Decision | Choice |
|---|---|---|
| D1 | In-container daemon or host-driven? | **Host-driven, permanently.** Daemon is redundant in sync (orchestrator controls) AND async (agent self-controls via MCP). |
| D2 | Coexist with old `install + _launch`, or replace? | **Fully replace.** `install()` keeps its public shape; internals stop launching a supervisor. M4 / M4.5 / M5.6 tests stay valid. |
| D3 | Per-phase artifacts? | **stdout + stderr + metadata.json** under `runs/<ts>/artifacts/team{N}/round-{N}/`. |
| D4 | One session per team, or separate defense/attack sessions? | **One session.** Operator-role model; defense knowledge informs attack. |
| D5 | First-phase prompt = briefing + first task, or briefing-only at install + tasks later? | **Combined first prompt.** Subsequent prompts are 2–3 line task cues. |
| D6 | Manager talks to host directly, or through a transport interface? | **Transport interface.** `AgentTransport` Protocol; `DockerExecBackend` v1; `SSHBackend` deferred. Manager unchanged when backend swaps. |
| D7 | `AgentManager` ABC contract — fat method `run_phase` or data-only recipe? | **Data-only recipe.** ABC = `install` + `phase_command` + `PROMPT_PATH` + `PHASE_KILL_PATTERN`. The 3-line dance moves to `agents/runner.py`; artifact persistence + AGENT_* event emission move to `GameMgr.record_phase_done`. Adding a new CLI = 50–80 LOC subclass, no host plumbing copy-paste. |
| D8 | How does GameMgr pick which AgentManager subclass per team? | **`AgentConfig.provider`** (from `game.yaml`'s `agent.provider:`). `agents.registry.build(cfg)` dispatches. Falls back to first cli token for M4-era yaml back-compat. |
