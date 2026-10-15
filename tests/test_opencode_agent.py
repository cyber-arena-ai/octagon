"""OpenCodeAgentManager unit tests — the second provider adapter.

Mirrors tests/test_dcf_phase_invocation.py (layer 1: pure argv builder;
layer 2: run_phase dance) and adds install-side coverage against an
in-memory FakeTransport (no docker daemon, no real npm/opencode).

What we pin:
  - phase_command builds `opencode run -c` with the cwd-pin, env source,
    prompt-from-$(cat …), and the setsid/pidfile timeout wrapper.
  - the model is env-driven by default (`$OPENCODE_MODEL`) and override-able.
  - install() writes opencode.json (OpenAI-compatible provider + remote
    MCP w/ X-Team-Token) and opencode.env, and fires on_installed.
  - the registry resolves provider "opencode" to this manager.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from agents.opencode import (
    CONFIG_PATH,
    ENV_PATH,
    PROVIDER_ID,
    SERVER_URL,
    OpenCodeAgentManager,
    _augment_lines,
    _merge_reasoning_lines,
    _rewrite_timing,
    _session_id_of,
)
from agents._provisioning import PROMPT_PATH, WORKDIR
from agents.transport import AgentTransport, CommandResult
from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle


# ── FakeTransport: in-memory AgentTransport (same shape as the dcf test) ─


@dataclass
class _FakeTransport(AgentTransport):
    files: dict[str, bytes] = field(default_factory=dict)
    run_calls: list[tuple[str, list[str] | str, float | None]] = field(default_factory=list)
    kill_calls: list[tuple[str, str]] = field(default_factory=list)
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


def _mgr() -> OpenCodeAgentManager:
    return OpenCodeAgentManager(transport=_FakeTransport(), client=None)


def _full_cfg(**over) -> AgentConfig:
    base = dict(
        cli="opencode run",
        provider="opencode",
        model="Qwen2.5-Coder-32B-Instruct",
        api_key="sk-test",
        base_url="http://gw:8000/v1",
        team_id="team1",
        team_token="dev-team1-token",
        mcp_endpoint="http://host.docker.internal:8000/mcp",
        submit_url="http://host.docker.internal:8000/game/flag/submit",
    )
    base.update(over)
    return AgentConfig(**base)


# ── (1) phase_command — pure argv builder ─────────────────────────────


def test_phase_command_contains_continue_workdir_envfile_and_prompt_arg():
    """Essential pieces: `opencode run -c`, the cwd-pinned workdir
    (session isolation), env source, and the prompt passed as a
    positional `$(cat PROMPT_PATH)` (opencode reads argv, not stdin)."""
    argv = _mgr().phase_command()

    assert argv[0] == "bash"
    assert argv[1] == "-c"
    shell = argv[2]
    assert "opencode run" in shell
    assert "--attach http://127.0.0.1:4096" in shell  # attach the persistent server
    assert "--dangerously-skip-permissions" in shell  # else external-dir tool calls auto-reject
    assert " -c " in shell  # STATEFUL: resume prior phase's session
    assert WORKDIR in shell
    assert ENV_PATH in shell
    assert PROMPT_PATH in shell
    assert "$(cat " in shell  # prompt-from-file as positional arg
    assert "< /dev/null" in shell  # headless: never read the exec stdin pipe


def test_phase_command_model_default_vs_explicit_override():
    """Default sources $OPENCODE_MODEL (exported by install()).
    Explicit model= injects `-m <name>` and drops the env-var fallback."""
    default_shell = _mgr().phase_command()[2]
    assert '-m "$OPENCODE_MODEL"' in default_shell

    override_shell = _mgr().phase_command(model="cyberarena/foo")[2]
    assert "-m cyberarena/foo" in override_shell or "-m 'cyberarena/foo'" in override_shell
    assert "$OPENCODE_MODEL" not in override_shell


def test_phase_command_bare_model_gets_provider_prefix():
    """The orchestrator passes the BARE model id (cfg.model == 'qwen3').
    phase_command MUST front it with the provider id ('cyberarena/qwen3')
    or opencode raises ProviderModelNotFoundError. Regression guard for the
    2026-06-10 'Unexpected server error' on every real phase."""
    shell = _mgr().phase_command(model="qwen3")[2]
    assert "-m cyberarena/qwen3" in shell or "-m 'cyberarena/qwen3'" in shell
    assert "-m qwen3 " not in shell  # never the bare, unprefixed form


def test_phase_command_session_marker_wraps_setsid_and_pidfile():
    """With a session marker, opencode is launched under setsid -w with
    its leader PID teed to the marker pidfile — same non-racy timeout
    kill mechanism as Claude."""
    shell = _mgr().phase_command(session_marker="abc123")[2]
    assert "setsid -w bash -c" in shell
    assert "/tmp/cyberarena_phase_abc123.pid" in shell
    assert "opencode run" in shell
    # marker exported so kill_session's /proc/<pid>/environ scan can reap
    # escaped children — parity with claude.py / codex.py.
    assert "export CYBERARENA_PHASE=abc123" in shell


def test_prompt_path_and_kill_pattern_match_abc_contract():
    assert OpenCodeAgentManager.PROMPT_PATH == PROMPT_PATH
    assert OpenCodeAgentManager.PHASE_KILL_PATTERN == "opencode run"


# ── (2) run_phase — inherited 3-line dance ────────────────────────────


async def _noop_ensure(transport, handle):  # server-liveness is tested separately
    return None


def test_run_phase_writes_prompt_then_invokes_opencode_with_marker():
    transport = _FakeTransport()
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr._ensure_server = _noop_ensure

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
    assert "opencode run" in shell


def test_run_phase_timeout_fires_kill_session():
    transport = _FakeTransport(
        next_result=CommandResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True),
    )
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr._ensure_server = _noop_ensure

    async def go():
        return await mgr.run_phase(transport, _handle("team2_dev"), "p", timeout=1)
    asyncio.run(go())

    assert len(transport.kill_calls) == 1
    container, kind, marker = transport.kill_calls[0]
    assert container == "team2_dev" and kind == "session"
    assert marker in transport.run_calls[0][1][2]


# ── (3) install — config + env writers (FakeTransport, no docker) ─────


def test_install_writes_opencode_config_with_provider_and_mcp():
    transport = _FakeTransport()
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    raw = transport.files[CONFIG_PATH]
    cfg = json.loads(raw)
    # OpenAI-compatible provider pointed at the vLLM endpoint.
    prov = cfg["provider"][PROVIDER_ID]
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert prov["options"]["baseURL"] == "http://gw:8000/v1"
    assert prov["options"]["apiKey"] == "sk-test"
    assert "Qwen2.5-Coder-32B-Instruct" in prov["models"]
    # Default model + small_model so resolution never depends on a per-run -m.
    assert cfg["model"] == f"{PROVIDER_ID}/Qwen2.5-Coder-32B-Instruct"
    assert cfg["small_model"] == f"{PROVIDER_ID}/Qwen2.5-Coder-32B-Instruct"
    # Remote MCP server carries the team token in headers.
    mcp = cfg["mcp"]["cyberarena"]
    assert mcp["type"] == "remote"
    assert mcp["url"] == "http://host.docker.internal:8000/mcp"
    assert mcp["headers"]["X-Team-Token"] == "dev-team1-token"


def test_install_env_exports_config_path_and_model_ref():
    transport = _FakeTransport()
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    env = transport.files[ENV_PATH].decode()
    assert f"export OPENCODE_CONFIG={CONFIG_PATH}" in env
    # model ref is fronted with our provider id, bare id stripped of any prefix.
    assert "export OPENCODE_MODEL=cyberarena/Qwen2.5-Coder-32B-Instruct" in env
    assert "export CYBERARENA_TEAM_ID=team1" in env


def test_install_strips_provider_prefix_from_model():
    transport = _FakeTransport()
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg(model="vllm/MyModel"))

    cfg = json.loads(transport.files[CONFIG_PATH])
    assert "MyModel" in cfg["provider"][PROVIDER_ID]["models"]
    env = transport.files[ENV_PATH].decode()
    assert "export OPENCODE_MODEL=cyberarena/MyModel" in env


def test_install_omits_mcp_block_when_no_endpoint():
    transport = _FakeTransport()
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg(mcp_endpoint=""))

    cfg = json.loads(transport.files[CONFIG_PATH])
    assert "mcp" not in cfg


def _shell_of(call):
    cmd = call[1]
    return cmd[2] if isinstance(cmd, list) and len(cmd) > 2 else ""


def test_install_starts_persistent_server_then_warms_it():
    """install() must (a) start `opencode serve` and (b) issue an attached
    warmup `opencode run --attach`, so the orchestrator's rapid per-phase
    runs share one server (not per-run servers that collide on the session
    DB and crash). See _start_server / SERVER_PORT."""
    transport = _FakeTransport()
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg())

    shells = [_shell_of(c) for c in transport.run_calls]
    assert any("opencode serve --port 4096" in s for s in shells), "must start the server"
    warmups = [s for s in shells if "opencode run --attach" in s]
    assert warmups, "install must issue an attached warmup run"
    assert "< /dev/null" in warmups[0]


def test_install_skips_server_and_warmup_for_non_opencode_cli():
    """A stand-in cli (e.g. 'sleep 60') must not trigger npm/serve/warmup."""
    transport = _FakeTransport()
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg(cli="sleep 60"))

    shells = [_shell_of(c) for c in transport.run_calls]
    assert not any("opencode run" in s for s in shells)
    assert not any("opencode serve" in s for s in shells)


def test_install_fires_on_installed_callback():
    transport = _FakeTransport()
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    fired: list[str] = []
    mgr.install(_handle(), _full_cfg(), on_installed=fired.append)
    assert fired == ["team1"]


# ── (4) registry dispatch ─────────────────────────────────────────────


def test_registry_maps_opencode_provider():
    from agents.registry import REGISTRY

    assert REGISTRY["opencode"] is OpenCodeAgentManager


# ── (6) reasoning backfill: headless --format json drops reasoning parts ──
# opencode #27361 — the run counts reasoning tokens but never emits the text on
# stdout; the reasoning survives in the server session and we fold it back in.

_STREAM = "\n".join([
    '{"type":"step_start","timestamp":100,"sessionID":"S1","part":{"type":"step-start","messageID":"M1"}}',
    '{"type":"tool_use","timestamp":110,"sessionID":"S1","part":{"type":"tool","tool":"read","messageID":"M1","state":{"status":"completed","input":{}}}}',
    '{"type":"step_finish","timestamp":120,"sessionID":"S1","part":{"type":"step-finish","messageID":"M1","tokens":{}}}',
]) + "\n"

# session payload as returned by GET /session/<id>/message: a M1 reasoning part
# (kept) and an M2 reasoning part whose message never reached this stream (dropped).
_SESSION = [{
    "info": {},
    "parts": [
        {"id": "p1", "sessionID": "S1", "messageID": "M1", "type": "reasoning",
         "text": "The dev key should decrypt team2's flag.", "time": {"start": 95}},
        {"id": "p2", "sessionID": "S1", "messageID": "M1", "type": "text",
         "text": "reading now", "time": {"start": 115}},
        {"id": "p3", "sessionID": "S1", "messageID": "M2", "type": "reasoning",
         "text": "from a different round", "time": {"start": 900}},
    ],
}]


def test_session_id_of_reads_first_line_or_none():
    assert _session_id_of(_STREAM) == "S1"
    assert _session_id_of("not json\n\n") is None
    assert _session_id_of("") is None


def test_merge_reasoning_inserts_before_the_message_first_line():
    merged = [json.loads(x) for x in _merge_reasoning_lines(_STREAM, _SESSION).splitlines()]
    kinds = [m["type"] for m in merged]
    # reasoning is folded in ahead of M1's first (step_start) line; order else kept
    assert kinds == ["reasoning", "step_start", "tool_use", "step_finish"]
    assert merged[0]["part"]["text"] == "The dev key should decrypt team2's flag."
    assert merged[0]["part"]["messageID"] == "M1"
    assert merged[0]["sessionID"] == "S1"
    # M2's reasoning (message absent from this stream) is NOT injected
    assert sum(1 for m in merged if m["type"] == "reasoning") == 1


def test_merge_reasoning_is_a_noop_without_reasoning_sid_or_session():
    # empty reasoning text → skipped
    empty = [{"parts": [{"type": "reasoning", "messageID": "M1", "text": "   "}]}]
    assert _merge_reasoning_lines(_STREAM, empty) == _STREAM
    # no session → unchanged
    assert _merge_reasoning_lines(_STREAM, []) == _STREAM
    # no parseable sessionID in the stream → unchanged
    assert _merge_reasoning_lines("garbage\n", _SESSION) == "garbage\n"


def test_run_phase_backfills_reasoning_from_the_server_session(tmp_path):
    tee = tmp_path / "stream-async-0.jsonl"
    tee.write_text(_STREAM)

    class _SessionTransport(_FakeTransport):
        async def run_command(self, handle, cmd, *, stdin=None, timeout=None,
                              cwd=None, env=None, tee_path=None):
            self.run_calls.append((handle.name, cmd, timeout))
            joined = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "/session/" in joined:      # the reasoning backfill fetch
                return CommandResult(exit_code=0, stdout=json.dumps(_SESSION).encode(),
                                     stderr=b"", timed_out=False)
            return CommandResult(exit_code=0, stdout=b"", stderr=b"", timed_out=False)

    t = _SessionTransport()
    mgr = OpenCodeAgentManager(transport=t, client=None)
    asyncio.run(mgr.run_phase(t, _handle(), "prompt", timeout=60, tee_path=str(tee)))

    merged = [json.loads(x) for x in tee.read_text().splitlines()]
    assert merged[0]["type"] == "reasoning"
    assert merged[0]["part"]["text"] == "The dev key should decrypt team2's flag."
    # the fetch hit opencode's session-messages endpoint for the sid stamped on
    # the stream (the root /session/... route; /api/... returns empty on 1.18.30)
    assert any("/session/S1/message" in (" ".join(c) if isinstance(c, list) else c)
               for _n, c, _t in t.run_calls)


def test_merge_reasoning_is_idempotent():
    once = _merge_reasoning_lines(_STREAM, _SESSION)
    twice = _merge_reasoning_lines(once, _SESSION)
    assert twice == once  # a second pass adds nothing (guard on existing reasoning)
    assert sum(1 for x in twice.splitlines() if json.loads(x)["type"] == "reasoning") == 1


def test_augment_lines_reports_timing_origin_for_inserted_lines():
    lines = _STREAM.splitlines()  # 3 lines, all messageID M1
    out_lines, origin = _augment_lines(lines, _SESSION)
    assert len(out_lines) == len(lines) + 1          # one reasoning inserted
    assert json.loads(out_lines[0])["type"] == "reasoning"
    # the reasoning line borrows the ts of the line it precedes (orig index 0);
    # every other output line maps to its own original index, shifted by the insert
    assert origin == [0, 0, 1, 2]


def test_rewrite_timing_stays_aligned_after_backfill():
    lines = _STREAM.splitlines()
    _out, origin = _augment_lines(lines, _SESSION)
    timing = "1\t100.0\n2\t110.0\n3\t120.0\n"      # original: 3 lines
    new = _rewrite_timing(timing, origin).splitlines()
    # 4 output lines get 4 keyed, contiguous entries (no gaps, no tail dropped)
    assert [t.split("\t")[0] for t in new] == ["1", "2", "3", "4"]
    # inserted reasoning (line 1) + the step_start it precedes (line 2) share the
    # step_start's original ts; the rest shift by one
    assert new == ["1\t100.0", "2\t100.0", "3\t110.0", "4\t120.0"]


def test_run_phase_realigns_timing_sidecar(tmp_path):
    tee = tmp_path / "stream-async-0.jsonl"
    tee.write_text(_STREAM)
    (tmp_path / "stream-async-0.jsonl.timing").write_text("1\t100.0\n2\t110.0\n3\t120.0\n")

    class _SessionTransport(_FakeTransport):
        async def run_command(self, handle, cmd, *, stdin=None, timeout=None,
                              cwd=None, env=None, tee_path=None):
            self.run_calls.append((handle.name, cmd, timeout))
            joined = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "/session/" in joined:
                return CommandResult(exit_code=0, stdout=json.dumps(_SESSION).encode(),
                                     stderr=b"", timed_out=False)
            return CommandResult(exit_code=0, stdout=b"", stderr=b"", timed_out=False)

    t = _SessionTransport()
    mgr = OpenCodeAgentManager(transport=t, client=None)
    asyncio.run(mgr.run_phase(t, _handle(), "prompt", timeout=60, tee_path=str(tee)))

    stream_n = len([x for x in tee.read_text().splitlines() if x.strip()])
    timing_lines = (tmp_path / "stream-async-0.jsonl.timing").read_text().splitlines()
    # sidecar has one entry per stream line, still keyed 1..N with no gaps
    assert [t.split("\t")[0] for t in timing_lines] == [str(i) for i in range(1, stream_n + 1)]


# ── (5) prompting: later phases get the short continuation cue ────────




def test_dry_run_diagnostics_tails_the_server_log():
    """A `run` client that exits 0 with empty output leaves its reason only in
    the persistent server's log, so the adapter hands that tail to the dry-run
    diagnostics under the name `opencode-serve`; an empty log contributes nothing."""
    import asyncio
    from agents.opencode import SERVE_LOG, OpenCodeAgentManager
    from agents.transport import CommandResult

    class T:
        def __init__(self, out):
            self.out, self.cmds = out, []

        async def run_command(self, handle, cmd, **kw):
            self.cmds.append(cmd)
            return CommandResult(exit_code=0, stdout=self.out, stderr=b"", timed_out=False)

    t = T(b"INFO service=session model=cyberarena/hy4-preview\nERROR ProviderModelNotFound\n")
    mgr = OpenCodeAgentManager(transport=t)
    got = asyncio.run(mgr.dry_run_diagnostics(t, object()))
    assert got == {"opencode-serve": t.out.decode()}
    assert SERVE_LOG in t.cmds[0][-1] and t.cmds[0][-1].startswith("tail -c")

    empty = T(b"\n")
    assert asyncio.run(OpenCodeAgentManager(transport=empty).dry_run_diagnostics(empty, object())) == {}


# ── mid-game server death recovery (glm/gogs b0002, 2026-09-18) ──────────────
def test_ensure_server_noop_when_reachable():
    """When the serve daemon answers /doc, _ensure_server must NOT restart it."""
    import asyncio
    t = _FakeTransport()
    t.next_result = CommandResult(exit_code=0, stdout=b"", stderr=b"", timed_out=False)
    mgr = OpenCodeAgentManager(transport=t, client=None)
    restarts = []
    mgr._start_server = lambda h: restarts.append(h.name)
    asyncio.run(mgr._ensure_server(t, _handle()))
    assert restarts == []
    # exactly one probe (the /doc curl), targeting SERVER_URL
    from agents.opencode import SERVER_URL
    assert len(t.run_calls) == 1 and SERVER_URL in t.run_calls[0][1][-1]


def test_ensure_server_restarts_when_dead():
    """A dead server (curl rc=7) is restarted so the persisted session resumes."""
    import asyncio
    t = _FakeTransport()
    t.next_result = CommandResult(exit_code=7, stdout=b"", stderr=b"", timed_out=False)
    mgr = OpenCodeAgentManager(transport=t, client=None)
    restarts = []
    mgr._start_server = lambda h: restarts.append(h.name)
    asyncio.run(mgr._ensure_server(t, _handle("team1_dev")))
    assert restarts == ["team1_dev"]


def test_run_phase_ensures_server_before_running():
    """Every chunk checks the server first, so a mid-game death costs one restart
    instead of failing the match."""
    import asyncio
    t = _FakeTransport()
    t.next_result = CommandResult(exit_code=0, stdout=b"", stderr=b"", timed_out=False)
    mgr = OpenCodeAgentManager(transport=t, client=None)
    ensured = []

    async def _spy(transport, handle):
        ensured.append(handle.name)
    mgr._ensure_server = _spy
    asyncio.run(mgr.run_phase(t, _handle(), "prompt", timeout=30, model="m", tee_path=None))
    assert ensured == ["team1_dev"]


# ── (7) timeout → abort the server-side turn; warmup prompt pre-empts reasoning ─


def test_run_phase_timeout_aborts_server_sessions():
    """Killing the `run` client on timeout leaves its turn running on the
    persistent server, and the next `run -c` would queue behind it — so a
    timed-out phase must POST /session/<id>/abort for every session."""
    transport = _FakeTransport(
        next_result=CommandResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True),
    )
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr._ensure_server = _noop_ensure

    async def go():
        return await mgr.run_phase(transport, _handle(), "p", timeout=1,
                                   model="hy4-preview")
    asyncio.run(go())

    aborts = [_shell_of(c) for c in transport.run_calls if "/abort" in _shell_of(c)]
    assert len(aborts) == 1
    assert f"{SERVER_URL}/session " in aborts[0] or f"{SERVER_URL}/session\n" in aborts[0] \
        or f"{SERVER_URL}/session" in aborts[0]
    assert f"-X POST {SERVER_URL}/session/$id/abort" in aborts[0]


def test_run_phase_success_does_not_abort_sessions():
    transport = _FakeTransport()
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr._ensure_server = _noop_ensure

    async def go():
        return await mgr.run_phase(transport, _handle(), "p", timeout=1,
                                   model="hy4-preview")
    asyncio.run(go())

    assert not [c for c in transport.run_calls if "/abort" in _shell_of(c)]


def test_run_phase_timeout_does_not_abort_for_non_hy4_model():
    """The server-side abort is scoped to NO_REASONING_PROBE_MODELS. A
    timed-out phase for any other opencode model keeps the prior behaviour:
    the base kill_session fires, but no session abort."""
    transport = _FakeTransport(
        next_result=CommandResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True),
    )
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr._ensure_server = _noop_ensure

    async def go():
        return await mgr.run_phase(transport, _handle(), "p", timeout=1,
                                   model="MiniMax-M3")
    asyncio.run(go())

    assert not [c for c in transport.run_calls if "/abort" in _shell_of(c)]
    assert len(transport.kill_calls) == 1  # base kill_session still fires


def _warmup_shell(model: str) -> str:
    transport = _FakeTransport()
    mgr = OpenCodeAgentManager(transport=transport, client=None)
    mgr.install(_handle(), _full_cfg(model=model))
    warmups = [_shell_of(c) for c in transport.run_calls
               if "opencode run --attach" in _shell_of(c)]
    assert warmups
    return warmups[0]


def test_warmup_prompt_pre_empts_reasoning_for_hy4_only():
    """hy4-preview can deliberate even "hi" for minutes, overrunning the warmup
    cap and leaving its turn running when the dry-run resumes the session — so
    ITS warmup says not to think. Every other model keeps the "hi" it always had."""
    hy4 = _warmup_shell("hy4-preview")
    assert "Do not think" in hy4 and " hi <" not in hy4
    other = _warmup_shell("MiniMax-M3")
    assert " hi < /dev/null" in other and "Do not think" not in other
