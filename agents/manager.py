"""`AgentManager` ABC — per-CLI recipe + the one-phase dance.

A subclass declares two things about an LLM-agent CLI (Claude Code,
codex, Gemini, …):

  1. `install` — how to install the CLI inside the agent host
  2. `phase_command` — shell argv to invoke it for one phase,
     resuming the prior session

`run_phase` is the shared glue (stage prompt → invoke → kill on
timeout). Concrete subclasses inherit it; no reimplementation needed.

Adding a new CLI = subclass + 2 class constants. The transport handles
host plumbing independently.

`status` / `kill` / `fetch_logs` are deliberately not on the ABC —
they exist only on `ClaudeAgentManager` as legacy smoke-test hooks
and are best-effort given the per-phase invocation model.
"""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from typing import ClassVar

from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle

from .transport import AgentHandle, AgentTransport, CommandResult


class AgentManager(ABC):
    # Path inside the agent host where the per-phase prompt is staged
    # before each invocation. The CLI reads it via stdin redirect (or
    # `--prompt-file` — up to `phase_command` to wire it).
    PROMPT_PATH: ClassVar[str]

    # `pkill -KILL -f <pattern>` fallback if `kill_session` can't find
    # the pidfile (transport-level timeout cleanup). Match a substring
    # unique to this CLI's process tree.
    PHASE_KILL_PATTERN: ClassVar[str]

    # The CLI binary this adapter drives, e.g. "claude" / "codex". `install`
    # asserts it is present in the agent image (`prov.verify_cli`) and installs
    # nothing — the image is the only install path. `None` means this adapter
    # needs no LLM CLI (see ScriptAgentManager).
    REQUIRED_CLI: ClassVar[str | None] = None

    # The canonical leading invocation this adapter actually runs, e.g.
    # "claude" / "codex exec". `game.yaml`'s `agent.cli` is used ONLY to
    # select the adapter (leading token) — which binary an adapter needs is
    # `REQUIRED_CLI` above, not a prefix of this. The
    # real command is fixed in `phase_command`, so any `agent.cli` tokens
    # beyond this prefix are ignored. Config load warns when extras are
    # present so an edit to them isn't a silent no-op. `None` means the
    # adapter honors the full `agent.cli` verbatim (see ScriptAgentManager)
    # — nothing is ignored, so no warning fires.
    CANONICAL_CLI: ClassVar[str | None] = None

    # The agent image this adapter's harness is baked into. The agent images are
    # layered: `cyberarena/agent-base` (dockerfiles/agent/base — tooling, Node, uv,
    # NO harness CLI) plus one thin `cyberarena/agent-<provider>` per harness
    # (dockerfiles/agent/<provider>, FROM agent-base). game.yaml's dev node must
    # name THIS image (config load warns on a mismatch; `install` then fails at
    # `verify_cli`). Adapters that drive no LLM CLI keep the base default.
    IMAGE: ClassVar[str] = "cyberarena/agent-base"

    @abstractmethod
    def install(
        self,
        container: ContainerHandle,
        cfg: AgentConfig,
        *,
        on_installed=None,
    ) -> None:
        """One-shot setup at game start: install the CLI, write env /
        MCP config, provision any SSH pivot the agent needs.
        Idempotent. No process is launched."""
        ...

    @abstractmethod
    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv that drives one phase. Reads the prompt from
        `PROMPT_PATH`; resumes the prior session (`--continue`).

        `session_marker` lets `kill_session` target this run's process
        group via a pidfile (`/tmp/cyberarena_phase_<marker>.pid`).
        Wrap the CLI under `setsid -w` and tee the leader PID into
        that file so the on-timeout kill is non-racy.
        """
        ...

    async def dry_run_diagnostics(self, transport, handle) -> dict[str, str]:
        """Extra evidence for a dry-run that exited without reporting ready,
        as {name: text}; each is persisted scrubbed + tailed to
        `diagnostics/dry-run-<name>-<team>.txt`. Nothing by default: for a CLI
        harness the stdout/stderr tails already say everything. An adapter
        whose harness runs as a persistent server (opencode) overrides this to
        read that server's log — the one place a `run` client that exits 0
        with empty output leaves its reason."""
        return {}

    async def run_phase(
        self,
        transport: AgentTransport,
        handle: AgentHandle,
        prompt: str,
        *,
        timeout: float,
        model: str | None = None,
        tee_path: "str | None" = None,
    ) -> CommandResult:
        """Stage prompt, run CLI with timeout, kill session on timeout.

        Returns the raw `CommandResult`. Persistence + event emission
        (AGENT_COMPLETED / TIMEOUT / FAILED) live in `GameMgr.record_phase_done`.
        `tee_path` (optional) streams the agent's stdout transcript to that file
        live, so the panel can show it in real time instead of only at phase end.
        """
        transport.write_file(handle, self.PROMPT_PATH, prompt.encode())
        session_marker = secrets.token_hex(6)
        result = await transport.run_command(
            handle,
            self.phase_command(model=model, session_marker=session_marker),
            timeout=timeout,
            tee_path=tee_path,
        )
        if result.timed_out:
            # SIGTERM on the docker exec wrapper doesn't always
            # propagate to the in-container child; signal the
            # session's whole process group via the pidfile marker.
            transport.kill_session(handle, session_marker)
        return result
