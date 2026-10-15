"""IdleAgentManager — a non-LLM "agent" that installs nothing and just stays
alive until the supervisor kills it at game end.

Provider key: ``idle``. Selected via ``agent.provider: idle`` in the game config.

Solo ATTACK mode uses it for the target team: team2's prod runs the challenge +
planted flag, and the "agent" on that team does no defense/patching — it simply
occupies the normal agent lifecycle so the topology stays a uniform 2 teams + 2
agents (no ``if agent:`` special-casing in AsyncLoop / supervisor / transcript /
lifecycle / logs / metrics). See docs/archived/solo_mode_plan.md §4e.

Unlike ``ScriptAgentManager`` it does NOT inherit Claude's install (no workdir /
agent.env / SSH pivot / mcp.json) — the target does nothing and needs none of it.
Each async "chunk" runs a long ``sleep`` that blocks until ``kill_all_agents``
(``pkill -f`` on ``PHASE_KILL_PATTERN``) terminates it at game end. It emits no
trajectory events (its stdout is empty), so the target team's transcript is
empty — that is expected, not an error.
"""

from __future__ import annotations

import shlex
from typing import ClassVar

from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle

from .manager import AgentManager
from .transport import AgentTransport

# A single long sleep marker the supervisor's kill pattern targets. Long enough
# that it never wakes on its own within a run; killed at game end.
_IDLE_MARKER = "cyberarena_idle_agent"
_IDLE_SLEEP_SECS = 10 ** 9


class IdleAgentManager(AgentManager):
    # No prompt is staged for an idle agent, but the ABC declares the attribute;
    # run_phase would write here if ever called with a prompt (harmless).
    PROMPT_PATH: ClassVar[str] = "/var/lib/cyberarena/prompt.txt"
    # kill_all_agents does `pkill -KILL -f <PHASE_KILL_PATTERN>`; match the sleep
    # wrapper's unique marker so the idle process group is reaped at game end.
    PHASE_KILL_PATTERN: ClassVar[str] = _IDLE_MARKER
    # Honors its whole (ignored) cli verbatim — nothing to warn about.
    # Installs nothing and runs nothing: the base image is all it needs.
    IMAGE: ClassVar[str] = "cyberarena/agent-base"
    CANONICAL_CLI: ClassVar[str | None] = None

    def install(
        self,
        container: ContainerHandle,
        cfg: AgentConfig,
        *,
        on_installed=None,
    ) -> None:
        """No-op: an idle target installs nothing (no CLI, env, SSH, or MCP)."""
        return None

    def phase_command(
        self,
        *,
        model: str | None = None,      # unused; an idle agent has no model
        session_marker: str | None = None,
    ) -> list[str]:
        # A long sleep tagged with the kill marker so `pkill -f` reaches it. Wrap
        # under setsid + a pidfile when a marker is given, mirroring the other
        # managers so on-timeout / teardown kills target the whole process group.
        sleep = f"exec sleep {_IDLE_SLEEP_SECS}  # {_IDLE_MARKER}"
        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            inner = f"echo $$ > {shlex.quote(pid_file)} && {sleep}"
            return ["bash", "-c", f"exec setsid -w bash -c {shlex.quote(inner)}"]
        return ["bash", "-c", sleep]

    async def run_phase(
        self,
        transport: AgentTransport,
        handle,
        prompt: str,
        *,
        timeout: float,
        model: str | None = None,
        tee_path: str | None = None,
    ):
        # Skip staging a prompt (no CLI reads it); just run the idle sleep until
        # killed. Reuses the base timeout/kill glue by delegating the invocation.
        import secrets

        session_marker = secrets.token_hex(6)
        result = await transport.run_command(
            handle,
            self.phase_command(model=model, session_marker=session_marker),
            timeout=timeout,
            tee_path=tee_path,
        )
        if result.timed_out:
            transport.kill_session(handle, session_marker)
        return result
