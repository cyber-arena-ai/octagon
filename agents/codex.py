"""CodexAgentManager — OpenAI Codex CLI adapter for the AgentManager ABC.

Mirrors `ClaudeAgentManager` but drives `codex exec` instead of
`claude -p`. The shared host scaffolding (SSH pivot, CLI preflight,
directory layout, team env vars) comes from `agents/_provisioning.py`;
only the Codex-specific bits live here:

  install(handle, cfg)       # assert the image baked `codex`, write
                             # $CODEX_HOME/config.toml + agent.env,
                             # SSH pivot. No process launched.
  phase_command(model=None)  # `codex exec [resume --last] --json …`
                             # consuming the prompt from PROMPT_PATH.

Key differences from Claude (verified against codex-cli 0.135.0 + the
app-us.ppapi.ai router, 2026-06-11; the pin has since moved to 0.146.0 —
see CODEX_VERSION — and these behaviors are NOT re-verified there):

  * Wire API — Codex's custom model_provider talks the **Responses
    API** (`wire_api = "responses"`); `base_url` keeps its `/v1`
    suffix (unlike Claude, which drops it). gpt-5.4 works on responses;
    gpt-5.3-codex is responses-only.
  * Memory across phases — `codex exec resume --last` resumes the most
    recent cwd-scoped session. The first phase has no session to
    resume, so a marker file (`STARTED_MARKER`) gates exec-vs-resume.
  * MCP auth — Codex's streamable-HTTP MCP client carries our
    `X-Team-Token` via `env_http_headers` (reading the already-exported
    `CYBERARENA_TEAM_TOKEN`), so the orchestrator's MCP server needs no
    change. The API key reaches Codex via `env_key = "CODEX_API_KEY"`.
  * Prompt — passed as a positional arg (`"$(cat prompt.txt)"`) with
    stdin closed; `codex exec` otherwise blocks reading stdin to EOF.
"""

from __future__ import annotations

import logging
import os
import shlex
from typing import Callable, ClassVar

import docker

from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle

from . import _provisioning as prov
from ._provisioning import ENV_PATH, PROMPT_PATH, STATE_DIR, WORKDIR
from .manager import AgentManager
from .transport import AgentTransport
from .transport_docker import DockerExecBackend

log = logging.getLogger(__name__)

# Codex-local host paths. CODEX_HOME holds config.toml + the session
# store (sessions/YYYY/MM/DD/*.jsonl that `resume --last` reads). Kept
# under STATE_DIR rather than ~/.codex so it sits with the rest of the
# arena's agent state. (Must NOT be under /tmp — codex refuses to create
# helper binaries there.)
CODEX_HOME = f"{STATE_DIR}/codex"
CODEX_CONFIG_PATH = f"{CODEX_HOME}/config.toml"
# Gates exec-vs-resume in phase_command: absent → first phase (plain
# `codex exec`, create the marker); present → `codex exec resume --last`.
STARTED_MARKER = f"{CODEX_HOME}/.cyberarena_started"

# Env var the config.toml `env_key` points at for the API key.
API_KEY_ENV = "CODEX_API_KEY"
# PINNED codex CLI version. Installed here AND baked in dockerfiles/agent/Dockerfile
# (keep the two in sync). Still pinned to a CONCRETE version, never the floating
# `latest` tag, so a run is reproducible.
#
# Bumped 0.135.0 -> 0.146.0 (2026-08-04), accepting the issue-#62 risk. What that
# risk concretely is on 0.146.0: `gpt-5.3-codex` is NO LONGER a model slug — in the
# shipped binary it survives only inside a fallback message ("routed to gpt-5.2 as a
# fallback ... apply for trusted access"). 0.146.0's model table is gpt-5.6-{sol,
# terra,luna} / gpt-5.5 / gpt-5.4. Configs still asking for `gpt-5.3-codex` are the
# exposure: #62's symptom was one empty turn then exit -> async give-up.
CODEX_VERSION = "0.146.0"
# The model_provider id written into config.toml. Arbitrary label; the
# real routing is base_url + wire_api below it.
PROVIDER_ID = "arena"
# MCP server name as the agent sees it (matches the cyberarena server).
MCP_SERVER_NAME = "cyberarena"
# The optional second MCP server: a LOCAL stdio process (baked into the agent image) that
# exposes the security scanner as a tool. Gated by `agent.security_scanner` in game.yaml.
SCANNER_SERVER_NAME = "arena_scanner"
SCANNER_MCP_PATH = "/usr/local/bin/codex-security-mcp"

# Arena-env fallback: when no router `api_key` is configured, reuse the host's
# Codex login (e.g. ChatGPT OAuth in ~/.codex/auth.json) by copying it into the
# container's CODEX_HOME and letting codex use its built-in `openai` provider.
# The router path (api_key set) is unaffected.
HOST_CODEX_AUTH = os.path.expanduser("~/.codex/auth.json")
CODEX_AUTH_PATH = f"{CODEX_HOME}/auth.json"

