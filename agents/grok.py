"""GrokAgentManager — xAI "Grok Build" CLI adapter for the AgentManager ABC.

The official xAI Grok CLI (`@xai-official/grok`, binary `grok`) is a headless-capable
agentic coding CLI: `grok -p "<prompt>"` runs one prompt to completion and exits, and
`-s <session-id>` create-or-resumes a headless session. It supports BYOK against any
OpenAI-compatible endpoint, so it slots into the same per-phase model as codex.

Two responsibilities, same shape as the other adapters:
  install(handle, cfg)        # per team: npm install, write ~/.grok/config.toml
                              # (BYOK model + remote MCP) + grok.env, ssh pivot.
  phase_command(model=None)   # `grok -p <prompt> -s <sid> --always-approve …`.

Grok specifics (verified against docs.x.ai/build):
  * LLM BYOK — a custom `[model.<id>]` provider (`model`/`base_url`/`env_key`/
    `api_backend = "chat_completions"`) with `[models] default = <id>`, so the CLI
    hits our api-router over OpenAI Chat Completions. The key is read from the env
    var named by `env_key` (kept in grok.env, not the toml). No browser OAuth.
  * MCP — a `[mcp_servers.cyberarena]` block with `url` + `headers` (streamable HTTP,
    `${VAR}` expansion), carrying the team token in an X-Team-Token header. Grok
    exposes remote MCP tools to the model as `<server>__<tool>` (double underscore),
    so `agents/prompts/_helpers.mcp_tool` gets a dedicated `grok` branch. Claude-config
    auto-load is turned off (`[compat.claude] mcps = false`) so it doesn't pick up a
    stray ~/.claude.json.
  * Approvals — `--always-approve` auto-approves every tool call (headless, no TTY).
  * Resume — the first invocation CREATES the session (`-s <uuid>`) and later ones
    RESUME it (`--resume <uuid>`), gated on a marker file. `-s` alone cannot resume:
    grok rejects a reused UUID with "Session ID <uuid> is already in use." (exit 1),
    which in async mode would fast-exit every chunk and fail the match.
  * Config home — grok reads `~/.grok/config.toml`; grok.env exports HOME=/root so the
    path is deterministic under `docker exec` (which does not always set HOME).
  * Beta hygiene — the CLI is closed-source, early-access and churns fast, so the
    version is PINNED and `--no-auto-update` stops a silent self-upgrade mid-match.

Host scaffolding (ssh pivot, npm install, dir layout, team env) comes from
agents/_provisioning.py.
"""

from __future__ import annotations

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

# grok reads ~/.grok/config.toml (user scope, its documented primary). The agent
# container runs as root, so HOME=/root; grok.env pins HOME so it's deterministic.
GROK_DIR = "/root/.grok"
CONFIG_PATH = f"{GROK_DIR}/config.toml"
ENV_PATH = f"{STATE_DIR}/grok.env"

# Fixed per-match session id (valid UUID). Each team is a separate container
# (separate ~/.grok/sessions), so a shared literal is fine.
#
# `-s` does NOT create-or-resume: grok's own help says the UUID "must not already
# exist under the target session directory ... Does not resume existing sessions —
# use `--resume` / `--continue` instead", and a second `-s` with the same UUID exits
# 1 with "Session ID <uuid> is already in use." So the first invocation creates the
# session with `-s` and every later one resumes it with `--resume`, gated on
# STARTED_MARKER — the same exec-vs-resume pattern codex uses.
SESSION_ID = "a1b2c3d4-0000-4000-8000-000000000001"
# Gates create-vs-resume in phase_command. Lives under GROK_DIR so install()'s
# wipe of that directory also clears it (fresh session per game).
STARTED_MARKER = f"{GROK_DIR}/.cyberarena_started"

# Env var the config.toml `env_key` points at for the LLM key.
API_KEY_ENV = "GROK_LLM_KEY"
# The BYOK model_provider id written into config.toml (arbitrary label).
PROVIDER_ID = "arena"
# MCP server name as the agent sees it; grok exposes tools as cyberarena__<tool>.
MCP_SERVER_NAME = "cyberarena"
# PINNED — keep in sync with the version baked in dockerfiles/agent/Dockerfile.
# The official Grok CLI is closed-source beta with a very high release cadence,
# so pinning the BAKED build (verify_cli, no runtime npm install) is what keeps
# a run reproducible — a runtime `npm i -g` would re-pull a possibly-different
# build of the same tag at game time.
GROK_VERSION = "0.2.111"

OnInstalled = Callable[[str], None]


