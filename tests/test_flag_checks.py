"""GameMgr.get_flag_checks — own-team flag-persistence status over MCP,
derived from the FLAG_PERSISTENCE timeline (end-of-round audit). The raw
ObservationStatus is collapsed to a defender-facing tri-state and the flag value
/ repair internals are never exposed. Offline (no docker)."""

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


def _audit(g, team, status, *, service="notes", store="default", round_id=0,
           detail="x", repair=None):
    """Seed a FLAG_PERSISTENCE event the way FlagServer.audit records it (via its
    EventSink in v3); get_flag_checks reads them off the timeline."""
    g.timeline.record(
        "FLAG_PERSISTENCE", team_id=team, round_id=round_id,
        payload={"service": service, "store": store, "status": status,
                 "initial_status": status, "detail": detail, "repair": repair},
    )


def test_empty_before_any_audit():
    g = _game()
    res = g.get_flag_checks("team1")
    assert res == {"team_id": "team1", "count": 0, "flags": []}


def test_tristate_mapping():
    g = _game()
    _audit(g, "team1", "PRESENT", store="a")
    _audit(g, "team1", "NOT_FOUND", store="b")
    _audit(g, "team1", "MISMATCH", store="c")
    _audit(g, "team1", "AUTH_REJECTED", store="d", repair="REPAIRED")
    _audit(g, "team1", "ERROR", store="e")
    by_store = {f["store"]: f["status"] for f in g.get_flag_checks("team1")["flags"]}
    assert by_store == {
        "a": "survived",
        "b": "lost_or_tampered",
        "c": "lost_or_tampered",
        "d": "inconclusive",     # AUTH_REJECTED + repair internals not surfaced
        "e": "inconclusive",
    }


def test_latest_round_per_store_wins():
    g = _game()
    _audit(g, "team1", "PRESENT", store="s", round_id=1)
    _audit(g, "team1", "MISMATCH", store="s", round_id=2)   # newer round → wins
    flags = g.get_flag_checks("team1")["flags"]
    assert len(flags) == 1
    assert flags[0]["round"] == 2 and flags[0]["status"] == "lost_or_tampered"


def test_scoped_to_own_team():
    g = _game()
    _audit(g, "team2", "PRESENT")           # opponent's own audit
    assert g.get_flag_checks("team1")["count"] == 0
    assert g.get_flag_checks("team2")["count"] == 1


def test_never_exposes_value_or_repair_internals():
    g = _game()
    _audit(g, "team1", "AUTH_REJECTED", repair="REPAIRED", detail="vault login rejected")
    (flag,) = g.get_flag_checks("team1")["flags"]
    assert set(flag) == {"service", "store", "round", "status"}
    assert "value" not in flag and "repair" not in flag and "detail" not in flag
    assert "initial_status" not in flag