OnInstalled = Callable[[str], None]


def _toml_str(s: str) -> str:
    """Quote a string as a TOML basic string (escape `\\` and `"`)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class CodexAgentManager(AgentManager):
    """Host-side controller for the Codex CLI. One instance per team;
    each method takes the team's dev ContainerHandle. Transport is
    injected (DockerExecBackend by default); tests pass a FakeTransport.
    """

    PROMPT_PATH: ClassVar[str] = PROMPT_PATH
    PHASE_KILL_PATTERN: ClassVar[str] = "codex"
    # `agent.cli` selects this adapter; the command is fixed in
    # phase_command, so only the leading "codex exec" is honored.
    IMAGE: ClassVar[str] = "cyberarena/agent-codex"
    CANONICAL_CLI: ClassVar[str | None] = "codex exec"
    REQUIRED_CLI: ClassVar[str | None] = "codex"

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
        """One-shot setup per team. Idempotent; called once per game.

        Steps:
          1. Make the host layout dirs (+ CODEX_HOME).
          2. Clear the resume marker so a re-install starts a fresh
             Codex session.
          3. Verify the image baked `codex` (installs nothing).
          4. SSH pivot if cfg.own_prod_container_name set.
          5. Write agent.env (CODEX_API_KEY + CODEX_HOME + team vars).
          6. Write $CODEX_HOME/config.toml (provider + MCP server).
          7. Fire on_installed (caller bridges to AGENT_INSTALLED).

        NO process is launched — the first `phase_command` invocation
        (driven by AgentManager.run_phase) starts Codex.
        """
        prov.ensure_dirs(self._client, self._transport, container, extra=(CODEX_HOME,))
        # Fresh session per game: drop any marker left by a prior run.
        prov.exec_sync(
            self._client, self._transport, container,
            ["sh", "-c", f"rm -f {shlex.quote(STARTED_MARKER)}"], check=True,
        )

        if self.REQUIRED_CLI:
            prov.verify_cli(
                self._client, self._transport, container, self.REQUIRED_CLI,
                expected_version=CODEX_VERSION,
            )

        if cfg.own_prod_container_name:
            prov.provision_ssh_pivot(self._client, self._transport, container, cfg)

        # Router path needs a key; without one, fall back to the host's Codex
        # login (copied into CODEX_HOME) so codex uses its built-in provider.
        use_host_login = (not cfg.api_key) and os.path.isfile(HOST_CODEX_AUTH)

        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())
        self._transport.write_file(
            container, CODEX_CONFIG_PATH,
            self._build_config_toml(cfg, use_host_login=use_host_login).encode(),
        )
        prov.exec_sync(
            self._client, self._transport, container,
            ["chmod", "600", CODEX_CONFIG_PATH], check=True,
        )
        if use_host_login:
            with open(HOST_CODEX_AUTH, "rb") as f:
                self._transport.write_file(container, CODEX_AUTH_PATH, f.read())
            prov.exec_sync(
                self._client, self._transport, container,
                ["chmod", "600", CODEX_AUTH_PATH], check=True,
            )

        log.info(
            "agent installed on %s (cli=%r, auth=%s)",
            container.name, cfg.cli, "host-login" if use_host_login else "api-key",
        )

        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv that drives one phase of Codex.

        Wraps `codex exec` in `bash -c` so we can:
          - cd into WORKDIR (codex's session store + `resume --last` are
            cwd-scoped, keeping this arena's memory isolated)
          - source agent.env (CODEX_API_KEY / CODEX_HOME / CYBERARENA_*)
          - gate exec-vs-resume on STARTED_MARKER so the first phase runs
            `codex exec` and later phases run `codex exec resume --last`
          - pass the staged prompt as a positional arg with stdin closed
            (codex exec otherwise blocks reading stdin to EOF)

        `session_marker` (set by run_phase per-invocation) launches codex
        under `setsid -w bash -c …` with its PID teed to
        `/tmp/cyberarena_phase_<marker>.pid` and `CYBERARENA_PHASE`
        exported, so the on-timeout `kill_session` reaps the whole group
        / any escaped children — identical mechanism to Claude.
        """
        model_arg = f"-m {shlex.quote(model)} " if model else ""
        prompt_q = shlex.quote(self.PROMPT_PATH)
        # `$RESUME` is intentionally unquoted so "resume --last" word-splits.
        codex_argv = (
            "codex exec $RESUME "
            "--json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox "
            + model_arg
            + f'"$(cat {prompt_q})" </dev/null'
        )
        marker_q = shlex.quote(STARTED_MARKER)
        resume_gate = (
            f'if [ -f {marker_q} ]; then RESUME="resume --last"; '
            f'else : > {marker_q}; RESUME=""; fi'
        )

        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            inner = (
                f"{resume_gate} && "
                f"echo $$ > {shlex.quote(pid_file)} && "
                f"export CYBERARENA_PHASE={shlex.quote(session_marker)} && "
                f"exec {codex_argv}"
            )
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = f"{resume_gate} && {codex_argv}"

        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]

    # ── internals ───────────────────────────────────────────────────────

    def _build_env_file(self, cfg: AgentConfig) -> str:
        """agent.env contents: the Codex API-key env var (referenced by
        config.toml's env_key) + CODEX_HOME, then the shared CYBERARENA_*
        / extra_env lines. Model + base URL live in config.toml, not env."""
        lines = [
            f"export {API_KEY_ENV}={shlex.quote(cfg.api_key)}",
            f"export CODEX_HOME={shlex.quote(CODEX_HOME)}",
        ]
        lines.extend(prov.team_env_lines(cfg))
        return "\n".join(lines) + "\n"

    def _build_config_toml(self, cfg: AgentConfig, *, use_host_login: bool = False) -> str:
        """$CODEX_HOME/config.toml. With a router key, defines a custom
        model_provider pointed at cfg.base_url over the Responses API. With
        `use_host_login` (no router key — see install), the custom provider is
        omitted so codex uses its built-in `openai` provider with the copied
        host login. The MCP server block (when cfg.mcp_endpoint is set) is
        written in both modes; the team token rides in as an X-Team-Token
        header from the environment. A second, LOCAL stdio MCP server (the
        scanner) is appended when `cfg.security_scanner` is set."""
        out: list[str] = []
        if cfg.model:
            out.append(f"model = {_toml_str(cfg.model)}")
        # orche #95: codex-cli enables its provider-hosted `web_search`
        # tool by default, and under
        # `--dangerously-bypass-approvals-and-sandbox`
        # (PermissionProfile::Disabled) it resolves to *live* external
        # web access. That search executes server-side on the model
        # provider and rides the NO_PROXY'd LLM-gateway channel, so it
        # never traverses the container egress path or the
        # api-router:3128 default-deny firewall -- no egress log,
        # invisible to the arena's network isolation. Force it off so any
        # web access must go client-side through the container shell and
        # hit the egress proxy. `disabled` makes codex omit the
        # web_search tool from the Responses request entirely (verified
        # against codex-cli 0.135.0: hosted_spec.create_web_search_tool
        # returns None for WebSearchMode::Disabled, and
        # resolve_web_search_mode_for_turn keeps Disabled even under the
        # danger-full-access permission profile). Applies in both router
        # and host-login modes -- web_search is provider-hosted either
        # way.
        out.append('web_search = "disabled"')
        if not use_host_login:
            out.append(f"model_provider = {_toml_str(PROVIDER_ID)}")
            out.append("")
            out.append(f"[model_providers.{PROVIDER_ID}]")
            out.append(f"name = {_toml_str(PROVIDER_ID)}")
            out.append(f"base_url = {_toml_str(cfg.base_url)}")
            # Responses API: gpt-5.x + gpt-5.3-codex route through it on the
            # ppapi router (chat-completions rejects the codex model).
            out.append('wire_api = "responses"')
            out.append(f"env_key = {_toml_str(API_KEY_ENV)}")

        if cfg.mcp_endpoint:
            out.append("")
            out.append(f"[mcp_servers.{MCP_SERVER_NAME}]")
            out.append(f"url = {_toml_str(cfg.mcp_endpoint)}")
            out.append("")
            out.append(f"[mcp_servers.{MCP_SERVER_NAME}.env_http_headers]")
            # Value is the NAME of the env var holding the token (codex
            # reads it at call time), not the token itself.
            out.append('X-Team-Token = "CYBERARENA_TEAM_TOKEN"')

        if cfg.security_scanner:
            # A SECOND MCP server, this one local and stdio: the scanner reaches the model
            # as an advertised tool rather than as a binary nothing announces. Codex merges
            # its tool list into the same request schema as the arena's tools, so discovery
            # is automatic and every call lands in the trajectory as `mcp_tool_call` —
            # countable, unlike a shell invocation that collapses into `command_execution`.
            # Off unless `agent.security_scanner: true`, so existing games are unchanged.
            out.append("")
            out.append(f"[mcp_servers.{SCANNER_SERVER_NAME}]")
            out.append('command = "python3"')
            out.append(f"args = [{_toml_str(SCANNER_MCP_PATH)}]")
            # A stdio MCP child does NOT inherit the agent's environment: codex hands it
            # exactly HOME, LC_CTYPE and PATH (measured against 0.146.0). Without this the
            # server would shell out to the routed `codex-security` wrapper with no credential
            # and no way to find the router, and it would refuse on every call — a tool
            # advertised to the model that then fails 100% of the time. `env_vars` forwards by
            # NAME, so the principal token keeps living in agent.env and is not copied into
            # config.toml. CODEX_HOME is what lets the wrapper read base_url out of the
            # provider block above.
            out.append(f'env_vars = [{_toml_str(API_KEY_ENV)}, "CODEX_HOME"]')

        return "\n".join(out) + "\n"
