"""GeminiAgentManager — official Gemini CLI adapter for the AgentManager ABC.

The official Google Gemini CLI (`@google/gemini-cli`, binary `gemini`) supports
headless prompts (`-p/--prompt`), MCP servers via `~/.gemini/settings.json`, and
session resume (`--resume`). That lets it fit the same per-phase model as Claude,
Codex, and Qwen.

Two responsibilities, same shape as the other adapters:
  install(handle, cfg)        # per team: verify the baked CLI, ssh pivot, write
                              # ~/.gemini/settings.json + gemini.env.
  phase_command(model=None)   # `gemini -p <prompt> ...` with create-vs-resume gate.

Gemini specifics (verified against `gemini --help`, live probes, and the
installed `@google/gemini-cli` bundle):
  * LLM auth — `GEMINI_API_KEY` supplies the key. Even with a custom
    `GOOGLE_GEMINI_BASE_URL`, Gemini CLI 0.58.0's non-interactive auth
    validator still accepts `gemini-api-key`, while forcing `gateway` can make
    startup fail fast with exit 41 before any request is sent. Keep
    `security.auth.selectedType = "gemini-api-key"` so headless runs stay on
    the proven API-key path, and use `GOOGLE_GEMINI_BASE_URL` only to retarget
    the HTTP endpoint.
  * MCP — one `~/.gemini/settings.json` with `mcpServers.cyberarena.httpUrl`,
    `headers`, `timeout`, and `trust`. Gemini flattens those MCP tools to
    `mcp_<server>_<tool>`, so prompts need a provider-specific branch.
  * Model selection — set the model in all three supported places:
    `--model`, `GEMINI_MODEL`, and `settings.model.name`. The CLI still follows
    its documented precedence, but the persisted setting makes the requested
    model visible in artifacts instead of looking "unset".
  * Resume — the first invocation creates a deterministic session with
    `--session-id <uuid>`; later ones resume it with `--resume latest`, gated on a
    marker file under the wiped-per-game HOME. Using `latest` avoids depending on
    a second code path for explicit session-id resolution.
  * Prompt — Gemini headless mode needs `-p/--prompt`; a bare positional prompt
    enters interactive mode. We read the prompt from PROMPT_PATH and close stdin.

Host scaffolding (dir layout, CLI preflight, ssh pivot, team env) comes from
agents/_provisioning.py.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from typing import Callable, ClassVar
from urllib.parse import urlsplit, urlunsplit

import docker

from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle

from . import _provisioning as prov
from ._provisioning import PROMPT_PATH, STATE_DIR, WORKDIR
from .manager import AgentManager
from .transport import AgentTransport
from .transport_docker import DockerExecBackend

log = logging.getLogger(__name__)

# Gemini-local host paths. HOME is pinned under STATE_DIR so settings + sessions are
# scoped to one arena and wiped fresh on install(). GEMINI_CLI_HOME is the logical
# home root itself; Gemini then resolves settings under "$GEMINI_CLI_HOME/.gemini".
GEMINI_HOME = f"{STATE_DIR}/gemini-home"
GEMINI_DIR = f"{GEMINI_HOME}/.gemini"
SETTINGS_PATH = f"{GEMINI_DIR}/settings.json"
ENV_PATH = f"{STATE_DIR}/gemini.env"

# Fixed per-match session id (valid UUID). HOME is wiped every install, so this
# starts fresh on phase 1 and remains unique within the session store.
SESSION_ID = "a1b2c3d4-0000-4000-8000-000000000002"
STARTED_MARKER = f"{GEMINI_DIR}/.cyberarena_started"

# PINNED — keep in sync with dockerfiles/agent/Dockerfile.
GEMINI_VERSION = "0.58.0"
NPM_PACKAGE = "@google/gemini-cli"
MCP_SERVER_NAME = "cyberarena"

# The highest gemini generation the stock CLI (GEMINI_VERSION) natively serves.
# A NEWER gemini tag (3.6+, 4.x) is unknown to the stock CLI: its resolveModel()
# treats any "*-flash" string as the flash tier and silently collapses it to
# DEFAULT_GEMINI_3_5_FLASH_MODEL ("gemini-3.5-flash") on the wire — so the match
# runs on 3.5 while everything is recorded under the requested tag. We refuse such
# a config at install() rather than produce a mislabeled run. Bump with GEMINI_VERSION.
GEMINI_MAX_SUPPORTED_VERSION = (3, 5)

# Newer-than-GA-flash tags our image explicitly registers into the from-source CLI
# build so it emits them verbatim (dockerfiles/agent/gemini/{Dockerfile,models_patch.py}).
# These are allowed through the guard below; everything else above the max is refused.
# Keep in sync with CYBERARENA_EXTRA_FLASH_MODELS in models_patch.py.
GEMINI_REGISTERED_EXTRA_MODELS = frozenset({"gemini-3.7-flash", "gemini-3.8-flash"})


def _gemini_model_version(model: str) -> tuple[int, int] | None:
    """(major, minor) parsed from a `gemini-<major>[.<minor>]-…` tag (minor
    defaults to 0), or None if `model` is not a versioned gemini-* tag."""
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?", model.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0))


OnInstalled = Callable[[str], None]


class GeminiAgentManager(AgentManager):
    """Host-side controller for the official Gemini CLI."""

    PROMPT_PATH: ClassVar[str] = PROMPT_PATH
    PHASE_KILL_PATTERN: ClassVar[str] = "gemini -p"
    IMAGE: ClassVar[str] = "cyberarena/agent-gemini"
    CANONICAL_CLI: ClassVar[str | None] = "gemini"
    REQUIRED_CLI: ClassVar[str | None] = "gemini"

    def __init__(
        self,
        *,
        transport: AgentTransport | None = None,
        client: docker.DockerClient | None = None,
    ) -> None:
        if transport is not None:
            self._transport = transport
            self._client = client
        else:
            self._client = client or docker.from_env()
            self._transport = DockerExecBackend(client=self._client)

    def install(
        self,
        container: ContainerHandle,
        cfg: AgentConfig,
        *,
        on_installed: OnInstalled | None = None,
    ) -> None:
        """One-shot setup per team: make dirs, clear stale HOME/session state,
        verify the baked CLI, stage ssh pivot, then write settings + env."""
        version = _gemini_model_version(cfg.model or "")
        if (version is not None and version > GEMINI_MAX_SUPPORTED_VERSION
                and cfg.model not in GEMINI_REGISTERED_EXTRA_MODELS):
            raise ValueError(
                f"gemini model {cfg.model!r} is newer than the pinned gemini-cli "
                f"({GEMINI_VERSION}) supports (max gemini-3.5-*) and is not registered "
                f"in the from-source build. The stock CLI does not recognize this tag "
                f"and would SILENTLY downgrade to gemini-3.5-flash on the wire, "
                f"recording the run under a model that never executed. Refusing to "
                f"start. Use gemini-3.5-flash or older, register the tag in the "
                f"from-source CLI build (dockerfiles/agent/gemini/models_patch.py + "
                f"GEMINI_REGISTERED_EXTRA_MODELS), or drive this model through a "
                f"different harness."
            )
        prov.ensure_dirs(
            self._client, self._transport, container, extra=(GEMINI_HOME, GEMINI_DIR),
        )
        prov.exec_sync(
            self._client, self._transport, container,
            ["sh", "-c", f"rm -rf {shlex.quote(GEMINI_HOME)} && mkdir -p {shlex.quote(GEMINI_DIR)}"],
            check=True,
        )

        prov.verify_cli(
            self._client, self._transport, container, self.REQUIRED_CLI,
            expected_version=GEMINI_VERSION,
        )

        if cfg.own_prod_container_name:
            prov.provision_ssh_pivot(self._client, self._transport, container, cfg)

        self._write_settings(container, cfg)
        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())

        if not cfg.model:
            log.warning(
                "gemini installed on %s with EMPTY model — headless runs will use the "
                "CLI default; set agent.model in game.yaml for reproducibility",
                container.name,
            )

        log.info("gemini installed on %s (cli=%r, model=%r)",
                 container.name, cfg.cli, cfg.model)

        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv driving one Gemini phase: source gemini.env, choose
        create-vs-resume, then run `gemini -p <prompt> --yolo` headlessly."""
        model_arg = (
            f"-m {shlex.quote(model)} "
            if model
            else '-m "$GEMINI_MODEL" '
        )
        prompt_q = shlex.quote(self.PROMPT_PATH)
        marker_q = shlex.quote(STARTED_MARKER)
        sid_q = shlex.quote(SESSION_ID)
        session_gate = (
            f'if [ -f {marker_q} ]; then SESS="--resume latest"; '
            f'else : > {marker_q}; SESS="--session-id {sid_q}"; fi'
        )
        gemini_argv = (
            "gemini "
            "$SESS "
            "--yolo --skip-trust "
            "--output-format stream-json "
            + model_arg
            + f'-p "$(cat {prompt_q})" < /dev/null'
        )
        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            inner = (
                f"{session_gate} && "
                f"echo $$ > {shlex.quote(pid_file)} && "
                f"export CYBERARENA_PHASE={shlex.quote(session_marker)} && "
                f"exec {gemini_argv}"
            )
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = f"{session_gate} && exec {gemini_argv}"
        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]

    def _write_settings(self, container: ContainerHandle, cfg: AgentConfig) -> None:
        settings: dict = {
            "security": {
                "auth": {
                    "selectedType": "gemini-api-key",
                }
            },
        }
        if cfg.model:
            settings["model"] = {"name": cfg.model}
        if cfg.mcp_endpoint:
            settings["mcpServers"] = {
                MCP_SERVER_NAME: {
                    "httpUrl": cfg.mcp_endpoint,
                    "headers": {"X-Team-Token": cfg.team_token},
                    "timeout": 30000,
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
        base_url = self._gemini_base_url(cfg.base_url)
        lines = [
            f"export HOME={shlex.quote(GEMINI_HOME)}",
            f"export GEMINI_CLI_HOME={shlex.quote(GEMINI_HOME)}",
            f"export GEMINI_API_KEY={shlex.quote(cfg.api_key)}",
            "export GEMINI_CLI_TRUST_WORKSPACE=true",
        ]
        if base_url:
            lines.append(f"export GOOGLE_GEMINI_BASE_URL={shlex.quote(base_url)}")
        if cfg.model:
            lines.append(f"export GEMINI_MODEL={shlex.quote(cfg.model)}")
        lines.extend(prov.team_env_lines(cfg))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _gemini_base_url(base_url: str) -> str:
        """Gemini native endpoints expect the API root, not OpenAI-compatible `/v1`.

        Existing arena configs often reuse `http://api-router:8080/v1`; accept that
        copy-paste by stripping a terminal `/v1` or `/v1beta` and leaving everything
        else untouched.
        """
        if not base_url:
            return base_url
        parts = urlsplit(base_url)
        path = parts.path.rstrip("/")
        if path not in ("/v1", "/v1beta"):
            return base_url
        return urlunsplit((parts.scheme, parts.netloc, "", parts.query, parts.fragment))
