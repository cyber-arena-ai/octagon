"""GeminiAgentManager unit tests — the official Gemini CLI adapter.

Pins the headless argv shape, create-vs-resume gate, settings/env rendering, the
Gemini-native MCP tool naming, and registry dispatch. Pure unit; no docker.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from agents.gemini import (
    ENV_PATH,
    GEMINI_HOME,
    GEMINI_VERSION,
    SETTINGS_PATH,
    SESSION_ID,
    STARTED_MARKER,
    GeminiAgentManager,
)
from agents._provisioning import PROMPT_PATH, WORKDIR
from agents.prompts._helpers import mcp_tool
from agents.transport import AgentTransport, CommandResult
from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle


@dataclass
class _FakeTransport(AgentTransport):
    files: dict[str, bytes] = field(default_factory=dict)
    run_calls: list[tuple[str, list[str] | str, float | None]] = field(default_factory=list)
    kill_calls: list[tuple[str, str, str]] = field(default_factory=list)
    next_result: CommandResult = field(
        default_factory=lambda: CommandResult(
            exit_code=0, stdout=b"ok\n", stderr=b"", timed_out=False,
        )
    )

    async def connect(self, handle):
        return None

    async def run_command(self, handle, cmd, *, stdin=None, timeout=None, cwd=None, env=None, tee_path=None):
        self.run_calls.append((handle.name, cmd, timeout))
        return self.next_result

    def write_file(self, handle, remote_path, content):
        self.files[remote_path] = content

    def read_file(self, handle, remote_path):
        return self.files.get(remote_path, b"")

    def kill_process(self, handle, pattern):
        self.kill_calls.append((handle.name, "process", pattern))

    def kill_session(self, handle, session_marker):
        self.kill_calls.append((handle.name, "session", session_marker))


def _handle(name: str = "team1_dev") -> ContainerHandle:
    return ContainerHandle(container_id="fake-id", name=name, networks=[])


def _mgr() -> GeminiAgentManager:
    return GeminiAgentManager(transport=_FakeTransport(), client=None)


def _full_cfg(**over) -> AgentConfig:
    base = dict(
        cli="gemini",
        provider="gemini",
        model="gemini-3.5-flash",
        api_key="sk-test",
        base_url="http://api-router:8080/v1",
        team_id="team1",
        team_token="dev-team1-token",
        mcp_endpoint="http://mcp-gateway:8080/mcp",
        submit_url="http://host.docker.internal:8000/game/flag/submit",
    )
    base.update(over)
    return AgentConfig(**base)


def _shell_of(call):
    cmd = call[1]
    return cmd[2] if isinstance(cmd, list) and len(cmd) > 2 else ""


def test_phase_command_headless_resume_gate_and_prompt():
    shell = _mgr().phase_command()[2]

    assert "gemini " in shell
    assert f'SESS="--session-id {SESSION_ID}"' in shell
    assert 'SESS="--resume latest"' in shell
    assert STARTED_MARKER in shell
    assert "--output-format stream-json" in shell
    assert "--yolo --skip-trust" in shell
    assert '-m "$GEMINI_MODEL"' in shell
    assert '-p "$(cat ' in shell and PROMPT_PATH in shell
    assert "< /dev/null" in shell
    assert WORKDIR in shell
    assert ENV_PATH in shell


def test_phase_command_model_override_injects_dash_m():
    default_shell = _mgr().phase_command()[2]
    assert '-m "$GEMINI_MODEL"' in default_shell

    override_shell = _mgr().phase_command(model="gemini-2.5-pro")[2]
    assert "-m gemini-2.5-pro" in override_shell
    assert "$GEMINI_MODEL" not in override_shell


def test_phase_command_session_marker_wraps_setsid_and_pidfile():
    shell = _mgr().phase_command(session_marker="abc123")[2]
    assert "setsid -w bash -c" in shell
    assert "/tmp/cyberarena_phase_abc123.pid" in shell
    assert "export CYBERARENA_PHASE=abc123" in shell
    assert "gemini " in shell


def test_prompt_path_and_kill_pattern_match_abc_contract():
    assert GeminiAgentManager.PROMPT_PATH == PROMPT_PATH
    assert GeminiAgentManager.PHASE_KILL_PATTERN == "gemini -p"
    assert GeminiAgentManager.CANONICAL_CLI == "gemini"
    assert GeminiAgentManager.REQUIRED_CLI == "gemini"


def test_run_phase_writes_prompt_then_invokes_gemini_with_marker():
    transport = _FakeTransport()
    mgr = GeminiAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(transport, _handle(), "patch the bug", timeout=10)
    asyncio.run(go())

    assert transport.files[PROMPT_PATH] == b"patch the bug"
    assert len(transport.run_calls) == 1
    name, cmd, timeout = transport.run_calls[0]
    assert name == "team1_dev" and timeout == 10
    shell = cmd[2]
    assert "setsid -w bash -c" in shell
    assert "/tmp/cyberarena_phase_" in shell and ".pid" in shell
    assert "gemini " in shell


def test_run_phase_timeout_fires_kill_session():
    transport = _FakeTransport(
        next_result=CommandResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True),
    )
    mgr = GeminiAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(transport, _handle("team2_dev"), "p", timeout=1)
    asyncio.run(go())

    assert len(transport.kill_calls) == 1
    container, kind, marker = transport.kill_calls[0]
    assert container == "team2_dev" and kind == "session"
    assert marker in transport.run_calls[0][1][2]


def test_install_writes_settings_with_remote_mcp_and_api_key_auth():
    transport = _FakeTransport()
    mgr = GeminiAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    settings = json.loads(transport.files[SETTINGS_PATH])
    assert settings["security"]["auth"]["selectedType"] == "gemini-api-key"
    assert settings["model"]["name"] == "gemini-3.5-flash"
    mcp = settings["mcpServers"]["cyberarena"]
    assert mcp["httpUrl"] == "http://mcp-gateway:8080/mcp"
    assert mcp["headers"]["X-Team-Token"] == "dev-team1-token"
    assert mcp["trust"] is True


def test_install_env_exports_gemini_vars_and_normalized_base_url():
    transport = _FakeTransport()
    mgr = GeminiAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    env = transport.files[ENV_PATH].decode()
    assert f"export HOME={GEMINI_HOME}" in env
    assert f"export GEMINI_CLI_HOME={GEMINI_HOME}" in env
    assert "export GEMINI_API_KEY=sk-test" in env
    assert "export GOOGLE_GEMINI_BASE_URL=http://api-router:8080" in env
    assert "export GEMINI_MODEL=gemini-3.5-flash" in env
    assert "export GEMINI_CLI_TRUST_WORKSPACE=true" in env
    assert "export CYBERARENA_TEAM_ID=team1" in env


def test_install_omits_mcp_block_when_no_endpoint():
    transport = _FakeTransport()
    mgr = GeminiAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg(mcp_endpoint=""))

    settings = json.loads(transport.files[SETTINGS_PATH])
    assert "mcpServers" not in settings


def test_install_without_base_url_uses_gemini_api_key_auth_and_no_gateway_env():
    transport = _FakeTransport()
    mgr = GeminiAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg(base_url=""))

    settings = json.loads(transport.files[SETTINGS_PATH])
    assert settings["security"]["auth"]["selectedType"] == "gemini-api-key"

    env = transport.files[ENV_PATH].decode()
    assert "GOOGLE_GEMINI_BASE_URL" not in env


def test_install_with_base_url_keeps_gemini_api_key_auth():
    transport = _FakeTransport()
    mgr = GeminiAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    settings = json.loads(transport.files[SETTINGS_PATH])
    assert settings["security"]["auth"]["selectedType"] == "gemini-api-key"


def test_install_clears_stale_home_and_verifies_baked_cli():
    transport = _FakeTransport()
    mgr = GeminiAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    shells = [_shell_of(c) for c in transport.run_calls]
    assert any(f"rm -rf {GEMINI_HOME}" in s for s in shells)
    assert any("command -v gemini" in s for s in shells)
    assert any(call[1] == ["gemini", "--version"] for call in transport.run_calls)


def test_install_fires_on_installed_callback():
    transport = _FakeTransport()
    mgr = GeminiAgentManager(transport=transport, client=None)
    fired: list[str] = []
    mgr.install(_handle(), _full_cfg(), on_installed=fired.append)
    assert fired == ["team1"]


def test_install_rejects_unregistered_gemini_model_newer_than_cli():
    """A newer-than-GA-flash gemini tag that is NOT registered in the from-source
    build (3.6, 3.9, 4.x) would be silently downgraded to gemini-3.5-flash on the
    wire, so install() must refuse it rather than run a mislabeled match."""
    for bad in ("gemini-3.6-flash", "gemini-3.9-flash", "gemini-4-pro"):
        with pytest.raises(ValueError, match="newer than the pinned gemini-cli"):
            _mgr().install(_handle(), _full_cfg(model=bad))


def test_install_allows_supported_and_registered_gemini_models():
    """<=3.5 gemini tags (natively served) plus the tags registered in the
    from-source build (3.7/3.8-flash) install fine — kept in sync with
    GEMINI_REGISTERED_EXTRA_MODELS."""
    for ok in ("gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3-pro-preview",
               "gemini-2.5-pro", "gemini-3.7-flash", "gemini-3.8-flash"):
        transport = _FakeTransport()
        GeminiAgentManager(transport=transport, client=None).install(
            _handle(), _full_cfg(model=ok))
        assert json.loads(transport.files[SETTINGS_PATH])["model"]["name"] == ok


def test_registry_maps_gemini_provider():
    from agents.registry import REGISTRY

    assert REGISTRY["gemini"] is GeminiAgentManager


def test_gemini_mcp_tool_naming():
    assert mcp_tool(_full_cfg(), "submit_flag") == "mcp_cyberarena_submit_flag"


def test_base_url_helper_keeps_root_and_strips_openai_compat_suffix():
    mgr = _mgr()
    assert mgr._gemini_base_url("http://api-router:8080") == "http://api-router:8080"
    assert mgr._gemini_base_url("http://api-router:8080/v1") == "http://api-router:8080"
    assert mgr._gemini_base_url("http://api-router:8080/v1beta/") == "http://api-router:8080"
    assert mgr._gemini_base_url("https://proxy.example/custom") == "https://proxy.example/custom"
    assert GEMINI_VERSION == "0.58.0"
