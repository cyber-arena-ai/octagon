"""GrokAgentManager unit tests — the sixth provider adapter (xAI Grok, codex-shaped).

Pure unit; no docker. Pins config.toml / grok.env rendering, phase_command argv,
provider-aware MCP tool naming, and registry resolution.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from agents.grok import (
    API_KEY_ENV,
    CONFIG_PATH,
    GROK_VERSION,
    PROVIDER_ID,
    SESSION_ID,
    STARTED_MARKER,
    GrokAgentManager,
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


def _mgr() -> GrokAgentManager:
    return GrokAgentManager(transport=_FakeTransport(), client=None)


def test_manager_declares_baked_cli_contract():
    assert GrokAgentManager.REQUIRED_CLI == "grok"
    assert GrokAgentManager.CANONICAL_CLI == "grok"
    assert GROK_VERSION == "0.2.111"


def test_install_preflights_baked_cli_and_skips_npm():
    """Bake-only: install verifies the baked grok (command -v + --version) and
    runs NO runtime npm install (a runtime install would re-pull a possibly-
    different build of this beta CLI at game time)."""
    transport = _FakeTransport()
    GrokAgentManager(transport=transport, client=None).install(_handle(), _cfg())

    commands = [c[1] for c in transport.run_calls]
    assert ["sh", "-c", "command -v grok"] in commands
    assert ["grok", "--version"] in commands
    shells = [c[1][2] for c in transport.run_calls if isinstance(c[1], list) and len(c[1]) > 2]
    assert not any("npm install" in s for s in shells)


def _cfg(**over) -> AgentConfig:
    base = dict(
        cli="grok", provider="grok", model="grok-code-fast-1",
        api_key="sk-test", base_url="http://api-router:8080/v1",
        team_id="team1", team_token="dev-team1-token",
        mcp_endpoint="http://mcp-gateway:8080/mcp",
        submit_url="http://host.docker.internal:8000/game/flag/submit",
    )
    base.update(over)
    return AgentConfig(**base)


# ── registry ───────────────────────────────────────────────────────────


def test_registry_resolves_grok_by_provider_and_cli():
    from agents.registry import build
    assert isinstance(build(_cfg()), GrokAgentManager)
    assert isinstance(build(_cfg(provider="")), GrokAgentManager)


# ── config.toml ────────────────────────────────────────────────────────


def test_config_toml_byok_chat_completions_provider():
    toml = _mgr()._build_config_toml(_cfg())
    assert f"[model.{PROVIDER_ID}]" in toml
    assert 'model = "grok-code-fast-1"' in toml
    assert 'base_url = "http://api-router:8080/v1"' in toml
    assert f'env_key = "{API_KEY_ENV}"' in toml     # key comes from env, not inline
    assert 'api_backend = "chat_completions"' in toml
    assert f'default = "{PROVIDER_ID}"' in toml
    assert "sk-test" not in toml                    # the key must never be in the toml


def test_config_toml_mcp_carries_x_team_token_via_env():
    toml = _mgr()._build_config_toml(_cfg())
    assert "[mcp_servers.cyberarena]" in toml
    assert 'url = "http://mcp-gateway:8080/mcp"' in toml
    assert "X-Team-Token" in toml and "${CYBERARENA_TEAM_TOKEN}" in toml
    assert "dev-team1-token" not in toml            # token via env, never literal
    # claude-config MCP auto-load disabled
    assert "[compat.claude]" in toml and "mcps = false" in toml


def test_config_toml_omits_mcp_without_endpoint():
    toml = _mgr()._build_config_toml(_cfg(mcp_endpoint=""))
    assert "[mcp_servers" not in toml


# ── grok.env ───────────────────────────────────────────────────────────


def test_env_file_exports_key_home_and_team_vars():
    env = _mgr()._build_env_file(_cfg())
    assert f"export {API_KEY_ENV}=sk-test" in env
    assert "export HOME=/root" in env               # so ~/.grok resolves under docker exec
    assert "export CYBERARENA_TEAM_TOKEN=dev-team1-token" in env
    assert "ANTHROPIC_API_KEY" not in env


# ── phase_command ──────────────────────────────────────────────────────


def test_phase_command_headless_flags_and_session():
    shell = _mgr().phase_command()[2]
    assert 'grok -p "$(cat ' in shell and PROMPT_PATH in shell
    assert "--always-approve" in shell              # unattended
    assert "--output-format streaming-json" in shell
    assert "--no-auto-update" in shell              # no silent self-upgrade mid-match
    assert "< /dev/null" in shell
    assert WORKDIR in shell


def test_phase_command_create_then_resume_gate():
    """grok's `-s` CREATES a session and errors on reuse ("already in use"), so the
    first invocation must use `-s <uuid>` and every later one `--resume <uuid>`,
    gated on STARTED_MARKER — same exec-vs-resume shape as codex. Regression guard:
    a constant `-s` would fast-exit every chunk and fail the whole match."""
    shell = _mgr().phase_command()[2]
    assert STARTED_MARKER in shell                       # the gate file
    assert f'SESS="--resume {SESSION_ID}"' in shell       # marker present -> resume
    assert f'SESS="-s {SESSION_ID}"' in shell             # first run -> create
    assert "$SESS" in shell                              # unquoted so it word-splits


def test_phase_command_session_marker_wraps_setsid():
    shell = _mgr().phase_command(session_marker="abc123")[2]
    assert "setsid -w bash -c" in shell
    assert "/tmp/cyberarena_phase_abc123.pid" in shell
    assert "export CYBERARENA_PHASE=abc123" in shell
    assert STARTED_MARKER in shell                       # gate survives the setsid wrap


def test_constants():
    m = _mgr()
    assert m.PHASE_KILL_PATTERN == "grok"
    assert m.CANONICAL_CLI == "grok"
    assert m.PROMPT_PATH.endswith("prompt.txt")
    assert CONFIG_PATH.endswith("config.toml")


# ── run_phase dance ────────────────────────────────────────────────────


def test_run_phase_timeout_fires_kill_session():
    transport = _FakeTransport(
        next_result=CommandResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True),
    )
    mgr = GrokAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(transport, _handle("team2_dev"), "p", timeout=1)
    asyncio.run(go())

    assert len(transport.kill_calls) == 1
    container, kind, marker = transport.kill_calls[0]
    assert container == "team2_dev" and kind == "session"


# ── MCP tool naming: grok uses cyberarena__<tool> (double underscore) ──


def test_grok_mcp_tool_naming_double_underscore():
    assert mcp_tool(_cfg(), "submit_flag") == "cyberarena__submit_flag"
