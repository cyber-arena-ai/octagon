"""DEFCON: restart_service machinery + MCP tool wire-up.

Mocks docker exec (we don't have docker in this sandbox) so the
async job lifecycle, event ordering, and in-flight rejection can be
verified independently. Real docker integration is exercised by the
ico live tests once those land.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


# ── Helpers ────────────────────────────────────────────────────────────


class _StubOrchestrator:
    """GameMgr only uses orchestrator.network during the round loop; tests
    that don't drive the loop can get away with a bare object."""

    network = None


def _make_game(challenge_name: str = "nautilus-ico") -> GameMgr:
    """Build a minimal GameMgr with one team + one challenge, no docker."""
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-team1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-team2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=[
            ChallengeConfig(name=challenge_name, type=challenge_name, port=4265, deploy_on=["team1", "team2"]),
        ],
        rounds=RoundConfig(),
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def _events_of(bus, type_: str) -> list:
    return [e for e in bus.history() if e.type == type_]


async def _wait_for(bus, type_: str, timeout: float = 5.0):
    """Spin until at least one event of `type_` lands on the bus."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _events_of(bus, type_):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {type_}; got {[e.type for e in bus.history()]}")


# ── Tests ──────────────────────────────────────────────────────────────


def test_restart_service_unknown_service_raises():
    """An unknown service name raises ValueError (MCP layer maps to InvalidParams)."""
    game = _make_game()

    async def run():
        with pytest.raises(ValueError, match="unknown service"):
            await game.restart_service("team1", "not-a-thing")

    asyncio.run(run())


def test_restart_service_notes_has_no_restart_handler():
    """NotesChallenge is attack-only (restart_handler = None); restart_service
    rejects it as not-defendable rather than crashing later."""
    game = _make_game(challenge_name="notes")

    async def run():
        with pytest.raises(ValueError, match="no restart handler"):
            await game.restart_service("team1", "notes")

    asyncio.run(run())


def test_restart_service_unknown_team():
    """A team not in topology raises ValueError."""
    game = _make_game()

    async def run():
        with pytest.raises(ValueError, match="unknown team"):
            await game.restart_service("team-bogus", "nautilus-ico")

    asyncio.run(run())


def test_restart_service_rejected_during_attack():
    """restart is DEFENSE-only: during ATTACK it's rejected without a
    rebuild and surfaces SERVICE_RESTART_REJECTED — NOT _FAILED (so the
    healthy-run 'restarts failed' count stays clean). This blocks the
    attack agent's --continue session from finishing a leftover defense
    restart in the attack phase."""
    import types
    from gamemgr.round import Phase

    game = _make_game()
    game.round_loop = types.SimpleNamespace(phase=Phase.ATTACK, round=1)
    bus = game.timeline

    async def run():
        with pytest.raises(ValueError, match="DEFENSE-only"):
            await game.restart_service("team1", "nautilus-ico")

    asyncio.run(run())
    assert len(_events_of(bus, EventType.SERVICE_RESTART_REJECTED.value)) == 1
    assert _events_of(bus, EventType.SERVICE_RESTART_STARTED.value) == []
    assert _events_of(bus, EventType.SERVICE_RESTART_FAILED.value) == []


def test_restart_service_happy_path_emits_started_completed_then_probes(monkeypatch):
    """Mock docker exec to a no-op success; assert STARTED/COMPLETED and that the
    post-restart probe is delegated to the (containerized) health-poller. v3: the
    SERVICE_HEALTH_CHECK chain is emitted by the poller container over its
    EventSink (covered in test_health_poller_container), not here — so we assert
    GameMgr signals trigger_now, not the chain itself."""
    game = _make_game()
    bus = game.timeline

    triggered: list[tuple[str, str]] = []

    class _FakePoller:
        name = "health"
        async def trigger_now(self, team_id, service):
            triggered.append((team_id, service))

    game.health_poller = _FakePoller()

    def fake_exec(self, container_name, cmd):
        return (0, "ok\n")

    with patch.object(GameMgr, "_exec_in_container", fake_exec):
        async def run():
            job_id = await game.restart_service("team1", "nautilus-ico")
            assert isinstance(job_id, str) and len(job_id) == 12
            await _wait_for(bus, EventType.SERVICE_RESTART_COMPLETED.value)
            for _ in range(50):
                if triggered:
                    break
                await asyncio.sleep(0.02)

            started = _events_of(bus, EventType.SERVICE_RESTART_STARTED.value)
            completed = _events_of(bus, EventType.SERVICE_RESTART_COMPLETED.value)
            assert len(started) == 1 and len(completed) == 1
            assert started[0].payload["job_id"] == completed[0].payload["job_id"] == job_id
            assert completed[0].payload["duration_ms"] >= 0
            assert _events_of(bus, EventType.SERVICE_RESTART_FAILED.value) == []
            # Post-restart probe delegated to the health-poller.
            assert triggered == [("team1", "nautilus-ico")]

        asyncio.run(run())


def test_restart_service_failed_path_emits_failed_no_health(monkeypatch):
    """A non-zero exit from restart_cmd → FAILED event, no health chain runs."""
    game = _make_game()
    bus = game.timeline

    def fake_exec(self, container_name, cmd):
        return (1, "fpc: syntax error in ico.pas\n")

    with patch.object(GameMgr, "_exec_in_container", fake_exec):
        async def run():
            job_id = await game.restart_service("team1", "nautilus-ico")
            await _wait_for(bus, EventType.SERVICE_RESTART_FAILED.value)

            failed = _events_of(bus, EventType.SERVICE_RESTART_FAILED.value)
            assert len(failed) == 1
            assert failed[0].payload["job_id"] == job_id
            assert "syntax error" in failed[0].payload["error"]

            # No COMPLETED. No health checks (we didn't get past the restart).
            assert _events_of(bus, EventType.SERVICE_RESTART_COMPLETED.value) == []
            assert _events_of(bus, EventType.SERVICE_HEALTH_CHECK.value) == []

        asyncio.run(run())


def test_restart_service_in_flight_rejected():
    """A second restart for the same (team, service) while the first is
    still running rejects with ValueError → InvalidParams at MCP layer."""
    game = _make_game()
    started_event = threading.Event()
    block_event = threading.Event()

    def slow_exec(self, container_name, cmd):
        # Signal that we've started, then block until release.
        started_event.set()
        block_event.wait(timeout=5)
        return (0, "ok\n")

    with patch.object(GameMgr, "_exec_in_container", slow_exec):
        async def run():
            job1 = await game.restart_service("team1", "nautilus-ico")
            # Wait until the background task has actually entered _exec_in_container,
            # i.e. the in-flight record exists and is not yet "done".
            await asyncio.get_running_loop().run_in_executor(None, started_event.wait, 5)
            # Second restart for same (team, service) should reject.
            with pytest.raises(ValueError, match="already in flight"):
                await game.restart_service("team1", "nautilus-ico")
            # A different team is fine.
            job_other_team = await game.restart_service("team2", "nautilus-ico")
            assert job_other_team != job1
            block_event.set()  # let the workers finish

        asyncio.run(run())


# ── restart output teed to a timestamped log on prod ─────────────────────────

def test_tee_restart_exec_wraps_and_preserves_rc():
    """_tee_restart_exec base64-wraps the inner command (so heredoc probes
    survive), tees combined output to the log path, and passes the real exit
    status + output straight back to the handler."""
    import base64
    seen = {}
    def orig(container, cmd):
        seen["container"], seen["cmd"] = container, cmd
        return (7, "raw restart output\n")

    wrapped = GameMgr._tee_restart_exec(orig, "/arena/restart-logs/restart-X.log")
    rc, out = wrapped("team1_prod", "/arena/restart.sh")

    assert (rc, out) == (7, "raw restart output\n")        # handler sees orig's result
    c = seen["cmd"]
    assert seen["container"] == "team1_prod"
    assert base64.b64encode(b"/arena/restart.sh").decode() in c   # inner cmd shipped b64
    assert "base64 -d | sh" in c                                  # decoded + run
    assert "tee -a /arena/restart-logs/restart-X.log" in c        # teed to the log
    assert "mkdir -p" in c and 'exit "${rc:-1}"' in c             # dir made, rc guarded


def _is_logging_probe(cmd: str) -> bool:
    return "command -v base64" in cmd and "tee -a" not in cmd


def test_restart_service_completed_event_carries_teed_log_path(monkeypatch):
    """Happy path (logging tools present): the run is teed to a NEW timestamped
    file under /arena/restart-logs/ carrying this job_id, and every restart-run
    command (not the tool probe) reaches docker exec tee-wrapped to that file."""
    game = _make_game()
    bus = game.timeline

    class _FakePoller:
        name = "health"
        async def trigger_now(self, team_id, service): pass
    game.health_poller = _FakePoller()

    cmds: list[str] = []
    def fake_exec(self, container_name, cmd):
        cmds.append(cmd)
        return (0, "ok\n")                    # probe passes → tools "available"

    with patch.object(GameMgr, "_exec_in_container", fake_exec):
        async def run():
            job_id = await game.restart_service("team1", "nautilus-ico")
            await _wait_for(bus, EventType.SERVICE_RESTART_COMPLETED.value)
            completed = _events_of(bus, EventType.SERVICE_RESTART_COMPLETED.value)[0]
            log_path = completed.payload["restart_log"]
            assert log_path.startswith("/arena/restart-logs/restart-")
            assert log_path.endswith(f"-{job_id}.log")            # correlatable to the job
            restart_cmds = [c for c in cmds if not _is_logging_probe(c)]
            assert restart_cmds and all(f"tee -a {log_path}" in c for c in restart_cmds)

        asyncio.run(run())


def test_restart_falls_back_to_raw_when_logging_unavailable(monkeypatch):
    """BEST-EFFORT: if the prod image lacks base64/mktemp/tee, the restart still
    runs — RAW (unwrapped), never failing because of the log. restart_log is
    None on the event, and no restart command is tee-wrapped."""
    game = _make_game()
    bus = game.timeline

    class _FakePoller:
        name = "health"
        async def trigger_now(self, team_id, service): pass
    game.health_poller = _FakePoller()

    cmds: list[str] = []
    def fake_exec(self, container_name, cmd):
        cmds.append(cmd)
        if _is_logging_probe(cmd):
            return (1, "")                    # tools MISSING → fall back to raw
        return (0, "ok\n")

    with patch.object(GameMgr, "_exec_in_container", fake_exec):
        async def run():
            await game.restart_service("team1", "nautilus-ico")
            await _wait_for(bus, EventType.SERVICE_RESTART_COMPLETED.value)
            completed = _events_of(bus, EventType.SERVICE_RESTART_COMPLETED.value)[0]
            assert completed.payload["restart_log"] is None       # no log captured
            # restart still ran, and NO command was tee-wrapped
            restart_cmds = [c for c in cmds if not _is_logging_probe(c)]
            assert restart_cmds and not any("tee -a /arena/restart-logs" in c for c in restart_cmds)

        asyncio.run(run())
