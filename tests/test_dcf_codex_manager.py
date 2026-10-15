"""Unit tests for CodexAgentManager — config.toml / agent.env rendering,
phase_command argv (exec-vs-resume gate, setsid wrapping), provider-aware
MCP tool naming, and registry resolution. Pure unit; no docker."""

from __future__ import annotations

import pytest

from agents.codex import (
    API_KEY_ENV,
    CODEX_CONFIG_PATH,
    CODEX_HOME,
    STARTED_MARKER,
    CodexAgentManager,
)
from agents.prompts._helpers import mcp_tool
from agents.registry import build
from models.agent import AgentConfig


def _cfg(**over) -> AgentConfig:
    base = dict(
        cli="codex exec", provider="codex", model="gpt-5.4",
        api_key="sk-TEST", base_url="https://app-us.ppapi.ai/v1",
        team_id="team1", team_token="dev-team1-token",
        own_prod_host="team1_prod", own_prod_container_name="team1_prod",
        opposing_prod_hosts=("team2_prod",),
        submit_url="http://host.docker.internal:8000/game/flag/submit",
        mcp_endpoint="http://host.docker.internal:8000/mcp/",
    )
    base.update(over)
    return AgentConfig(**base)


def _mgr() -> CodexAgentManager:
    # transport/client unused by the pure-render methods under test.
    return CodexAgentManager(transport=object(), client=None)


# ── registry ───────────────────────────────────────────────────────────


def test_registry_resolves_codex_by_provider():
    assert isinstance(build(_cfg()), CodexAgentManager)


def test_registry_resolves_codex_by_cli_token():
    # provider unset → inferred from the leading cli token
    assert isinstance(build(_cfg(provider="")), CodexAgentManager)


# ── config.toml ────────────────────────────────────────────────────────


def test_config_toml_responses_provider():
    toml = _mgr()._build_config_toml(_cfg())
    assert 'model = "gpt-5.4"' in toml
    assert 'model_provider = "arena"' in toml
    assert 'base_url = "https://app-us.ppapi.ai/v1"' in toml
    assert 'wire_api = "responses"' in toml          # gpt-5.x via Responses API
    assert f'env_key = "{API_KEY_ENV}"' in toml      # key comes from env, not inline


def test_config_toml_disables_server_side_web_search():
    # orche #95: codex's provider-hosted web_search runs server-side and
    # bypasses the arena egress firewall. The adapter must force it off so
    # web access is only reachable client-side through the container shell.
    # `web_search = "disabled"` makes codex-cli omit the web_search tool
    # from the Responses request (create_web_search_tool -> None), even
    # under --dangerously-bypass-approvals-and-sandbox.
    toml = _mgr()._build_config_toml(_cfg())
    assert 'web_search = "disabled"' in toml
    # provider-hosted either way, so it must also be off in host-login mode
    toml_host = _mgr()._build_config_toml(_cfg(), use_host_login=True)
    assert 'web_search = "disabled"' in toml_host


def test_config_toml_mcp_server_carries_x_team_token():
    toml = _mgr()._build_config_toml(_cfg())
    assert "[mcp_servers.cyberarena]" in toml
    assert 'url = "http://host.docker.internal:8000/mcp/"' in toml
    # X-Team-Token sourced from an env var (codex env_http_headers), so the
    # orchestrator's existing X-Team-Token auth needs no change.
    assert "[mcp_servers.cyberarena.env_http_headers]" in toml
    assert 'X-Team-Token = "CYBERARENA_TEAM_TOKEN"' in toml
    # the literal token must never appear in the config file
    assert "dev-team1-token" not in toml


def test_config_toml_omits_mcp_without_endpoint():
    toml = _mgr()._build_config_toml(_cfg(mcp_endpoint=""))
    assert "[mcp_servers" not in toml


# ── agent.env ──────────────────────────────────────────────────────────


