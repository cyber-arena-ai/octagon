"""GameMgr.attacks_received — count of accepted captures against a team's OWN
service(s), derived from the FLAG_SUBMITTED timeline. Offline (no docker)."""

from __future__ import annotations

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


class _StubOrchestrator:
    network = None


def _game() -> GameMgr:
    teams = [
        Team(id="team1", dev="t1d", prod="t1p", token="tok1"),
        Team(id="team2", dev="t2d", prod="t2p", token="tok2"),
    ]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000,
                                    deploy_on=["team1", "team2"])],
        rounds=RoundConfig(),
        agents={},
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def test_no_attacks_initially():
    g = _game()
    res = g.attacks_received("team1")
    assert res["total"] == 0
    assert res["this_round"] == 0
    assert res["by_service"] == {}
    assert res["team_id"] == "team1"


def _capture(g, submitter, victim, service="notes", *, round_id=0, accepted=True):
    """Seed a FLAG_SUBMITTED event (the flag-service records these via its
    EventSink in v3; attacks_received reads them off the timeline)."""
    g.timeline.record(
        "FLAG_SUBMITTED", team_id=submitter, round_id=round_id,
        payload={"accepted": accepted, "submitter_team_id": submitter,
                 "victim_team_id": victim, "service": service},
    )


def test_counts_accepted_capture_against_own_service():
    g = _game()
    _capture(g, "team2", "team1")  # team2 stole + submitted team1's flag

    res = g.attacks_received("team1")
    assert res["total"] == 1
    assert res["this_round"] == 1            # current round (0) == event round
    assert res["by_service"] == {"notes": 1}
    # team2's own service was not attacked.
    assert g.attacks_received("team2")["total"] == 0


def test_rejected_and_own_submissions_do_not_count():
    g = _game()
    _capture(g, "team1", "team1", accepted=False)   # OWN → rejected
    _capture(g, "team2", "team1", accepted=False)   # UNKNOWN → rejected
    assert g.attacks_received("team1")["total"] == 0


def test_only_accepted_captures_count():
    g = _game()
    _capture(g, "team2", "team1", accepted=True)
    _capture(g, "team2", "team1", accepted=False)   # e.g. a later DUPLICATE
    assert g.attacks_received("team1")["total"] == 1


def test_this_round_excludes_other_rounds():
    """`this_round` reflects only the current round (the live patch-holding
    signal); `total` is cumulative across rounds."""
    g = _game()  # current round is 0 (INIT)
    for rid in (0, 3):
        g.timeline.record(
            "FLAG_SUBMITTED", team_id="team2", round_id=rid,
            payload={"accepted": True, "victim_team_id": "team1", "service": "notes"},
        )
    res = g.attacks_received("team1")
    assert res["total"] == 2
    assert res["this_round"] == 1            # only the round-0 capture
    assert res["current_round"] == 0