def _toml_str(s: str) -> str:
    """Quote a string as a TOML basic string (escape `\\` and `"`)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class GrokAgentManager(AgentManager):
    """Host-side controller for the xAI Grok CLI; one instance handles every team.
    Transport is injected (DockerExecBackend by default; tests pass a
    FakeTransport)."""

    PROMPT_PATH: ClassVar[str] = PROMPT_PATH
    PHASE_KILL_PATTERN: ClassVar[str] = "grok"
    IMAGE: ClassVar[str] = "cyberarena/agent-grok"
    CANONICAL_CLI: ClassVar[str | None] = "grok"
    # Bake-only: grok is baked into the agent image (pinned to GROK_VERSION);
    # install() verifies it via verify_cli and does NO runtime npm install.
    REQUIRED_CLI: ClassVar[str | None] = "grok"

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
        ~/.grok (config + session store, so a reused container can't resume a prior
        match), verify the baked grok CLI, ssh pivot, write config.toml + grok.env."""
        prov.ensure_dirs(self._client, self._transport, container)
        # Fresh config + session store per game.
        prov.exec_sync(
            self._client, self._transport, container,
            ["sh", "-c", f"rm -rf {shlex.quote(GROK_DIR)} && mkdir -p {shlex.quote(GROK_DIR)}"],
            check=True,
        )

        # Bake-only: the pinned grok is in the image — verify it, never runtime-install.
        if self.REQUIRED_CLI:
            prov.verify_cli(
                self._client, self._transport, container, self.REQUIRED_CLI,
                expected_version=GROK_VERSION,
            )

        if cfg.own_prod_container_name:
            prov.provision_ssh_pivot(self._client, self._transport, container, cfg)

        self._transport.write_file(container, CONFIG_PATH, self._build_config_toml(cfg).encode())
        prov.exec_sync(
            self._client, self._transport, container,
            ["chmod", "600", CONFIG_PATH], check=True,
        )
        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())

        log.info("grok installed on %s (cli=%r, model=%r)", container.name, cfg.cli, cfg.model)

        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv driving one phase: cd to WORKDIR, source grok.env, then
        `grok -p <prompt> $SESS --always-approve`, where `$SESS` is `-s <uuid>` on
        the first invocation and `--resume <uuid>` afterwards (gated on
        STARTED_MARKER — grok's `-s` cannot resume). `--always-approve`
        auto-approves every tool call; `--output-format streaming-json` emits NDJSON
        for the tee'd transcript; `--no-auto-update`/`--no-alt-screen` keep it CI-safe
        and non-TUI. The model comes from config.toml's default provider (the `model`
        param is accepted for ABC parity but not injected). `session_marker` (set by
        run_phase) launches under `setsid -w` with a pidfile for the on-timeout kill."""
        prompt_q = shlex.quote(self.PROMPT_PATH)
        # `$SESS` is intentionally unquoted so "--resume <uuid>" word-splits.
        grok_argv = (
            f'grok -p "$(cat {prompt_q})" '
            "$SESS "
            "--always-approve "
            "--output-format streaming-json "
            "--no-auto-update --no-alt-screen "
            "< /dev/null"
        )
        marker_q = shlex.quote(STARTED_MARKER)
        sid_q = shlex.quote(SESSION_ID)
        # First invocation creates the session (-s); later ones resume it
        # (--resume). Re-using -s on an existing UUID exits 1 ("already in use"),
        # which in async mode would fast-exit every chunk and fail the match.
        session_gate = (
            f'if [ -f {marker_q} ]; then SESS="--resume {sid_q}"; '
            f'else : > {marker_q}; SESS="-s {sid_q}"; fi'
        )
        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            inner = (
                f"{session_gate} && "
                f"echo $$ > {shlex.quote(pid_file)} && "
                f"export CYBERARENA_PHASE={shlex.quote(session_marker)} && "
                f"exec {grok_argv}"
            )
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = f"{session_gate} && {grok_argv}"
        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]

    # ── internals ───────────────────────────────────────────────────────

    def _build_config_toml(self, cfg: AgentConfig) -> str:
        """~/.grok/config.toml: a BYOK `[model.<id>]` provider pointed at
        cfg.base_url over Chat Completions (key via env_key), the default model,
        the remote MCP server (when cfg.mcp_endpoint is set) with the team token
        via `${...}` env expansion, and claude-config MCP auto-load disabled."""
        out: list[str] = [
            "[models]",
            f"default = {_toml_str(PROVIDER_ID)}",
            "",
            f"[model.{PROVIDER_ID}]",
            f"model = {_toml_str(cfg.model)}",
            f"base_url = {_toml_str(cfg.base_url)}",
            f"env_key = {_toml_str(API_KEY_ENV)}",
            'api_backend = "chat_completions"',
        ]
        if cfg.mcp_endpoint:
            out += [
                "",
                f"[mcp_servers.{MCP_SERVER_NAME}]",
                f"url = {_toml_str(cfg.mcp_endpoint)}",
                # Token via ${...}; team_env_lines exports CYBERARENA_TEAM_TOKEN.
                'headers = { "X-Team-Token" = "${CYBERARENA_TEAM_TOKEN}" }',
                "startup_timeout_sec = 30",
            ]
        # Don't auto-load ~/.claude.json / .mcp.json MCP servers.
        out += ["", "[compat.claude]", "mcps = false"]
        return "\n".join(out) + "\n"

    def _build_env_file(self, cfg: AgentConfig) -> str:
        """grok.env: the LLM key (referenced by config.toml's env_key), HOME=/root
        (so ~/.grok resolves under docker exec), and the shared CYBERARENA_* /
        extra_env lines (which export CYBERARENA_TEAM_TOKEN for the MCP header)."""
        lines = [
            f"export {API_KEY_ENV}={shlex.quote(cfg.api_key)}",
            "export HOME=/root",
        ]
        lines.extend(prov.team_env_lines(cfg))
        return "\n".join(lines) + "\n"