def test_env_file_exports_key_home_and_team_vars():
    env = _mgr()._build_env_file(_cfg())
    assert f"export {API_KEY_ENV}=sk-TEST" in env
    assert f"export CODEX_HOME={CODEX_HOME}" in env
    assert "export CYBERARENA_TEAM_TOKEN=dev-team1-token" in env  # read by env_http_headers
    assert "export CYBERARENA_TEAM_ID=team1" in env
    # Claude-specific vars must NOT leak into the codex env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_MODEL" not in env


# ── phase_command ──────────────────────────────────────────────────────


def test_phase_command_resume_gate_and_flags():
    argv = _mgr().phase_command(model="gpt-5.4")
    assert argv[0] == "bash" and argv[1] == "-c"
    script = argv[2]
    # exec-vs-resume gate keyed on the marker file
    assert STARTED_MARKER in script
    assert 'RESUME="resume --last"' in script
    assert "codex exec $RESUME" in script
    # unattended, non-git, streaming
    assert "--skip-git-repo-check" in script
    assert "--dangerously-bypass-approvals-and-sandbox" in script
    assert "--json" in script
    assert "-m gpt-5.4" in script
    # prompt staged on disk, passed as arg, stdin closed
    assert 'cat ' in script and "prompt.txt" in script
    assert "</dev/null" in script


def test_phase_command_session_marker_wraps_setsid():
    argv = _mgr().phase_command(model="gpt-5.4", session_marker="abc123")
    script = argv[2]
    assert "setsid -w bash -c" in script
    assert "/tmp/cyberarena_phase_abc123.pid" in script
    assert "export CYBERARENA_PHASE=abc123" in script


def test_constants():
    m = _mgr()
    assert m.PHASE_KILL_PATTERN == "codex"
    assert m.PROMPT_PATH.endswith("prompt.txt")
    assert CODEX_CONFIG_PATH.startswith(CODEX_HOME)


# ── provider-aware MCP tool naming ─────────────────────────────────────


def test_mcp_tool_naming_per_provider():
    codex = _cfg(provider="codex")
    claude = _cfg(provider="claude")
    opencode = _cfg(provider="opencode")
    assert mcp_tool(codex, "submit_flag") == "submit_flag"
    assert mcp_tool(claude, "submit_flag") == "mcp__cyberarena__submit_flag"
    # opencode flattens remote MCP tools to `<server>_<tool>`.
    assert mcp_tool(opencode, "submit_flag") == "cyberarena_submit_flag"


# ── the optional scanner MCP server (agent.security_scanner) ───────────


def test_scanner_block_absent_by_default():
    """Off unless asked for: every existing game keeps a byte-identical config.toml,
    and a codex agent is advertised only the arena's own tools."""
    toml = _mgr()._build_config_toml(_cfg())
    assert "arena_scanner" not in toml
    assert "codex-security-mcp" not in toml
    # the arena's own MCP server is still wired
    assert "[mcp_servers.cyberarena]" in toml


def test_scanner_block_is_a_local_stdio_server_when_enabled():
    """`security_scanner: true` adds a SECOND mcp_servers block — a local stdio process,
    not a url — so the scanner is advertised to the model as a tool. Codex's stdio shape is
    `command` + `args` (verified against codex 0.146.0 `codex mcp add`)."""
    toml = _mgr()._build_config_toml(_cfg(security_scanner=True))
    assert "[mcp_servers.arena_scanner]" in toml
    assert 'command = "python3"' in toml
    assert '"/usr/local/bin/codex-security-mcp"' in toml
    # It must not clobber the arena server or the router provider block.
    assert "[mcp_servers.cyberarena]" in toml
    assert "[model_providers.arena]" in toml


def test_scanner_block_survives_host_login_mode():
    """The scanner is orthogonal to how the model is authenticated: with no router key the
    custom provider is dropped, but the scanner tool must still be advertised."""
    toml = _mgr()._build_config_toml(_cfg(security_scanner=True), use_host_login=True)
    assert "[mcp_servers.arena_scanner]" in toml
    assert "[model_providers." not in toml
