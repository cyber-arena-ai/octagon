"""Persistent-memory v1 — per-phase invocation, in three layers.

  1. `ClaudeAgentManager.phase_command(model)` — pure CLI argv builder.
     No transport, no I/O. Just check what shell command we'd run.

  2. `AgentManager.run_phase(...)` — the 3-line dance: write prompt →
     run_command → kill_session on timeout. Tested against an in-memory
     FakeTransport.

  3. `GameMgr.record_phase_done(...)` — artifact persistence + event
     emission. Independent of the transport.

See docs/persistent_memory_agent_plan_v1.md.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agents.claude import (
    ClaudeAgentManager,
    ENV_PATH,
    MCP_CONFIG_PATH,
    PROMPT_PATH,
    WORKDIR,
)
from agents.transport import AgentTransport, CommandResult
from gamemgr.artifacts import ArtifactStore
from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType
from gamemgr.game_mgr import GameMgr
from models.agent import AgentConfig
from models.topology import Team, TopologyGraph
from orchestrator.handle import ContainerHandle


# ── FakeTransport: in-memory AgentTransport for the runner tests ──────


@dataclass
class _FakeTransport(AgentTransport):
    files: dict[str, bytes] = field(default_factory=dict)
    run_calls: list[tuple[str, list[str] | str, float | None]] = field(default_factory=list)
    kill_calls: list[tuple[str, str]] = field(default_factory=list)
    next_result: CommandResult = field(
        default_factory=lambda: CommandResult(
            exit_code=0,
            stdout=b'{"ok": true}\n',
            stderr=b"",
            timed_out=False,
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


# ── (1) ClaudeAgentManager.phase_command — pure argv builder ──────────


def test_phase_command_contains_continue_workdir_envfile_and_stdin_redirect():
    """The load-bearing pieces for persistent memory: --continue, the
    cwd-pinned workdir (so session selection is isolated), and stdin
    redirect from PROMPT_PATH (the CLI reads the user prompt from stdin
    in -p mode)."""
    mgr = ClaudeAgentManager(transport=_FakeTransport(), client=None)
    argv = mgr.phase_command()

    assert argv[0] == "bash"
    assert argv[1] == "-c"
    shell = argv[2]
    assert "claude -p --continue" in shell
    assert WORKDIR in shell
    assert ENV_PATH in shell
    assert MCP_CONFIG_PATH in shell
    assert PROMPT_PATH in shell
    assert "< " in shell  # stdin redirect


def test_phase_command_model_default_vs_explicit_override():
    """Default: shell sources $CLAUDE_MODEL (from agent.env).
    Explicit `model=` injects a literal `--model <name>` and drops the env-var fallback."""
    mgr = ClaudeAgentManager(transport=_FakeTransport(), client=None)

    default_shell = mgr.phase_command()[2]
    assert '--model "$CLAUDE_MODEL"' in default_shell

    override_shell = mgr.phase_command(model="claude-haiku-4-5")[2]
    assert "--model claude-haiku-4-5" in override_shell or "--model 'claude-haiku-4-5'" in override_shell
    assert "$CLAUDE_MODEL" not in override_shell


def test_phase_command_disables_server_side_web_search():
    # orche #95/#109: claude-code's `WebSearch` is provider-hosted and runs
    # server-side, bypassing the arena egress firewall (results ride the
    # NO_PROXY'd LLM-gateway channel, never the :3128 default-deny proxy).
    # The adapter must pass --disallowedTools WebSearch so web access is
    # only reachable client-side through the container egress proxy.
    mgr = ClaudeAgentManager(transport=_FakeTransport(), client=None)
    shell = mgr.phase_command()[2]
    assert "--disallowedTools WebSearch" in shell
    # WebFetch stays enabled (client-side, already proxy-blocked+logged)
    assert "WebFetch" not in shell


def test_prompt_path_and_kill_pattern_match_abc_contract():
    """Both must be class-level attributes (so the runner can read them
    without instantiating the manager more than once)."""
    assert ClaudeAgentManager.PROMPT_PATH == PROMPT_PATH
    assert ClaudeAgentManager.PHASE_KILL_PATTERN == "claude -p"


# ── (2) AgentManager.run_phase — 3-line dance ─────────────────────────


def test_run_phase_writes_prompt_then_invokes_phase_command():
    transport = _FakeTransport()
    mgr = ClaudeAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(
            transport, _handle(), "do the thing", timeout=10,
        )
    asyncio.run(go())

    # Prompt was written to PROMPT_PATH.
    assert transport.files[PROMPT_PATH] == b"do the thing"
    # The shell argv passed to run_command came from mgr.phase_command()
    # with a per-invocation session marker baked into the bash wrapper.
    # The marker is random; assert the shape (setsid wrapper + pidfile)
    # rather than literal equality with the default phase_command().
    assert len(transport.run_calls) == 1
    container_name, cmd, timeout = transport.run_calls[0]
    assert container_name == "team1_dev"
    assert timeout == 10
    assert cmd[0] == "bash" and cmd[1] == "-c"
    shell = cmd[2]
    assert "setsid -w bash -c" in shell, shell
    assert "/tmp/cyberarena_phase_" in shell and ".pid" in shell, shell
    assert "claude -p --continue" in shell
    # The per-invocation marker is exported into claude's env so
    # kill_session can reap it via /proc/<pid>/environ.
    assert "export CYBERARENA_PHASE=" in shell, shell


def test_run_phase_returns_raw_command_result():
    """run_phase returns the transport's CommandResult unchanged —
    caller decides what to do with stdout/stderr (persistence is
    GameMgr's job)."""
    transport = _FakeTransport(
        next_result=CommandResult(
            exit_code=0,
            stdout=b'{"transcript":"hi"}\n',
            stderr=b"some warnings\n",
            timed_out=False,
        )
    )
    mgr = ClaudeAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(
            transport, _handle(), "p", timeout=5,
        )
    result = asyncio.run(go())

    assert result.exit_code == 0
    assert result.stdout == b'{"transcript":"hi"}\n'
    assert result.stderr == b"some warnings\n"
    assert result.timed_out is False


def test_run_phase_timeout_fires_kill_session_with_invocation_marker():
    """On timeout run_phase calls transport.kill_session with the
    per-invocation session marker — precision SIGKILL on the timed-out
    claude's process group (not a pattern that would race with a
    sibling claude started by the next phase)."""
    transport = _FakeTransport(
        next_result=CommandResult(
            exit_code=-1, stdout=b"partial\n", stderr=b"", timed_out=True,
        )
    )
    mgr = ClaudeAgentManager(transport=transport, client=None)

    async def go():
        return await mgr.run_phase(
            transport, _handle("team2_dev"), "p", timeout=1,
        )
    result = asyncio.run(go())

    assert result.timed_out is True
    assert len(transport.kill_calls) == 1
    container, kind, marker = transport.kill_calls[0]
    assert container == "team2_dev"
    assert kind == "session"
    # secrets.token_hex(6) → 12 hex chars
    assert len(marker) == 12 and all(c in "0123456789abcdef" for c in marker)
    # The same marker must have been baked into phase_command's shell.
    shell = transport.run_calls[0][1][2]
    assert marker in shell, f"marker {marker!r} not found in shell {shell!r}"


def test_run_phase_no_kill_on_clean_exit():
    transport = _FakeTransport()  # default: clean exit
    mgr = ClaudeAgentManager(transport=transport, client=None)

    async def go():
        await mgr.run_phase(transport, _handle(), "p", timeout=10)

    asyncio.run(go())
    assert transport.kill_calls == []


# ── (3) GameMgr.record_phase_done — artifacts + events ────────────────


class _StubOrchestrator:
    network = None


def _make_game_for_persistence(tmp_path: Path) -> GameMgr:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1"])],
        rounds=RoundConfig(),
    )
    game = GameMgr(config=cfg, orchestrator=_StubOrchestrator())
    game.artifacts = ArtifactStore(tmp_path)
    return game


