"""KimiAgentManager unit tests — the native Kimi Code CLI adapter.

Mirrors tests/test_opencode_agent.py: a pure argv builder layer plus
install-side coverage against an in-memory FakeTransport (no docker or
real kimi). The kimi-specific thing we pin hardest is the per-model
provider `type` selection (openai_legacy / openai_responses / anthropic),
which is what lets the one kimi harness host `codex-5.3 vs opus-4.8`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from agents.kimi import (
    KIMI_CONFIG_PATH,
    KIMI_MCP_PATH,
    KIMI_STATE,
    KIMI_SESSION_HOME,
    KIMI_VERSION,
    PROVIDER_ID,
    WIRE_ANTHROPIC,
    WIRE_LEGACY,
    WIRE_RESPONSES,
    KimiAgentManager,
    _wire_type_for,
)
from agents._provisioning import ENV_PATH, PROMPT_PATH, WORKDIR
from agents.prompts._helpers import mcp_tool
from agents.transport import AgentTransport, CommandResult
from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle


@dataclass
class _FakeTransport(AgentTransport):
    files: dict[str, bytes] = field(default_factory=dict)
    run_calls: list = field(default_factory=list)
    kill_calls: list = field(default_factory=list)
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


@dataclass
class _MissingKimiTransport(_FakeTransport):
    async def run_command(self, handle, cmd, *, stdin=None, timeout=None,
                          cwd=None, env=None, tee_path=None):
        self.run_calls.append((handle.name, cmd, timeout))
        if cmd == ["sh", "-c", "command -v kimi"]:
            return CommandResult(
                exit_code=127, stdout=b"", stderr=b"not found", timed_out=False,
            )
        return self.next_result


def _handle(name: str = "team1_dev") -> ContainerHandle:
    return ContainerHandle(container_id="fake-id", name=name, networks=[])


def _mgr() -> KimiAgentManager:
    return KimiAgentManager(transport=_FakeTransport(), client=None)


def _cfg(**over) -> AgentConfig:
    base = dict(
        cli="kimi",
        provider="kimi",
        model="kimi-k3",
        api_key="sk-test",
        base_url="http://gw:8000/v1",
        team_id="team1",
        team_token="dev-team1-token",
        mcp_endpoint="http://host.docker.internal:8000/mcp",
    )
    base.update(over)
    return AgentConfig(**base)


# ── (1) wire-type selection — the core of this change ──────────────────


@pytest.mark.parametrize(
    "model,expected",
    [
        ("kimi-k3", WIRE_LEGACY),
        ("qwen3", WIRE_LEGACY),
        ("codex-5.3", WIRE_RESPONSES),
        ("gpt-5.3-codex", WIRE_RESPONSES),
        ("gpt-5.4", WIRE_RESPONSES),
        ("claude-opus-4.8", WIRE_ANTHROPIC),
        ("opus-4.8", WIRE_ANTHROPIC),
        ("claude-sonnet-4.6", WIRE_ANTHROPIC),
    ],
)
def test_wire_type_inferred_from_model(model, expected):
    assert _wire_type_for(_cfg(model=model)) == expected


def test_wire_type_explicit_override_wins():
    # A model name that would infer openai_legacy, forced to responses.
    cfg = _cfg(model="some-weird-model", extra_env={"KIMI_WIRE_API": "openai_responses"})
    assert _wire_type_for(cfg) == WIRE_RESPONSES


def test_wire_type_invalid_override_raises():
    cfg = _cfg(extra_env={"KIMI_WIRE_API": "grpc"})
    with pytest.raises(ValueError):
        _wire_type_for(cfg)


def test_wire_type_unknown_model_falls_back_to_legacy():
    assert _wire_type_for(_cfg(model="mystery-1")) == WIRE_LEGACY


# ── (2) config.toml carries the right provider type per model ──────────


def test_config_toml_codex_emits_openai_responses():
    toml = _mgr()._build_config_toml(_cfg(model="codex-5.3"))
    assert 'type = "openai_responses"' in toml
    assert 'base_url = "http://gw:8000/v1"' in toml
    assert '[models."codex-5.3"]' in toml
    assert f"provider = {chr(34)}{PROVIDER_ID}{chr(34)}" in toml
    assert "max_context_size" in toml


def test_config_toml_opus_emits_anthropic():
    toml = _mgr()._build_config_toml(_cfg(model="claude-opus-4.8"))
    assert 'type = "anthropic"' in toml
    assert '[models."claude-opus-4.8"]' in toml


def test_config_toml_kimi_stays_openai_legacy():
    toml = _mgr()._build_config_toml(_cfg(model="kimi-k3"))
    assert 'type = "openai_legacy"' in toml


def test_config_toml_never_configures_hosted_web_tools():
    """Egress-containment invariant: the adapter must not configure any kimi-cli
    `services.*` (moonshot_search/fetch) or hosted tool on any wire."""
    for model in ("kimi-k3", "codex-5.3", "claude-opus-4.8"):
        toml = _mgr()._build_config_toml(_cfg(model=model))
        assert "moonshot_search" not in toml
        assert "moonshot_fetch" not in toml
        assert "web_search" not in toml
        assert "[services" not in toml


# ── (3) phase_command essentials (unchanged behaviour) ─────────────────


def test_phase_command_has_workdir_envfile_streamjson_and_prompt():
    shell = _mgr().phase_command()[2]
    assert "kimi -w" in shell
    assert "--output-format stream-json" in shell
    assert "--print" in shell and "--yolo" in shell and "--afk" in shell
    assert WORKDIR in shell
    assert ENV_PATH in shell
    assert PROMPT_PATH in shell
    assert "</dev/null" in shell



def test_phase_command_first_run_marker_then_continue_and_timeout_wrapper():
    shell = _mgr().phase_command(session_marker="abc123")[2]
    assert f"if [ -f {KIMI_STATE}/.cyberarena_started ]" in shell
    assert "CONTINUE=\"-C\"" in shell
    assert "setsid -w bash -c" in shell
    assert "/tmp/cyberarena_phase_abc123.pid" in shell
    assert "export CYBERARENA_PHASE=abc123" in shell


def test_manager_declares_baked_cli_contract():
    assert KimiAgentManager.REQUIRED_CLI == "kimi"
    assert KimiAgentManager.CANONICAL_CLI == "kimi"
    assert KIMI_VERSION == "1.49.0"


def _shell_of(call):
    cmd = call[1]
    return cmd[2] if isinstance(cmd, list) and len(cmd) > 2 else ""


def test_install_missing_baked_cli_fails_loud():
    transport = _MissingKimiTransport()
    with pytest.raises(RuntimeError, match="cyberarena/agent-<provider>"):
        KimiAgentManager(transport=transport, client=None).install(_handle(), _cfg())


def test_install_preflights_baked_cli_and_clears_all_stale_state():
    transport = _FakeTransport()
    KimiAgentManager(transport=transport, client=None).install(_handle(), _cfg())
    commands = [call[1] for call in transport.run_calls]
    assert ["sh", "-c", "command -v kimi"] in commands
    assert ["kimi", "--version"] in commands
    assert any(
        f"rm -rf {KIMI_STATE} {KIMI_SESSION_HOME} && mkdir -p {KIMI_STATE}"
        in _shell_of(c) for c in transport.run_calls
    )
    assert not any("pip install" in _shell_of(c) for c in transport.run_calls)


# ── (4) install writes config + mcp; model type flows through ──────────


def test_install_writes_config_and_mcp_with_responses_for_codex():
    mgr = KimiAgentManager(transport=_FakeTransport(), client=None)
    t = mgr._transport
    mgr.install(_handle(), _cfg(model="codex-5.3"))
    cfg_written = t.files[KIMI_CONFIG_PATH].decode()
    assert 'type = "openai_responses"' in cfg_written
    mcp_written = json.loads(t.files[KIMI_MCP_PATH].decode())
    assert mcp_written["mcpServers"]["cyberarena"]["headers"]["X-Team-Token"] == "dev-team1-token"


def test_install_omits_mcp_file_when_endpoint_is_empty():
    transport = _FakeTransport()
    KimiAgentManager(transport=transport, client=None).install(
        _handle(), _cfg(mcp_endpoint=""),
    )
    assert KIMI_MCP_PATH not in transport.files


def test_registry_resolves_kimi_by_provider_and_cli():
    from agents.registry import resolve_cls

    assert resolve_cls(_cfg()) is KimiAgentManager
    assert resolve_cls(_cfg(provider="")) is KimiAgentManager


def test_kimi_prompt_uses_cli_visible_mcp_tool_name():
    assert mcp_tool(_cfg(), "submit_flag") == "mcp__cyberarena__submit_flag"
