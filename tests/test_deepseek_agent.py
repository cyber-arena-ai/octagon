"""DeepSeekAgentManager unit tests — the DeepSeek Harness (`dsh`) adapter.

Mirrors tests/test_opencode_agent.py (layer 1: pure argv builder; layer 2:
run_phase dance; layer 3: install-side writers against an in-memory
FakeTransport — no docker daemon, no real npm/dsh).

What we pin:
  - phase_command builds `dsh --profile headless --patch <overlay> "<task>"`
    with the cwd-pin, env source, prompt-from-$(cat …), stdin closed, and the
    setsid/pidfile timeout wrapper.
  - install() writes agent.env (DEEPSEEK_API_KEY + DEEPSEEK_BASE_URL + DSH_HOME)
    and the arena overlay patch (model pin + web-tool disable + cyberarena MCP
    with X-Team-Token), and fires on_installed.
  - the overlay omits the MCP insert when no mcp_endpoint is configured.
  - the registry resolves provider "deepseek" to this manager.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from agents.deepseek import (
    API_KEY_ENV,
    BASE_URL_ENV,
    DSH_FINAL_PATH,
    DSH_HOME,
    DSH_PATCH_PATH,
    DSH_SESSIONS_ROOT,
    DSH_PROFILE,
    DSH_VERSION,
    DSH_TOOL_ASSEMBLER_PATCH,
    EMITTER_PATH,
    PROVIDER_ROUTE,
    DeepSeekAgentManager,
)
from agents._provisioning import ENV_PATH, PROMPT_PATH, WORKDIR
from agents.registry import REGISTRY, resolve_cls
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


def _mgr() -> DeepSeekAgentManager:
    return DeepSeekAgentManager(transport=_FakeTransport(), client=None)


def _full_cfg(**over) -> AgentConfig:
    base = dict(
        cli="dsh",
        provider="deepseek",
        model="deepseek-v4-flash",
        api_key="team1-token",
        base_url="http://api-router:8080/v1",
        team_id="team1",
        team_token="dev-team1-token",
        mcp_endpoint="http://mcp-gateway:8080/mcp",
        submit_url="http://host.docker.internal:8000/game/flag/submit",
    )
    base.update(over)
    return AgentConfig(**base)


# ── (1) phase_command — pure argv builder ─────────────────────────────


def test_phase_command_headless_profile_patch_workdir_env_and_prompt_arg():
    """Essential pieces: `dsh --profile headless --patch <overlay>`, the
    cwd-pinned workdir (dsh's workspace root), env source, and the prompt as a
    positional `$(cat PROMPT_PATH)` with stdin closed (dsh reads argv)."""
    argv = _mgr().phase_command()

    assert argv[0] == "bash"
    assert argv[1] == "-c"
    shell = argv[2]
    assert f"dsh --profile {DSH_PROFILE}" in shell
    assert f"--patch {DSH_PATCH_PATH}" in shell
    assert WORKDIR in shell
    assert ENV_PATH in shell
    assert PROMPT_PATH in shell
    assert "$(cat " in shell          # prompt-from-file as positional task arg
    assert "</dev/null" in shell       # headless: never read the exec stdin pipe


def test_phase_command_has_no_model_flag():
    """dsh has NO per-invocation model flag — model is pinned in the overlay.
    A stray `model=` must not leak a `-m` into the argv."""
    shell = _mgr().phase_command(model="deepseek-v4-pro")[2]
    assert " -m " not in shell
    assert "deepseek-v4-pro" not in shell  # model lives in the patch, not the argv


def test_phase_command_session_marker_wraps_setsid_and_pidfile():
    """With a session marker, dsh is launched under setsid -w with its leader
    PID teed to the marker pidfile — same non-racy timeout kill as Claude."""
    shell = _mgr().phase_command(session_marker="abc123")[2]
    assert "setsid -w bash -c" in shell
    assert "/tmp/cyberarena_phase_abc123.pid" in shell
    assert f"dsh --profile {DSH_PROFILE}" in shell
    assert "export CYBERARENA_PHASE=abc123" in shell


def test_phase_command_runs_emitter_and_preserves_dsh_summary_path():
    """The phase stdout is the live dsh trajectory, not dsh's buffered final
    text; the launcher drains the emitter after dsh exits."""
    shell = _mgr().phase_command()[2]
    assert f"python3 {EMITTER_PATH} --root {DSH_SESSIONS_ROOT} --not-before \"$__not_before\" &" in shell
    assert f">{DSH_FINAL_PATH}" in shell
    assert "kill -TERM $__emit" in shell
    assert "wait $__emit" in shell
    assert "exit $__rc" in shell


def test_prompt_path_and_kill_pattern_match_abc_contract():
    assert DeepSeekAgentManager.PROMPT_PATH == PROMPT_PATH
    assert DeepSeekAgentManager.PHASE_KILL_PATTERN == "dsh"


def test_dsh_release_is_consistent_across_adapter_image_and_acceptance():
    root = Path(__file__).parents[1]
    dockerfile = (root / "dockerfiles/agent/deepseek/Dockerfile").read_text()
    acceptance = (root / "scripts/dsh_acceptance.py").read_text()
    assert dockerfile.count(f"@deepseek-ai/dsh@{DSH_VERSION}") == 1
    assert f'"version": "{DSH_VERSION}"' in acceptance
    assert f"dsh-{DSH_VERSION}.tgz" in acceptance


# ── (2) run_phase — inherited 3-line dance ────────────────────────────


def test_run_phase_writes_prompt_then_invokes_dsh_with_marker():
    transport = _FakeTransport()
    mgr = DeepSeekAgentManager(transport=transport, client=None)

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
    assert f"dsh --profile {DSH_PROFILE}" in shell


def test_run_phase_timeout_fires_kill_session():
    transport = _FakeTransport(
        next_result=CommandResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True),
    )
    mgr = DeepSeekAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(transport, _handle("team2_dev"), "p", timeout=1)
    asyncio.run(go())

    assert len(transport.kill_calls) == 1
    container, kind, marker = transport.kill_calls[0]
    assert container == "team2_dev" and kind == "session"
    assert marker in transport.run_calls[0][1][2]


# ── (3) install — env + overlay-patch writers (FakeTransport, no docker) ─


def test_install_env_exports_key_baseurl_and_dsh_home():
    transport = _FakeTransport()
    mgr = DeepSeekAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    env = transport.files[ENV_PATH].decode()
    assert f"export {API_KEY_ENV}=team1-token" in env
    assert f"export {BASE_URL_ENV}=http://api-router:8080/v1" in env
    assert f"export DSH_HOME={DSH_HOME}" in env
    assert "export DSH_TOOLS_MODE=both" in env
    assert "export DSH_TELEMETRY_DISABLED=1" in env
    assert "export CYBERARENA_TEAM_ID=team1" in env
    assert "export CYBERARENA_TEAM_TOKEN=dev-team1-token" in env


def test_install_rejects_missing_readiness_contract():
    mgr = _mgr()
    for field in ("base_url", "mcp_endpoint", "team_token"):
        cfg = _full_cfg(**{field: None})
        try:
            mgr.install(_handle(), cfg)
        except ValueError as exc:
            assert "DeepSeek readiness contract missing" in str(exc)
            assert field in str(exc)
        else:
            raise AssertionError(f"missing {field} was not rejected")


def test_install_ships_emitter_and_forces_plaintext_session_log():
    transport = _FakeTransport()
    mgr = DeepSeekAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    assert b"def iter_arena_lines" in transport.files[EMITTER_PATH]
    patch = transport.files[DSH_PATCH_PATH].decode()
    assert "id: session-persistence-jsonl" in patch
    assert f'root: "{DSH_SESSIONS_ROOT}"' in patch
    assert "compression: none" in patch


def test_install_overlay_pins_model_disables_web_and_wires_mcp():
    transport = _FakeTransport()
    mgr = DeepSeekAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg(model="deepseek-v4-pro"))

    patch = transport.files[DSH_PATCH_PATH].decode()
    # model pinned on the agent-default-model row (whole config restated).
    assert "id: agent-default-model" in patch
    assert f"provider: {PROVIDER_ROUTE}" in patch
    assert 'model: "deepseek-v4-pro"' in patch
    # server-side web search disabled.
    assert "id: tool-web" in patch
    assert "disabled: true" in patch
    # cyberarena MCP inserted, token carried in the X-Team-Token header from env.
    assert "@deepseek-ai/dsh-mcp-client" in patch
    assert "serverName: cyberarena" in patch
    assert "transport: streamable-http" in patch
    assert 'url: "http://mcp-gateway:8080/mcp"' in patch
    assert "X-Team-Token: !!js process.env.CYBERARENA_TEAM_TOKEN" in patch
    assert "failOnStartupError: true" in patch


def test_install_overlay_omits_mcp_when_no_endpoint_and_defaults_flash():
    mgr = _mgr()
    patch = mgr._build_overlay_patch(_full_cfg(model="", mcp_endpoint=""))
    assert "dsh-mcp-client" not in patch          # no MCP row without an endpoint
    assert 'model: "deepseek-v4-flash"' in patch  # empty model → flash default


def test_install_fires_on_installed_with_team_id():
    transport = _FakeTransport()
    mgr = DeepSeekAgentManager(transport=transport, client=None)
    seen: list[str] = []
    mgr.install(_handle(), _full_cfg(), on_installed=seen.append)
    assert seen == ["team1"]


def test_warmup_exit_class_is_bounded_and_nonzero_remains_best_effort(
    monkeypatch, caplog,
):
    secret = b"raw-warmup-secret-must-not-persist"
    monkeypatch.setattr(
        "agents.deepseek.prov.exec_sync", lambda *_args, **_kwargs: (7, secret)
    )
    monkeypatch.setattr(
        "agents.deepseek.prov.verify_cli", lambda *_args, **_kwargs: None
    )
    mgr = _mgr()
    seen: list[str] = []

    with caplog.at_level(logging.WARNING):
        mgr.install(_handle(), _full_cfg(), on_installed=seen.append)

    assert mgr.warmup_exit_class == "nonzero_exit"
    assert seen == ["team1"]
    assert secret.decode() not in caplog.text
    assert secret.decode() not in repr(mgr.__dict__)


def test_warmup_exit_class_distinguishes_clean_timeout_and_exception(monkeypatch):
    mgr = _mgr()
    monkeypatch.setattr(
        "agents.deepseek.prov.exec_sync", lambda *_args, **_kwargs: (0, b"secret")
    )
    mgr._warmup_profile(_handle())
    assert mgr.warmup_exit_class == "clean_exit"

    monkeypatch.setattr(
        "agents.deepseek.prov.exec_sync", lambda *_args, **_kwargs: (124, b"secret")
    )
    mgr._warmup_profile(_handle())
    assert mgr.warmup_exit_class == "timeout"

    def _raise(*_args, **_kwargs):
        raise RuntimeError("raw-secret")

    monkeypatch.setattr("agents.deepseek.prov.exec_sync", _raise)
    mgr._warmup_profile(_handle())
    assert mgr.warmup_exit_class == "unknown"


def test_install_verifies_baked_dsh_exact_pin(monkeypatch):
    seen: list[tuple[str, str | None]] = []

    def verify_cli(client, transport, container, binary, *, expected_version=None):
        seen.append((binary, expected_version))

    monkeypatch.setattr("agents.deepseek.prov.verify_cli", verify_cli)
    transport = _FakeTransport()
    mgr = DeepSeekAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    assert seen == [("dsh", DSH_VERSION)]


def test_install_patches_empty_tool_name_delta_assembler():
    transport = _FakeTransport()
    mgr = DeepSeekAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    patch_calls = [
        cmd[2]
        for _name, cmd, _timeout in transport.run_calls
        if isinstance(cmd, list) and len(cmd) == 3 and cmd[:2] == ["bash", "-c"]
    ]
    assert any("dsh-llm-deepseek/lib/index.js" in cmd for cmd in patch_calls)
    assert "if (call.function?.name) block.name = call.function.name;" in DSH_TOOL_ASSEMBLER_PATCH


# ── (4) registry wiring ───────────────────────────────────────────────


def test_registry_maps_deepseek_provider():
    assert REGISTRY["deepseek"] is DeepSeekAgentManager
    assert resolve_cls(_full_cfg()) is DeepSeekAgentManager


def test_registry_maps_dsh_cli_without_provider():
    assert REGISTRY["dsh"] is DeepSeekAgentManager
    assert resolve_cls(_full_cfg(provider="")) is DeepSeekAgentManager