def test_record_phase_done_writes_three_artifacts(tmp_path):
    game = _make_game_for_persistence(tmp_path)
    result = CommandResult(
        exit_code=0,
        stdout=b'{"line": 1}\n{"line": 2}\n',
        stderr=b"some stderr\n",
        timed_out=False,
    )

    async def go():
        await game.record_phase_done(
            "team1", round_id=2, phase="defense",
            prompt="first line\nsecond line\n",
            result=result, started_at=1000.0, ended_at=1042.5,
        )
    asyncio.run(go())

    base = tmp_path / "artifacts" / "team1" / "round-2"
    assert (base / "stream-defense.jsonl").read_bytes() == b'{"line": 1}\n{"line": 2}\n'
    # agent-*.log is the raw combined archive: stdout followed by stderr.
    assert (base / "agent-defense.log").read_bytes() == b'{"line": 1}\n{"line": 2}\nsome stderr\n'
    meta = json.loads((base / "metadata-defense.json").read_text())
    assert meta["round_id"] == 2
    assert meta["phase"] == "defense"
    assert meta["exit_code"] == 0
    assert meta["timed_out"] is False
    assert meta["duration_ms"] == 42500
    assert meta["prompt_first_line"] == "first line"


def test_record_phase_done_emits_agent_completed_on_clean_exit(tmp_path):
    game = _make_game_for_persistence(tmp_path)
    result = CommandResult(exit_code=0, stdout=b"", stderr=b"", timed_out=False)

    async def go():
        await game.record_phase_done(
            "team1", round_id=1, phase="attack", prompt="p",
            result=result, started_at=0.0, ended_at=1.0,
        )
    asyncio.run(go())

    events = [e for e in game.timeline.history() if e.team_id == "team1"]
    assert any(e.type == EventType.AGENT_COMPLETED.value for e in events), events
    assert not any(e.type in (EventType.AGENT_TIMEOUT.value, EventType.AGENT_FAILED.value) for e in events)


