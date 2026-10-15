"""QwenCodeAgentManager — Qwen Code CLI adapter for the AgentManager ABC.

Qwen Code (https://github.com/QwenLM/qwen-code) is an OSS terminal coding agent
— a gemini-cli fork tuned for the Qwen models but driving ANY OpenAI-compatible
endpoint. Like codex/opencode it runs one headless prompt and exits, and it
resumes the prior session with `--continue`, so it slots into the same per-phase
model with no long-lived daemon.

Two responsibilities, same shape as the Claude/Codex/OpenCode adapters:
  install(handle, cfg)        # per team: npm install, ssh pivot, write
                              # settings.json (remote MCP) + qwen.env. No daemon.
  phase_command(model=None)   # argv running `qwen --continue <prompt> --yolo`.

Qwen specifics (verified against qwen-code v0.20.1 source):
  * LLM provider — selected purely by env vars: `OPENAI_API_KEY` +
    `OPENAI_BASE_URL` + `OPENAI_MODEL` make `getAuthTypeFromEnv` pick USE_OPENAI
    (modelConfigUtils.ts). ALL THREE plus a non-empty model are required or the
    non-interactive run exits 1 ("No auth type selected"). Chat Completions wire.
  * MCP — one `~/.qwen/settings.json` (found via `QWEN_HOME`) with a remote
    `mcpServers.cyberarena` carrying the team token in an `X-Team-Token` header.
    `alwaysLoadTools: true` is REQUIRED: without it qwen defers MCP tools behind
    an in-band ToolSearch (mcp-tool.ts shouldDefer=true), so a short phase's agent
    may never surface submit_flag/restart_service.
  * Tool naming — qwen ALWAYS fully-qualifies remote MCP tools as
    `mcp__<server>__<tool>` (mcp-tool.ts:593), i.e. the SAME flattening Claude Code
    uses, so `agents/prompts/_helpers.mcp_tool` needs no qwen branch (it falls
    through to the claude default).
  * Resume — `--continue` resumes the most recent session and is a no-op on the
    first phase (does NOT error unless `--fork-session` is also set), so no
    exec-vs-resume marker is needed (unlike codex).
  * Prompt — passed as a positional arg (`"$(cat prompt.txt)"`) with stdin closed;
    `-p/--prompt` still works but is deprecated in favor of the positional form.

Host scaffolding (ssh pivot, npm install, dir layout, team env) comes from
agents/_provisioning.py; the per-phase loop is inherited from AgentManager.run_phase.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Callable, ClassVar

import docker

from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle

from . import _provisioning as prov
from ._provisioning import PROMPT_PATH, STATE_DIR, WORKDIR
from .manager import AgentManager
from .transport import AgentTransport
from .transport_docker import DockerExecBackend

log = logging.getLogger(__name__)

# qwen-local host paths (shared layout comes from _provisioning). QWEN_HOME
# overrides qwen's ~/.qwen dir so its settings.json + session store sit with the
# rest of the arena's agent state; settings.json lives directly under it.
QWEN_HOME = f"{STATE_DIR}/qwen"
SETTINGS_PATH = f"{QWEN_HOME}/settings.json"
ENV_PATH = f"{STATE_DIR}/qwen.env"

# npm package shipping the `qwen` binary. PINNED — keep in sync with the version
# baked in dockerfiles/agent/Dockerfile. Unpinned `@qwen-code/qwen-code` follows
# npm `latest` and the project ships 500+ releases (very churny), so a silent bump
# could change flag/config surface mid-match. Bumping is a deliberate, tested change.
QWEN_VERSION = "0.20.1"

# MCP server name as the agent sees it (matches the cyberarena server); qwen
# exposes its tools as mcp__cyberarena__<tool>.
MCP_SERVER_NAME = "cyberarena"

# install() callback — fires after config / env / ssh pivot are staged.
OnInstalled = Callable[[str], None]


class QwenCodeAgentManager(AgentManager):
    """Host-side controller for the Qwen Code CLI; one instance handles every
    team. Transport is injected (DockerExecBackend by default; tests pass a
    FakeTransport)."""

    # ABC contract (`agents.manager.AgentManager`).
    PROMPT_PATH: ClassVar[str] = PROMPT_PATH
    PHASE_KILL_PATTERN: ClassVar[str] = "qwen"
    # `agent.cli` selects this adapter; the command is fixed in phase_command,
    # so only the leading "qwen" token is honored.
    IMAGE: ClassVar[str] = "cyberarena/agent-qwen"
    CANONICAL_CLI: ClassVar[str | None] = "qwen"
    # Bake-only: qwen is baked into the agent image (pinned to QWEN_VERSION);
    # install() verifies it via verify_cli and does NO runtime npm install.
    REQUIRED_CLI: ClassVar[str | None] = "qwen"
    # Memory carries across phases via `qwen --continue` (like claude --continue).

    def __init__(
        self,
        *,
        transport: AgentTransport | None = None,
        client: docker.DockerClient | None = None,
    ) -> None:
        if transport is not None:
            self._transport = transport
            self._client = client  # may be None
        else:
            self._client = client or docker.from_env()
            self._transport = DockerExecBackend(client=self._client)

    # ── public API ──────────────────────────────────────────────────────

    def install(
        self,
        container: ContainerHandle,
        cfg: AgentConfig,
        *,
        on_installed: OnInstalled | None = None,
    ) -> None:
        """One-shot setup per team (idempotent): make dirs, clear any stale
        session store (so a reused container can't `--continue` a prior match),
        verify the baked qwen CLI, ssh pivot, write settings.json + qwen.env. No
        daemon — every per-phase run is a fresh `qwen --continue`."""
        prov.ensure_dirs(self._client, self._transport, container, extra=(QWEN_HOME,))
        # Fresh session per game: wipe QWEN_HOME (session/chat store lives here)
        # then re-create it, so `--continue` on phase 1 starts clean.
        prov.exec_sync(
            self._client, self._transport, container,
            ["sh", "-c", f"rm -rf {shlex.quote(QWEN_HOME)} && mkdir -p {shlex.quote(QWEN_HOME)}"],
            check=True,
        )

        # Bake-only: the pinned qwen is in the image — verify it, never runtime-install.
        if self.REQUIRED_CLI:
            prov.verify_cli(
                self._client, self._transport, container, self.REQUIRED_CLI,
                expected_version=QWEN_VERSION,
            )

        if cfg.own_prod_container_name:
            prov.provision_ssh_pivot(self._client, self._transport, container, cfg)

        self._write_settings(container, cfg)
        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())

        if not cfg.model:
            # qwen's non-interactive auth needs a non-empty OPENAI_MODEL or it
            # exits 1 ("No auth type selected"). Surface it at install, not mid-match.
            log.warning(
                "qwen installed on %s with EMPTY model — headless auth will fail; "
                "set agent.model in game.yaml", container.name,
            )

        log.info("qwen installed on %s (cli=%r, model=%r)",
                 container.name, cfg.cli, cfg.model)

        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv driving one phase: cd to WORKDIR, source qwen.env, then
        `qwen --continue <prompt> --yolo` with the prompt from PROMPT_PATH as a
        positional arg. `--continue` resumes the prior phase's session (no-op on
        phase 1, so no marker gate); `--yolo` auto-approves all tool calls
        (headless qwen else blocks on the approval gate). `-o stream-json` emits
        NDJSON events so the run's tee_path captures a structured transcript.
        `session_marker` (set by run_phase) launches under `setsid -w` with the
        leader PID teed to a pid file so on-timeout kill_session reaps the group."""
        model_arg = f"-m {shlex.quote(model)} " if model else ""
        prompt_q = shlex.quote(self.PROMPT_PATH)
        qwen_argv = (
            "qwen --continue "
            # Auto-approve every tool call; headless qwen else blocks on approval.
            "--yolo "
            # Raw NDJSON events to stdout (not the TUI) so the tee'd transcript is
            # parseable (parallels claude stream-json / codex exec --json).
            "--output-format stream-json "
            # `-m <model>` only when explicitly overridden; otherwise the model is
            # taken from $OPENAI_MODEL (qwen.env). Prompt is a positional arg
            # (`-p` is deprecated); stdin closed so the exec pipe EOF is never read.
            + model_arg
            + f'"$(cat {prompt_q})" < /dev/null'
        )
        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            inner = (
                f"echo $$ > {shlex.quote(pid_file)} && "
                f"export CYBERARENA_PHASE={shlex.quote(session_marker)} && "
                f"exec {qwen_argv}"
            )
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = qwen_argv
        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]

    # ── internals (qwen-specific config / env) ───────────────────────────

    def _write_settings(self, container: ContainerHandle, cfg: AgentConfig) -> None:
        """Write settings.json (mode 600): the remote MCP server carrying the team
        token in an X-Team-Token header when cfg.mcp_endpoint is set. The LLM
        provider (base_url/key/model) is selected via env vars, not here."""
        settings: dict = {}
        if cfg.mcp_endpoint:
            settings["mcpServers"] = {
                MCP_SERVER_NAME: {
                    "httpUrl": cfg.mcp_endpoint,
                    "headers": {"X-Team-Token": cfg.team_token},
                    "timeout": 30000,
                    # REQUIRED — else qwen defers MCP tools behind ToolSearch and a
                    # short phase's agent may never surface the arena tools.
                    "alwaysLoadTools": True,
                    # Skip qwen's per-tool trust prompt for the arena server (belt +
                    # braces with --yolo).
                    "trust": True,
                }
            }
        self._transport.write_file(
            container, SETTINGS_PATH, (json.dumps(settings, indent=2) + "\n").encode()
        )
        prov.exec_sync(
            self._client, self._transport, container,
            ["chmod", "600", SETTINGS_PATH], check=True,
        )

    def _build_env_file(self, cfg: AgentConfig) -> str:
        """qwen.env: the OpenAI-compatible provider selection (OPENAI_API_KEY +
        OPENAI_BASE_URL + OPENAI_MODEL — all three needed for headless auth) +
        QWEN_HOME (so qwen finds settings.json) + the shared CYBERARENA_* /
        extra_env lines."""
        lines = [
            f"export OPENAI_API_KEY={shlex.quote(cfg.api_key)}",
            f"export OPENAI_BASE_URL={shlex.quote(cfg.base_url)}",
            f"export OPENAI_MODEL={shlex.quote(cfg.model)}",
            f"export QWEN_HOME={shlex.quote(QWEN_HOME)}",
        ]
        lines.extend(prov.team_env_lines(cfg))
        return "\n".join(lines) + "\n"
