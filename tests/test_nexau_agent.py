"""NexauAgentManager unit tests — the fifth provider adapter (git+uv baked).

Mirrors tests/test_opencode_agent.py: pure argv builder, run_phase dance, and
install-side config/env writers against an in-memory FakeTransport (no docker,
no real nexau).

What we pin:
  - phase_command builds `nexau chat <agent.yaml> --query <prompt> --session-id`
    with the cwd-pin, env source, positional prompt-from-$(cat …), stdin closed,
    and the setsid/pidfile timeout wrapper.
  - install() renders agent.yaml (llm_config + remote MCP w/ X-Team-Token via env +
    tools) and nexau.env (LLM key + SANDBOX_WORK_DIR local-exec + state dir), copies
    the vendored tool descriptors, and fires on_installed.
  - the registry resolves provider "nexau" (and the "nexau chat" cli token).
  - nexau exposes MCP tools BARE (like codex), so mcp_tool routes it to the bare branch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from agents.nexau import (
    AGENT_YAML,
    DRIVER_PATH,
    ENV_PATH,
    NEXAU_STATE,
    SESSION_ID,
    TOOLS_DIR,
    VENV_PYTHON,
    NexauAgentManager,
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


def _mgr() -> NexauAgentManager:
    return NexauAgentManager(transport=_FakeTransport(), client=None)


def _full_cfg(**over) -> AgentConfig:
    base = dict(
        cli="nexau chat",
        provider="nexau",
        model="gpt-5.4",
        api_key="sk-test",
        base_url="http://api-router:8080/v1",
        team_id="team1",
        team_token="dev-team1-token",
        own_prod_host="team1_prod",
        opposing_prod_hosts=("team2_prod",),
        mcp_endpoint="http://mcp-gateway:8080/mcp",
        submit_url="http://host.docker.internal:8000/game/flag/submit",
    )
    base.update(over)
    return AgentConfig(**base)


# ── (1) phase_command ─────────────────────────────────────────────────


def test_phase_command_runs_driver_with_agent_and_prompt():
    shell = _mgr().phase_command()[2]
    # driver under the baked venv python (nexau chat can't load tools/MCP in v0.3.9)
    assert VENV_PYTHON in shell
    assert DRIVER_PATH in shell
    assert f"--agent {AGENT_YAML}" in shell
    assert f"--prompt-file {PROMPT_PATH}" in shell
    assert f"--session-id {SESSION_ID}" in shell
    assert WORKDIR in shell
    assert ENV_PATH in shell
    assert "< /dev/null" in shell   # headless: never read the exec stdin pipe


def test_phase_command_session_marker_wraps_setsid_and_pidfile():
    shell = _mgr().phase_command(session_marker="abc123")[2]
    assert "setsid -w bash -c" in shell
    assert "/tmp/cyberarena_phase_abc123.pid" in shell
    assert "driver.py" in shell
    assert "export CYBERARENA_PHASE=abc123" in shell


def test_prompt_path_and_kill_pattern_match_abc_contract():
    assert NexauAgentManager.PROMPT_PATH == PROMPT_PATH
    assert NexauAgentManager.PHASE_KILL_PATTERN == "nexau"
    assert NexauAgentManager.CANONICAL_CLI == "nexau chat"


# ── (2) run_phase — inherited 3-line dance ────────────────────────────


def test_run_phase_writes_prompt_then_invokes_nexau_with_marker():
    transport = _FakeTransport()
    mgr = NexauAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(transport, _handle(), "patch the bug", timeout=10)
    asyncio.run(go())

    assert transport.files[PROMPT_PATH] == b"patch the bug"
    name, cmd, timeout = transport.run_calls[0]
    assert name == "team1_dev" and timeout == 10
    assert "driver.py" in cmd[2]


def test_run_phase_timeout_fires_kill_session():
    transport = _FakeTransport(
        next_result=CommandResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True),
    )
    mgr = NexauAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(transport, _handle("team2_dev"), "p", timeout=1)
    asyncio.run(go())

    assert len(transport.kill_calls) == 1
    container, kind, marker = transport.kill_calls[0]
    assert container == "team2_dev" and kind == "session"
    assert marker in transport.run_calls[0][1][2]


# ── (3) install — agent.yaml + env + tool descriptors ─────────────────


def test_install_renders_agent_yaml_with_llm_mcp_and_tools():
    transport = _FakeTransport()
    mgr = NexauAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    y = transport.files[AGENT_YAML].decode()
    # model/base_url/api_key are NOT in the yaml — NexAU's LLMConfig reads them from
    # env (LLM_MODEL/LLM_BASE_URL/LLM_API_KEY); the yaml carries only the wire selector.
    assert "api_type: openai_chat_completion" in y
    assert "gpt-5.4" not in y                       # model comes from env, not the yaml
    assert "sk-test" not in y                       # the key must never be in the yaml
    # remote MCP server carries the team token via ${env}, not literally
    assert "type: http" in y
    assert "url: \"http://mcp-gateway:8080/mcp\"" in y
    assert "${env.CYBERARENA_TEAM_TOKEN}" in y
    assert "dev-team1-token" not in y
    # tools list references shell + file bindings
    assert "nexau.archs.tool.builtin.shell_tools:run_shell_command" in y
    assert "nexau.archs.tool.builtin.file_tools:read_file" in y


def test_install_copies_tool_descriptors():
    transport = _FakeTransport()
    mgr = NexauAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    assert f"{TOOLS_DIR}/run_shell_command_sync.tool.yaml" in transport.files
    assert f"{TOOLS_DIR}/read_file.tool.yaml" in transport.files
    # descriptor content is real YAML shipped in-repo
    assert b"name: run_shell_command" in transport.files[f"{TOOLS_DIR}/run_shell_command_sync.tool.yaml"]
    # the SDK driver is copied too (nexau chat can't load tools/MCP; the driver does)
    assert DRIVER_PATH in transport.files
    assert b"AgentConfig.from_yaml" in transport.files[DRIVER_PATH]


def test_install_env_exports_key_sandbox_and_state():
    transport = _FakeTransport()
    mgr = NexauAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    env = transport.files[ENV_PATH].decode()
    # NexAU's LLMConfig resolves the model/endpoint/key from these env vars.
    assert "export LLM_MODEL=gpt-5.4" in env
    assert "export LLM_BASE_URL=http://api-router:8080/v1" in env
    assert "export LLM_API_KEY=sk-test" in env
    assert f"export SANDBOX_WORK_DIR={WORKDIR}" in env   # local shell exec, not e2b
    assert f"export NEXAU_CLI_STATE_DIR={NEXAU_STATE}" in env
    assert "export CYBERARENA_TEAM_TOKEN=dev-team1-token" in env  # referenced by the MCP header
    assert "E2B_API_KEY" not in env                     # must NOT trigger the cloud sandbox


def test_install_omits_mcp_block_when_no_endpoint():
    transport = _FakeTransport()
    mgr = NexauAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg(mcp_endpoint=""))

    y = transport.files[AGENT_YAML].decode()
    assert "mcp_servers:" not in y


def _shell_of(call):
    cmd = call[1]
    return cmd[2] if isinstance(cmd, list) and len(cmd) > 2 else ""


def test_install_clears_stale_session_store():
    transport = _FakeTransport()
    mgr = NexauAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())
    shells = [_shell_of(c) for c in transport.run_calls]
    assert any(f"rm -rf {NEXAU_STATE}" in s for s in shells)


def test_install_fires_on_installed_callback():
    transport = _FakeTransport()
    mgr = NexauAgentManager(transport=transport, client=None)
    fired: list[str] = []
    mgr.install(_handle(), _full_cfg(), on_installed=fired.append)
    assert fired == ["team1"]


# ── (4) registry dispatch ─────────────────────────────────────────────


def test_registry_maps_nexau_provider():
    from agents.registry import REGISTRY, resolve_cls

    assert REGISTRY["nexau"] is NexauAgentManager
    # also resolves via the leading cli token when provider is unset
    assert resolve_cls(_full_cfg(provider="")) is NexauAgentManager


# ── (5) MCP tool naming: nexau is BARE (codex branch), NOT claude form ─


def test_nexau_mcp_tool_naming_is_bare():
    """NexAU builds MCPTool(name=<raw>) with no prefix, so the model sees the bare
    tool name — same as codex, and unlike claude/qwen's mcp__ form."""
    assert mcp_tool(_full_cfg(), "submit_flag") == "submit_flag"
