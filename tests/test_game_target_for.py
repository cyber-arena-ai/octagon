"""Unit test for GameMgr.target_for — the v2 helper that builds a
VulboxTarget for {team_id, challenge}.

Pure construction logic; no docker needed. The challenge instance is the
real NotesChallenge (cheap to construct) so we also pin the wiring
between Challenge.vulbox.exposed_ports and VulboxTarget.ports.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from challenges.interface import VulboxTarget
from challenges.notes import NotesChallenge
from facility import ExecResult
from gamemgr.config import GameConfig
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


def _minimal_cfg() -> GameConfig:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod"),
        Team(id="team2", dev="team2_dev", prod="team2_prod"),
    ]
    return GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[],
    )


class _FakeOrch:
    """GameMgr only touches `orchestrator` from start()/restart paths;
    target_for itself doesn't reach for it, so a sentinel is enough."""


def test_target_for_builds_privileged_target():
    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    ch = NotesChallenge()
    target = g.target_for("team1", ch)

    assert isinstance(target, VulboxTarget)
    assert target.host == "team1_prod"
    # Ports are copied from the challenge's vulbox, NOT shared by ref.
    assert target.ports == {"service": 5000}
    assert target.ports is not ch.vulbox.exposed_ports
    assert target.meta["team_id"] == "team1"
    # exec_in_container satisfies the challenge contract: callable as
    # (container_name, cmd) -> (rc, output). It's now a shim over the
    # facility ExecPort (see test below), not the raw bound method.
    assert callable(target.meta["exec_in_container"])


def test_target_for_exec_routes_through_execport_by_identity():
    """The challenge-facing exec callable delegates to GameMgr's host ExecPort,
    addressed by (team_id, "prod") — the raw container_name arg the handler
    passes is ignored (the port resolves the container)."""
    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    ch = NotesChallenge()

    calls: list[tuple[str, str, str]] = []

    class _FakeExec:
        def exec(self, team_id, role, cmd, *, timeout_s=120.0):
            calls.append((team_id, role, cmd))
            return ExecResult(exit_code=0, stdout="hi")

    g._exec_port = _FakeExec()
    target = g.target_for("team1", ch)

    rc, out = target.meta["exec_in_container"]("ignored_container", "echo hi")
    assert (rc, out) == (0, "hi")
    assert calls == [("team1", "prod", "echo hi")]


def test_target_for_different_teams_get_different_hosts():
    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    ch = NotesChallenge()
    assert g.target_for("team1", ch).host == "team1_prod"
    assert g.target_for("team2", ch).host == "team2_prod"


def test_target_for_unknown_team_raises():
    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    ch = NotesChallenge()
    with pytest.raises(KeyError, match="unknown team_id 'teamX'"):
        g.target_for("teamX", ch)


def test_record_container_ready_tags_dev_vs_prod():
    from gamemgr.events import EventType
    from models.topology import Node

    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    g.record_container_ready(Node(id="team1_dev", image="cyberarena/agent-base", networks=[]))
    g.record_container_ready(Node(id="team2_prod", image="cyberarena/chal-x", networks=[]))

    ready = [e for e in g.timeline.history() if e.type == EventType.CONTAINER_READY.value]
    by_node = {e.payload["node"]: e.payload for e in ready}
    assert by_node["team1_dev"]["role"] == "dev"
    assert by_node["team2_prod"]["role"] == "prod"
    assert by_node["team1_dev"]["image"] == "cyberarena/agent-base"


def test_record_setup_complete_summarizes_arena():
    from gamemgr.events import EventType

    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    g.record_setup_complete()
    ev = next(e for e in g.timeline.history() if e.type == EventType.SETUP_COMPLETE.value)
    # Says WHAT came up, not just "done".
    assert ev.payload["containers"] == len(g.config.topology.nodes)
    assert "services" in ev.payload
    assert "flag" in ev.payload["facilities"]   # flag_server exists from __init__


def test_stop_drains_inflight_kickoff_tasks():
    """stop() awaits tracked kickoff tasks to completion before returning,
    so the final round's record_phase_done lands before timeline.close()."""
    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    done = []

    async def run():
        async def slow():
            await asyncio.sleep(0.05)
            done.append(1)
        t = asyncio.create_task(slow())
        g._track_kickoffs([t])
        await g.stop()
        assert done == [1]          # drained to completion
        assert not t.cancelled()

    asyncio.run(run())


def test_stop_cancels_kickoffs_past_grace():
    """A wedged kickoff is cancelled at the grace deadline, not allowed to
    hang teardown."""
    g = GameMgr(config=_minimal_cfg(), orchestrator=_FakeOrch())
    g._STOP_DRAIN_GRACE_SECS = 0.05

    async def run():
        async def wedged():
            await asyncio.sleep(5)
        t = asyncio.create_task(wedged())
        g._track_kickoffs([t])
        await g.stop()
        await asyncio.sleep(0)      # let the cancellation propagate
        assert t.cancelled()

    asyncio.run(run())
