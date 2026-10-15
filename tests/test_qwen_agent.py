"""QwenCodeAgentManager unit tests — the fourth provider adapter (gemini-cli fork).

Mirrors tests/test_opencode_agent.py: layer 1 (pure argv builder), layer 2
(run_phase dance), install-side config/env writers against an in-memory
FakeTransport (no docker, no real npm/qwen).

What we pin:
  - phase_command builds `qwen --continue <prompt> --yolo --output-format
    stream-json` with the cwd-pin, env source, positional prompt-from-$(cat …),
    stdin closed, and the setsid/pidfile timeout wrapper.
  - install() writes settings.json (remote MCP w/ X-Team-Token + alwaysLoadTools)
    and qwen.env (all three OPENAI_* vars + QWEN_HOME), and fires on_installed.
  - the registry resolves provider "qwen" to this manager.
  - qwen exposes MCP tools in the CLAUDE form (mcp__cyberarena__<tool>), so
    mcp_tool needs no qwen branch.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from agents.qwen import (
    ENV_PATH,
    QWEN_HOME,
    QWEN_VERSION,
    SETTINGS_PATH,
    QwenCodeAgentManager,
)
from agents._provisioning import PROMPT_PATH, WORKDIR
from agents.prompts._helpers import mcp_tool
from agents.transport import AgentTransport, CommandResult
from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle


# ── FakeTransport: in-memory AgentTransport (same shape as the opencode test) ─


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


def _mgr() -> QwenCodeAgentManager:
    return QwenCodeAgentManager(transport=_FakeTransport(), client=None)


def _full_cfg(**over) -> AgentConfig:
    base = dict(
        cli="qwen",
        provider="qwen",
        model="qwen3-coder-plus",
        api_key="sk-test",
        base_url="http://api-router:8080/v1",
        team_id="team1",
        team_token="dev-team1-token",
        mcp_endpoint="http://mcp-gateway:8080/mcp",
        submit_url="http://host.docker.internal:8000/game/flag/submit",
    )
    base.update(over)
    return AgentConfig(**base)


# ── (1) phase_command — pure argv builder ─────────────────────────────


def test_phase_command_continue_workdir_envfile_and_positional_prompt():
    """Essential pieces: `qwen --continue`, --yolo, stream-json output, the
    cwd-pinned workdir (session isolation), env source, and the prompt passed
    as a positional `$(cat PROMPT_PATH)` with stdin closed."""
    shell = _mgr().phase_command()[2]

    assert "qwen --continue" in shell  # STATEFUL: resume prior phase's session
    assert "--yolo" in shell           # auto-approve every tool call (headless)
    assert "--output-format stream-json" in shell
    assert WORKDIR in shell
    assert ENV_PATH in shell
    assert PROMPT_PATH in shell
    assert "$(cat " in shell           # prompt-from-file as positional arg
    assert "< /dev/null" in shell      # headless: never read the exec stdin pipe


def test_phase_command_model_override_injects_dash_m():
    """Default relies on $OPENAI_MODEL from qwen.env (no -m). Explicit model=
    injects `-m <name>` for test/override parity."""
    default_shell = _mgr().phase_command()[2]
    assert " -m " not in default_shell

    override_shell = _mgr().phase_command(model="qwen3-coder")[2]
    assert "-m qwen3-coder" in override_shell


def test_phase_command_session_marker_wraps_setsid_and_pidfile():
    shell = _mgr().phase_command(session_marker="abc123")[2]
    assert "setsid -w bash -c" in shell
    assert "/tmp/cyberarena_phase_abc123.pid" in shell
    assert "qwen --continue" in shell
    assert "export CYBERARENA_PHASE=abc123" in shell


def test_prompt_path_and_kill_pattern_match_abc_contract():
    assert QwenCodeAgentManager.PROMPT_PATH == PROMPT_PATH
    assert QwenCodeAgentManager.PHASE_KILL_PATTERN == "qwen"
    assert QwenCodeAgentManager.CANONICAL_CLI == "qwen"


# ── (2) run_phase — inherited 3-line dance ────────────────────────────


def test_run_phase_writes_prompt_then_invokes_qwen_with_marker():
    transport = _FakeTransport()
    mgr = QwenCodeAgentManager(transport=transport, client=None)

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
    assert "qwen --continue" in shell


def test_run_phase_timeout_fires_kill_session():
    transport = _FakeTransport(
        next_result=CommandResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True),
    )
    mgr = QwenCodeAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(transport, _handle("team2_dev"), "p", timeout=1)
    asyncio.run(go())

    assert len(transport.kill_calls) == 1
    container, kind, marker = transport.kill_calls[0]
    assert container == "team2_dev" and kind == "session"
    assert marker in transport.run_calls[0][1][2]


# ── (3) install — config + env writers (FakeTransport, no docker) ─────


def test_install_writes_settings_with_remote_mcp_and_alwaysloadtools():
    transport = _FakeTransport()
    mgr = QwenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    settings = json.loads(transport.files[SETTINGS_PATH])
    mcp = settings["mcpServers"]["cyberarena"]
    assert mcp["httpUrl"] == "http://mcp-gateway:8080/mcp"
    assert mcp["headers"]["X-Team-Token"] == "dev-team1-token"
    # REQUIRED: else qwen defers MCP tools behind ToolSearch and a short phase
    # may never surface the arena tools.
    assert mcp["alwaysLoadTools"] is True


def test_install_env_exports_all_three_openai_vars_and_qwen_home():
    transport = _FakeTransport()
    mgr = QwenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    env = transport.files[ENV_PATH].decode()
    # All three needed or qwen's non-interactive auth exits 1.
    assert "export OPENAI_API_KEY=sk-test" in env
    assert "export OPENAI_BASE_URL=http://api-router:8080/v1" in env
    assert "export OPENAI_MODEL=qwen3-coder-plus" in env
    assert f"export QWEN_HOME={QWEN_HOME}" in env
    assert "export CYBERARENA_TEAM_ID=team1" in env
    # Claude/codex/opencode-specific vars must NOT leak into the qwen env.
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENCODE_CONFIG" not in env


def test_install_omits_mcp_block_when_no_endpoint():
    transport = _FakeTransport()
    mgr = QwenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg(mcp_endpoint=""))

    settings = json.loads(transport.files[SETTINGS_PATH])
    assert "mcpServers" not in settings


def _shell_of(call):
    cmd = call[1]
    return cmd[2] if isinstance(cmd, list) and len(cmd) > 2 else ""


def test_install_clears_stale_qwen_home():
    """A reused container/volume must not `--continue` a prior match's session:
    install wipes QWEN_HOME before re-creating it."""
    transport = _FakeTransport()
    mgr = QwenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    shells = [_shell_of(c) for c in transport.run_calls]
    assert any(f"rm -rf {QWEN_HOME}" in s for s in shells), "must clear stale session store"


def test_manager_declares_baked_cli_contract():
    assert QwenCodeAgentManager.REQUIRED_CLI == "qwen"
    assert QwenCodeAgentManager.CANONICAL_CLI == "qwen"
    assert QWEN_VERSION == "0.20.1"


def test_install_preflights_baked_cli_and_skips_npm():
    """Bake-only: install verifies the baked qwen (command -v + --version) and
    runs NO runtime npm install (a runtime install would re-pull at game time)."""
    transport = _FakeTransport()
    QwenCodeAgentManager(transport=transport, client=None).install(_handle(), _full_cfg())

    commands = [c[1] for c in transport.run_calls]
    assert ["sh", "-c", "command -v qwen"] in commands
    assert ["qwen", "--version"] in commands
    assert not any("npm install" in _shell_of(c) for c in transport.run_calls)


def test_install_fires_on_installed_callback():
    transport = _FakeTransport()
    mgr = QwenCodeAgentManager(transport=transport, client=None)
    fired: list[str] = []
    mgr.install(_handle(), _full_cfg(), on_installed=fired.append)
    assert fired == ["team1"]


# ── (4) registry dispatch ─────────────────────────────────────────────


def test_registry_maps_qwen_provider():
    from agents.registry import REGISTRY

    assert REGISTRY["qwen"] is QwenCodeAgentManager


# ── (5) MCP tool naming: qwen uses the CLAUDE form (no _helpers branch) ─


def test_qwen_mcp_tool_naming_is_claude_form():
    """qwen fully-qualifies remote MCP tools as mcp__<server>__<tool>
    (mcp-tool.ts:593), identical to Claude Code — so mcp_tool falls through
    to the default branch with no qwen-specific code."""
    assert mcp_tool(_full_cfg(), "submit_flag") == "mcp__cyberarena__submit_flag"
