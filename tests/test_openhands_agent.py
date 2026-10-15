"""OpenHandsAgentManager unit tests — the seventh provider adapter (SDK driver).

Pure unit; no docker, no openhands-sdk. Pins the driver invocation, openhands.env
rendering, install-side driver copy + session clear, registry resolution, and the
bare MCP tool naming.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from agents.openhands import (
    CONVO_ID,
    DRIVER_PATH,
    ENV_PATH,
    PERSIST_DIR,
    VENV_PYTHON,
    OpenHandsAgentManager,
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


def _mgr() -> OpenHandsAgentManager:
    return OpenHandsAgentManager(transport=_FakeTransport(), client=None)


def _cfg(**over) -> AgentConfig:
    base = dict(
        cli="openhands", provider="openhands", model="gpt-5.4",
        api_key="sk-test", base_url="http://api-router:8080/v1",
        team_id="team1", team_token="dev-team1-token",
        mcp_endpoint="http://mcp-gateway:8080/mcp",
        submit_url="http://host.docker.internal:8000/game/flag/submit",
    )
    base.update(over)
    return AgentConfig(**base)


# ── phase_command ──────────────────────────────────────────────────────


def test_phase_command_runs_driver_with_venv_python_and_prompt():
    shell = _mgr().phase_command()[2]
    assert VENV_PYTHON in shell
    assert DRIVER_PATH in shell
    assert f"--prompt-file {PROMPT_PATH}" in shell
    assert WORKDIR in shell
    assert ENV_PATH in shell
    assert "< /dev/null" in shell


def test_phase_command_session_marker_wraps_setsid():
    shell = _mgr().phase_command(session_marker="abc123")[2]
    assert "setsid -w bash -c" in shell
    assert "/tmp/cyberarena_phase_abc123.pid" in shell
    assert "export CYBERARENA_PHASE=abc123" in shell


def test_constants():
    m = _mgr()
    assert m.PHASE_KILL_PATTERN == "openhands"
    assert m.CANONICAL_CLI == "openhands"
    assert m.PROMPT_PATH == PROMPT_PATH


# ── openhands.env ──────────────────────────────────────────────────────


def test_env_file_exports_llm_mcp_and_driver_knobs():
    env = _mgr()._build_env_file(_cfg())
    assert "export LLM_MODEL=gpt-5.4" in env
    assert "export LLM_BASE_URL=http://api-router:8080/v1" in env
    assert "export LLM_API_KEY=sk-test" in env
    assert "export CYBERARENA_MCP_ENDPOINT=http://mcp-gateway:8080/mcp" in env
    assert f"export OPENHANDS_WORKSPACE={WORKDIR}" in env
    assert f"export OPENHANDS_PERSIST_DIR={PERSIST_DIR}" in env
    assert f"export OPENHANDS_CONVO_ID={CONVO_ID}" in env
    # team token exported for the driver's MCP header
    assert "export CYBERARENA_TEAM_TOKEN=dev-team1-token" in env


# ── install ────────────────────────────────────────────────────────────


def _shell_of(call):
    cmd = call[1]
    return cmd[2] if isinstance(cmd, list) and len(cmd) > 2 else ""


def test_install_copies_driver_and_writes_env():
    transport = _FakeTransport()
    mgr = OpenHandsAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _cfg())

    # driver.py copied verbatim from the in-repo asset
    assert DRIVER_PATH in transport.files
    assert b"openhands.sdk" in transport.files[DRIVER_PATH]
    assert b"conversation_id" in transport.files[DRIVER_PATH]
    assert ENV_PATH in transport.files


def test_install_clears_stale_conversation_store():
    transport = _FakeTransport()
    mgr = OpenHandsAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _cfg())
    shells = [_shell_of(c) for c in transport.run_calls]
    assert any(f"rm -rf {PERSIST_DIR}" in s for s in shells)


def test_install_fires_on_installed_callback():
    transport = _FakeTransport()
    mgr = OpenHandsAgentManager(transport=transport, client=None)
    fired: list[str] = []
    mgr.install(_handle(), _cfg(), on_installed=fired.append)
    assert fired == ["team1"]


def test_run_phase_timeout_fires_kill_session():
    transport = _FakeTransport(
        next_result=CommandResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True),
    )
    mgr = OpenHandsAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(transport, _handle("team2_dev"), "p", timeout=1)
    asyncio.run(go())
    assert transport.kill_calls and transport.kill_calls[0][1] == "session"


# ── registry + MCP tool naming (bare) ──────────────────────────────────


def test_registry_maps_openhands_provider():
    from agents.registry import REGISTRY
    assert REGISTRY["openhands"] is OpenHandsAgentManager


def test_openhands_mcp_tool_naming_is_bare():
    """The SDK exposes remote MCP tools under their bare server name (no prefix),
    like codex/nexau."""
    assert mcp_tool(_cfg(), "submit_flag") == "submit_flag"
