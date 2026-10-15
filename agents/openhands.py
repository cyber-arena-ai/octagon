"""OpenHandsAgentManager — OpenHands (V1 SDK) adapter for the AgentManager ABC.

OpenHands (https://github.com/OpenHands/OpenHands) is an OSS software-agent platform.
Its headless CLI can't resume a session across invocations, but our per-phase model
needs cross-phase memory — so this adapter drives the **openhands-sdk** directly via a
tiny driver script (agents/openhands_assets/driver.py). The driver reuses a fixed
`conversation_id` + `persistence_dir`, so a fresh process each phase resumes the prior
conversation. Actions run via `LocalWorkspace` (direct subprocess — NO Docker-in-Docker
inside the agent container).

Two responsibilities, same shape as the other adapters:
  install(handle, cfg)        # per team: copy driver.py + write openhands.env, ssh
                              # pivot. NO package install (baked via uv). No daemon.
  phase_command(model=None)   # `<venv-python> driver.py --prompt-file <PROMPT_PATH>`.

OpenHands specifics (verified against openhands-sdk v1.36.1 source):
  * Install channel — openhands-sdk/-tools are PyPI packages baked into the agent image
    with uv at /opt/openhands-venv (the isolated agent net blocks PyPI, so it must be
    fully baked); install() does ZERO network — it only copies the driver + writes env.
  * LLM provider — the driver builds `LLM(model, base_url, api_key)` (LiteLLM) from
    LLM_MODEL/LLM_BASE_URL/LLM_API_KEY, so it hits our api-router over Chat Completions.
  * MCP — the driver builds `MCPServer(url, transport="streamable-http", headers={
    X-Team-Token})`. The SDK exposes remote MCP tools to the model under their BARE
    server name (MCPToolDefinition.name == mcp_tool.name, no prefix), so
    `agents/prompts/_helpers.mcp_tool` routes `openhands` through the codex/bare branch.
  * Runtime — LocalWorkspace = `subprocess.run()`, the SDK default; never DockerWorkspace,
    so tools execute in this agent container with no nested Docker.
  * Resume — fixed conversation_id + persistence_dir; the first phase creates the
    conversation, later phases resume it. No exec-vs-resume marker.
  * Transcript — the driver/SDK logs to stdout; not a known stream schema, so the
    live-panel transcript/metrics degrade to empty (token usage/cost still come from the
    api-router meter).

Host scaffolding (ssh pivot, dir layout, team env) comes from agents/_provisioning.py.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
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

# openhands-local host paths (shared layout comes from _provisioning).
OPENHANDS_HOME = f"{STATE_DIR}/openhands"
DRIVER_PATH = f"{OPENHANDS_HOME}/driver.py"
# Conversation persistence store (cross-phase memory keyed on CONVO_ID).
PERSIST_DIR = f"{OPENHANDS_HOME}/conversations"
ENV_PATH = f"{STATE_DIR}/openhands.env"

# Baked venv (dockerfiles/agent/openhands bakes openhands-sdk + openhands-tools here with uv).
VENV_PYTHON = "/opt/openhands-venv/bin/python"

# Fixed per-match conversation id (valid UUID). Fresh-on-first, resume-after; each team
# is its own container (its own PERSIST_DIR), so a shared literal is fine.
CONVO_ID = "a1b2c3d4-0000-4000-8000-000000000002"

# PINNED — keep in sync with the versions baked in dockerfiles/agent/openhands/Dockerfile.
OPENHANDS_SDK_VERSION = "1.36.1"

# Vendored driver shipped in-repo.
_ASSETS_DIR = Path(__file__).parent / "openhands_assets"

OnInstalled = Callable[[str], None]


class OpenHandsAgentManager(AgentManager):
    """Host-side controller for OpenHands (via the openhands-sdk driver); one instance
    handles every team. Transport is injected (DockerExecBackend by default; tests pass
    a FakeTransport)."""

    PROMPT_PATH: ClassVar[str] = PROMPT_PATH
    PHASE_KILL_PATTERN: ClassVar[str] = "openhands"
    IMAGE: ClassVar[str] = "cyberarena/agent-openhands"
    CANONICAL_CLI: ClassVar[str | None] = "openhands"

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
        """One-shot setup per team (idempotent). OpenHands is baked into the image, so
        install() does NO network: make dirs, clear any stale conversation store (so a
        reused container can't resume a prior match), ssh pivot, copy the driver, write
        openhands.env."""
        # Fail LOUD on a stale image. The openhands-sdk venv is baked (no runtime
        # install), so an old cyberarena/agent-openhands has no /opt/openhands-venv — without
        # this the phase dies with a bare rc=127 whose stderr the dry-run discards
        # (it runs without tee_path), leaving only "AGENT_READY not received".
        rc, _out = prov.exec_sync(
            self._client, self._transport, container, ["test", "-x", VENV_PYTHON],
        )
        if rc != 0:
            raise RuntimeError(
                f"openhands interpreter missing at {VENV_PYTHON} on {container.name}: "
                f"this agent image predates the openhands-sdk bake. Rebuild it with "
                f"`docker build -t cyberarena/agent-openhands dockerfiles/agent/openhands` "
                f"(or `bash scripts/build_images.sh`)."
            )

        prov.ensure_dirs(self._client, self._transport, container, extra=(OPENHANDS_HOME, PERSIST_DIR))
        # Fresh conversation per game: drop the persistence store.
        prov.exec_sync(
            self._client, self._transport, container,
            ["sh", "-c", f"rm -rf {shlex.quote(PERSIST_DIR)} && mkdir -p {shlex.quote(PERSIST_DIR)}"],
            check=True,
        )

        if cfg.own_prod_container_name:
            prov.provision_ssh_pivot(self._client, self._transport, container, cfg)

        driver = (_ASSETS_DIR / "driver.py").read_bytes()
        self._transport.write_file(container, DRIVER_PATH, driver)
        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())

        log.info("openhands installed on %s (cli=%r, model=%r)", container.name, cfg.cli, cfg.model)

        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv driving one phase: cd to WORKDIR, source openhands.env, then run
        the baked-venv python on the SDK driver with the prompt from PROMPT_PATH. The
        driver resumes the fixed conversation id from PERSIST_DIR. `model` comes from
        LLM_MODEL (env); the param is accepted for ABC parity but not injected.
        `session_marker` (set by run_phase) launches under `setsid -w` with a pidfile
        for the on-timeout kill."""
        driver_argv = (
            f"{shlex.quote(VENV_PYTHON)} {shlex.quote(DRIVER_PATH)} "
            f"--prompt-file {shlex.quote(self.PROMPT_PATH)} < /dev/null"
        )
        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            inner = (
                f"echo $$ > {shlex.quote(pid_file)} && "
                f"export CYBERARENA_PHASE={shlex.quote(session_marker)} && "
                f"exec {driver_argv}"
            )
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = driver_argv
        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]

    # ── internals ───────────────────────────────────────────────────────

    def _build_env_file(self, cfg: AgentConfig) -> str:
        """openhands.env: the LiteLLM provider (LLM_MODEL/LLM_BASE_URL/LLM_API_KEY), the
        MCP endpoint, the driver's workspace/persistence/conversation knobs, and the
        shared CYBERARENA_* / extra_env lines (which export CYBERARENA_TEAM_TOKEN, used
        by the driver's MCP header)."""
        lines = [
            f"export LLM_MODEL={shlex.quote(cfg.model)}",
            f"export LLM_BASE_URL={shlex.quote(cfg.base_url)}",
            f"export LLM_API_KEY={shlex.quote(cfg.api_key)}",
            f"export CYBERARENA_MCP_ENDPOINT={shlex.quote(cfg.mcp_endpoint)}",
            f"export OPENHANDS_WORKSPACE={shlex.quote(WORKDIR)}",
            f"export OPENHANDS_PERSIST_DIR={shlex.quote(PERSIST_DIR)}",
            f"export OPENHANDS_CONVO_ID={shlex.quote(CONVO_ID)}",
        ]
        lines.extend(prov.team_env_lines(cfg))
        return "\n".join(lines) + "\n"
