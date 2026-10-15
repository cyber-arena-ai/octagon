"""KimiAgentManager — Moonshot Kimi Code CLI adapter for the AgentManager ABC.

kimi-cli (https://moonshotai.github.io/kimi-cli/) is Moonshot's terminal
agentic coding agent for the Kimi K2/K3 models. It matters here because
kimi-k3 speaks ONLY OpenAI chat/completions — not the Responses API (codex)
nor Anthropic messages (claude) — so codex/claude can't drive it, and Codex no longer exposes a Chat Completions wire. kimi-cli is the
model's NATIVE harness, so it's the clean way to run kimi in the arena.

Same shape as the Claude/Codex adapters:
  install(handle, cfg)        # per team: verify baked kimi, clear session,
                              # write config.toml + mcp.json + agent.env.
  phase_command(model=None)   # `kimi --print --output-format stream-json …`
                              # consuming the prompt from PROMPT_PATH.

kimi specifics (verified against kimi-cli 1.49.0 in the cyberarena/agent
image, 2026-07-17):

  * Install — the pinned PyPI package is baked once in the agent image. The
    manager verifies the binary/version and performs no runtime network install.
  * Endpoint — a custom provider in config.toml points at cfg.base_url (the
    api-router), which forwards verbatim to ppapi. The provider `type` is chosen
    per model by `_wire_type_for`: openai_legacy (chat/completions) for kimi-k3,
    openai_responses for codex-family models, anthropic for claude/opus. This is
    what lets the ONE kimi harness host `codex-5.3 vs opus-4.8`. All three types
    are native to kimi-cli 1.49.0 (see `kimi_cli/llm.py`); none injects a
    provider-hosted web_search tool (see the egress note on _build_config_toml).
  * Model block — `[models.<model>]` REQUIRES `max_context_size` (pydantic
    rejects the config without it); see KIMI_MAX_CONTEXT.
  * MCP — a separate `--mcp-config-file` (Claude-desktop-shaped mcpServers
    JSON) carries the team token in an `X-Team-Token` header. kimi HARD-FAILS
    at startup if a configured MCP server is unreachable, so the mcp block is
    written only when cfg.mcp_endpoint is set, and the flag is gated on the
    file existing.
  * Memory across phases — `kimi -C` continues the previous session for the
    working directory (like claude --continue / codex resume --last). The
    first phase has no session, so a marker file gates continue-vs-fresh.
    NOTE: `-C` is --continue but `-c` is an alias for --prompt (opposite of
    claude/codex) — this adapter uses the capital `-C`.
  * Non-interactive — `--print --output-format stream-json --yolo --afk`:
    print mode + streaming JSONL to stdout (parsed for the live panel like
    claude stream-json / codex --json), auto-approve tools, no user present.
  * Prompt — passed via `-p "$(cat prompt.txt)"` with stdin closed.
  * Usage — kimi --print emits NO usage/token events in the stream; per-team
    token/cost come from the api-router's usage-router.jsonl metering, same as
    every other adapter.
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
from ._provisioning import ENV_PATH, PROMPT_PATH, STATE_DIR, WORKDIR
from .manager import AgentManager
from .transport import AgentTransport
from .transport_docker import DockerExecBackend

log = logging.getLogger(__name__)

# kimi-local host paths (kept under STATE_DIR with the rest of the arena's
# agent state). kimi's own session/log store lives under $HOME/.kimi; -C
# resumes the working-directory session, which stays isolated because every
# phase runs with the same -w WORKDIR.
KIMI_STATE = f"{STATE_DIR}/kimi"
# kimi-cli keeps resumable sessions under the container root home independently
# of --config-file. The agent image runs as root, so install must clear this too.
KIMI_SESSION_HOME = "/root/.kimi"
KIMI_CONFIG_PATH = f"{KIMI_STATE}/config.toml"
KIMI_MCP_PATH = f"{KIMI_STATE}/mcp.json"
# Gates continue-vs-fresh in phase_command: absent → first phase (no -C,
# create the marker); present → `kimi -C` continues the session.
STARTED_MARKER = f"{KIMI_STATE}/.cyberarena_started"

# The custom provider id in config.toml; the model's `[models.<m>].provider`
# points at it. Arbitrary label — real routing is base_url below it.
PROVIDER_ID = "cyberarena"
# MCP server name as the agent sees it (matches the cyberarena server).
MCP_SERVER_NAME = "cyberarena"
# PyPI package shipping the `kimi` entrypoint. Installed ONLY by the agent
# Dockerfile; install() verifies the baked binary and version, with no runtime
# network dependency. Keep this pin synchronized with that Dockerfile.
KIMI_VERSION = "1.49.0"
PIP_PACKAGE = f"kimi-cli=={KIMI_VERSION}"
# `[models.<m>].max_context_size` is REQUIRED by kimi-cli's config schema.
# It's kimi's client-side context-management threshold, not an API limit.
# kimi-k3's official window is 1M tokens; an arena match never approaches it,
# but this matches the model's real spec. Adjust for a model with a smaller window.
KIMI_MAX_CONTEXT = 1_000_000

# kimi-cli provider `type` values this adapter emits. kimi-cli natively supports
# all three (verified against kimi-cli 1.49.0 `kimi_cli/llm.py`): openai_legacy →
# chat/completions, openai_responses → the OpenAI Responses API, anthropic →
# Anthropic messages. Which one a match needs depends on the MODEL being driven,
# not on kimi-cli: kimi-k3 speaks chat/completions; codex-family models are
# Responses-API-only; claude/opus models speak Anthropic messages. This lets the
# ONE kimi harness host codex-5.3 vs opus-4.8 (each side just gets the right wire).
WIRE_LEGACY = "openai_legacy"
WIRE_RESPONSES = "openai_responses"
WIRE_ANTHROPIC = "anthropic"
_VALID_WIRES = (WIRE_LEGACY, WIRE_RESPONSES, WIRE_ANTHROPIC)
# Optional explicit override in game.yaml `agent.extra_env` for a model name whose
# wire type can't be inferred; must be one of _VALID_WIRES. Inference is used when
# unset (the codex/opus/kimi names in scope here are unambiguous).
WIRE_OVERRIDE_ENV = "KIMI_WIRE_API"

OnInstalled = Callable[[str], None]


def _wire_type_for(cfg: AgentConfig) -> str:
    """Pick the kimi-cli provider `type` for the model this team drives.

    An explicit `extra_env["KIMI_WIRE_API"]` wins (for a genuinely ambiguous
    model); otherwise infer from the model name:
      * codex-family / gpt-5.x / o-series  → openai_responses (Responses-only)
      * claude / opus / sonnet / haiku     → anthropic
      * everything else (kimi-k3, qwen, …) → openai_legacy (chat/completions)
    Inference is deliberately conservative: an unrecognized model falls back to
    openai_legacy (kimi-k3's native wire), matching the pre-existing behaviour."""
    override = (cfg.extra_env or {}).get(WIRE_OVERRIDE_ENV, "").strip().lower()
    if override:
        if override not in _VALID_WIRES:
            raise ValueError(
                f"{WIRE_OVERRIDE_ENV}={override!r} is not a valid kimi provider type "
                f"(expected one of {_VALID_WIRES})"
            )
        return override
    m = (cfg.model or "").strip().lower()
    if "codex" in m or m.startswith(("gpt-5", "gpt5", "o1", "o3", "o4")):
        return WIRE_RESPONSES
    if any(tag in m for tag in ("claude", "opus", "sonnet", "haiku")):
        return WIRE_ANTHROPIC
    return WIRE_LEGACY


def _toml_str(s: str) -> str:
    """Quote a string as a TOML basic string (escape `\\` and `"`)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class KimiAgentManager(AgentManager):
    """Host-side controller for the Kimi CLI. One instance per team; each
    method takes the team's dev ContainerHandle. Transport is injected
    (DockerExecBackend by default); tests pass a FakeTransport."""

    PROMPT_PATH: ClassVar[str] = PROMPT_PATH
    PHASE_KILL_PATTERN: ClassVar[str] = "kimi"
    # `agent.cli` selects this adapter; the command is fixed in phase_command,
    # so only the leading "kimi" is honored.
    IMAGE: ClassVar[str] = "cyberarena/agent-kimi"
    CANONICAL_CLI: ClassVar[str | None] = "kimi"
    REQUIRED_CLI: ClassVar[str | None] = "kimi"

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
          1. Make the host layout dirs (+ KIMI_STATE).
          2. Verify the image-baked kimi CLI and pinned version.
          3. Clear the full Kimi state so a re-install starts fresh.
          4. SSH pivot if cfg.own_prod_container_name set.
          5. Write agent.env (team vars; kimi reads the key from config.toml).
          6. Write config.toml (provider + model) and, when cfg.mcp_endpoint is
             set, mcp.json (remote MCP + X-Team-Token header).
          7. Fire on_installed (caller bridges to AGENT_INSTALLED).

        NO process is launched — the first phase_command starts kimi.
        """
        if not (cfg.model or "").strip():
            # Fail loud at install, not silently mid-match: an empty model means
            # _build_config_toml falls back to "kimi-k3". Surface it now (qwen parity)
            # so a mis-configured match doesn't look like the model just did nothing.
            log.warning("no model set on %s; kimi will default to 'kimi-k3'", container.name)

        prov.ensure_dirs(self._client, self._transport, container, extra=(KIMI_STATE,))
        if self.REQUIRED_CLI:
            prov.verify_cli(
                self._client, self._transport, container, self.REQUIRED_CLI,
                expected_version=KIMI_VERSION,
            )
        # Wipe both arena-owned config/marker state and kimi-cli resume history.
        # A reused container must never continue a previous game. Re-create only
        # KIMI_STATE; kimi-cli lazily creates its own session home.
        prov.exec_sync(
            self._client, self._transport, container,
            ["sh", "-c",
             f"rm -rf {shlex.quote(KIMI_STATE)} {shlex.quote(KIMI_SESSION_HOME)} "
             f"&& mkdir -p {shlex.quote(KIMI_STATE)}"],
            check=True,
        )

        if cfg.own_prod_container_name:
            prov.provision_ssh_pivot(self._client, self._transport, container, cfg)

        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())
        self._transport.write_file(
            container, KIMI_CONFIG_PATH, self._build_config_toml(cfg).encode(),
        )
        prov.exec_sync(
            self._client, self._transport, container,
            ["chmod", "600", KIMI_CONFIG_PATH], check=True,
        )
        if cfg.mcp_endpoint:
            self._transport.write_file(
                container, KIMI_MCP_PATH, self._build_mcp_json(cfg).encode(),
            )
            prov.exec_sync(
                self._client, self._transport, container,
                ["chmod", "600", KIMI_MCP_PATH], check=True,
            )

        log.info("agent installed on %s (cli=%r, model=%r)",
                 container.name, cfg.cli, cfg.model)

        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv that drives one phase of kimi.

        Wraps `kimi … --print --output-format stream-json` in `bash -c` so we
        can cd into WORKDIR, source agent.env, gate continue-vs-fresh on
        STARTED_MARKER (first phase runs without -C, later phases with -C to
        resume the prior session), attach the MCP config file when present, and
        pass the staged prompt as `-p "$(cat prompt.txt)"` with stdin closed.

        `session_marker` (set by run_phase) launches kimi under `setsid -w bash
        -c …` with its PID teed to `/tmp/cyberarena_phase_<marker>.pid` and
        `CYBERARENA_PHASE` exported, so on-timeout kill_session reaps the whole
        group — identical mechanism to Claude/Codex.
        """
        model_arg = f"-m {shlex.quote(model)} " if model else ""
        prompt_q = shlex.quote(self.PROMPT_PATH)
        cfg_q = shlex.quote(KIMI_CONFIG_PATH)
        mcp_q = shlex.quote(KIMI_MCP_PATH)
        marker_q = shlex.quote(STARTED_MARKER)
        # `$CONTINUE` / `$MCP` are intentionally unquoted so they word-split
        # (empty → dropped, "-C" / "--mcp-config-file <path>" → passed).
        kimi_argv = (
            f"kimi -w {shlex.quote(WORKDIR)} "
            f"--config-file {cfg_q} $MCP "
            f"--print --output-format stream-json --yolo --afk "
            f"$CONTINUE "
            + model_arg
            + f'-p "$(cat {prompt_q})" </dev/null'
        )
        gate = (
            f'if [ -f {marker_q} ]; then CONTINUE="-C"; '
            f'else : > {marker_q}; CONTINUE=""; fi; '
            f'if [ -f {mcp_q} ]; then MCP="--mcp-config-file {mcp_q}"; else MCP=""; fi'
        )

        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            inner = (
                f"{gate} && "
                f"echo $$ > {shlex.quote(pid_file)} && "
                f"export CYBERARENA_PHASE={shlex.quote(session_marker)} && "
                f"exec {kimi_argv}"
            )
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = f"{gate} && {kimi_argv}"

        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]

    # ── internals ───────────────────────────────────────────────────────

    def _build_env_file(self, cfg: AgentConfig) -> str:
        """agent.env: just the shared CYBERARENA_* / extra_env lines. kimi
        reads its API key + model + base URL from config.toml, not the env, so
        (unlike codex) no API-key env var is exported here."""
        return "\n".join(prov.team_env_lines(cfg)) + "\n"

    def _build_config_toml(self, cfg: AgentConfig) -> str:
        """~/.kimi-style config.toml: a custom provider pointed at cfg.base_url,
        plus the model block (provider ref + required max_context_size). The
        provider `type` is chosen per model by `_wire_type_for` so the one kimi
        harness can host chat/completions (kimi-k3), Responses (codex), or
        Anthropic (opus) models. base_url is used verbatim, exactly like the
        sibling codex/claude adapters — game.yaml sets the per-team base_url to
        match the wire (e.g. keep the `/v1` suffix for the OpenAI wires; the
        Anthropic SDK appends `/v1/messages`, matching the claude adapter). The
        api_key is the per-team principal label (the router substitutes the real
        upstream key), same as codex/opencode.

        SECURITY NOTE (egress containment, #92–#95 class). The Responses wire is
        the path that let codex opt into a provider-HOSTED `web_search` tool,
        whose fetch runs on the provider's servers and bypasses the container's
        `:3128` egress firewall. kimi-cli does NOT re-open that hole: via kosong's
        OpenAIResponses it sends ONLY client-side `{"type":"function"}` tools
        (verified in kosong 0.56.0 `_convert_tool`) — it never injects
        web_search/computer_use/etc. It also configures no kimi-cli `services.*`
        (moonshot_search/fetch), so kimi has no built-in web tool. Any web/fetch
        the model attempts is therefore a client-side tool contained by the
        firewall. The apirouter `strip_hosted_tools` guard (enabled for this
        harness in game.yaml) enforces this at the wire as defense-in-depth."""
        model = cfg.model or "kimi-k3"
        wire = _wire_type_for(cfg)
        out = [
            f"[providers.{PROVIDER_ID}]",
            f"type = {_toml_str(wire)}",
            f"base_url = {_toml_str(cfg.base_url)}",
            f"api_key = {_toml_str(cfg.api_key or 'sk-local')}",
            "",
            f"[models.{_toml_str(model)}]",   # quote: model ids contain dots (gpt-5.3-codex) which TOML would treat as key separators
            f"provider = {_toml_str(PROVIDER_ID)}",
            f"model = {_toml_str(model)}",
            f"max_context_size = {KIMI_MAX_CONTEXT}",
        ]
        return "\n".join(out) + "\n"

    def _build_mcp_json(self, cfg: AgentConfig) -> str:
        """mcp.json (Claude-desktop-shaped): the remote cyberarena MCP server
        carrying the team token in an X-Team-Token header."""
        config = {
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "url": cfg.mcp_endpoint,
                    "headers": {"X-Team-Token": cfg.team_token},
                }
            }
        }
        return json.dumps(config, indent=2) + "\n"