def test_record_phase_done_emits_agent_timeout_when_timed_out(tmp_path):
    game = _make_game_for_persistence(tmp_path)
    result = CommandResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True)

    async def go():
        await game.record_phase_done(
            "team1", round_id=1, phase="defense", prompt="p",
            result=result, started_at=0.0, ended_at=120.0,
        )
    asyncio.run(go())

    events = [e for e in game.timeline.history()]
    timeouts = [e for e in events if e.type == EventType.AGENT_TIMEOUT.value]
    assert len(timeouts) == 1
    assert timeouts[0].payload["duration_ms"] == 120000


def test_record_phase_done_emits_agent_failed_on_nonzero_exit(tmp_path):
    """Non-zero exit AND not timed_out → AGENT_FAILED with error_excerpt."""
    game = _make_game_for_persistence(tmp_path)
    result = CommandResult(exit_code=137, stdout=b"", stderr=b"", timed_out=False)

    async def go():
        await game.record_phase_done(
            "team1", round_id=1, phase="defense", prompt="p",
            result=result, started_at=0.0, ended_at=1.0,
        )
    asyncio.run(go())

    failed = [e for e in game.timeline.history() if e.type == EventType.AGENT_FAILED.value]
    assert len(failed) == 1
    assert "non-zero exit 137" in failed[0].payload.get("error_excerpt", "")


def test_record_phase_done_artifact_write_failure_still_emits_event(tmp_path):
    """If artifact write raises, the bus event still fires so observers
    aren't left hanging."""
    game = _make_game_for_persistence(tmp_path)
    def boom(*a, **kw):
        raise OSError("simulated disk full")
    game.artifacts.path_for = boom  # type: ignore[assignment]

    result = CommandResult(exit_code=0, stdout=b"", stderr=b"", timed_out=False)

    async def go():
        # Should not raise.
        await game.record_phase_done(
            "team1", round_id=1, phase="attack", prompt="p",
            result=result, started_at=0.0, ended_at=1.0,
        )
    asyncio.run(go())

    completed = [e for e in game.timeline.history() if e.type == EventType.AGENT_COMPLETED.value]
    assert len(completed) == 1, "event must publish even when artifact write failed"
