"""ScriptAgentManager — a non-LLM "agent" that runs a fixed script.

Provider key: ``script``. Selected via ``agent.provider: script`` in the
game config. Useful when you want a *deterministic* attacker/defender in
the arena — for challenge bring-up, CI, or demos — without provisioning
LLM-CLI credentials.

It reuses everything ``ClaudeAgentManager`` already does at install time
(workdir, ``agent.env`` with the ``CYBERARENA_*`` context, the SSH pivot,
and ``mcp.json``) but:

  * skips the LLM-CLI preflight (``REQUIRED_CLI = None``), and
  * stashes ``cfg.cli`` to a script file and runs THAT each phase instead
    of ``claude -p --continue``.

So ``agent.cli`` in the YAML can be any shell program (e.g. a multi-line
``|`` block running an exploit). It is invoked once per phase with the
phase prompt on stdin and ``agent.env`` sourced into the environment, so
the script can read ``CYBERARENA_OPPOSING_PRODS`` or provisioned values such as
``CYBERARENA_EXPLOIT_PATH`` and branch on the prompt text. Exit 0 →
AGENT_COMPLETED; non-zero → AGENT_FAILED (same classification as Claude).
"""

from __future__ import annotations

import shlex
from typing import ClassVar

from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle

from .claude import ENV_PATH, STATE_DIR, WORKDIR, ClaudeAgentManager, OnInstalled

# Where install() stashes cfg.cli; phase_command runs this each phase.
CLI_SCRIPT_PATH = f"{STATE_DIR}/agent_cli.sh"


class ScriptAgentManager(ClaudeAgentManager):
    # Reuse Claude's PROMPT_PATH (run_phase stages the prompt there).
    PHASE_KILL_PATTERN: ClassVar[str] = "agent_cli.sh"
    # A scripted agent drives no LLM CLI, so nothing to preflight (this is what
    # the `cli.startswith("claude")` gate used to accomplish by accident).
    REQUIRED_CLI: ClassVar[str | None] = None
    # This adapter runs the FULL `agent.cli` verbatim (stashed to a script),
    # so no tokens are ignored — override Claude's "claude" back to None so
    # config load never warns about "ignored" flags for a scripted agent.
    # A scripted agent needs the shared tooling only — no harness CLI — so it
    # runs the base image, NOT Claude's (overrides the inherited IMAGE).
    IMAGE: ClassVar[str] = "cyberarena/agent-base"
    CANONICAL_CLI: ClassVar[str | None] = None

    def install(
        self,
        container: ContainerHandle,
        cfg: AgentConfig,
        *,
        on_installed: OnInstalled | None = None,
    ) -> None:
        # Inherit the full setup (workdir + agent.env + ssh pivot + mcp.json).
        # The `claude` preflight is skipped via REQUIRED_CLI = None.
        super().install(container, cfg, on_installed=on_installed)
        # Stash the agent program so phase_command can run it verbatim.
        self._transport.write_file(
            container, CLI_SCRIPT_PATH, (cfg.cli.rstrip() + "\n").encode()
        )

    def phase_command(
        self,
        *,
        model: str | None = None,  # unused; a script agent has no model
        session_marker: str | None = None,
    ) -> list[str]:
        run = f"bash {shlex.quote(CLI_SCRIPT_PATH)} < {shlex.quote(self.PROMPT_PATH)}"
        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            inner = (
                f"echo $$ > {shlex.quote(pid_file)} && "
                f"export CYBERARENA_PHASE={shlex.quote(session_marker)} && "
                f"exec {run}"
            )
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = run
        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]
