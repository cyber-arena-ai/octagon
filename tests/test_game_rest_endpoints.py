"""v3 (mcp-gateway prep): player-token REST mirrors of the GameMgr-coupled MCP
tools (attacks / report-ready / report-deploy-complete / restart). The
containerized mcp-gateway forwards the agent's X-Team-Token to these; GameMgr
resolves identity + executes. Offline (no docker on the tested paths)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from gamemgr.api import build_app
from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph

HDR = {"X-Team-Token": "tok1"}
HDR2 = {"X-Team-Token": "tok2"}
ADMIN = {"X-Admin-Token": "admintok"}


class _StubOrchestrator:
    network = None


def _app():
    teams = [Team(id="team1", dev="d1", prod="p1", token="tok1"),
             Team(id="team2", dev="d2", prod="p2", token="tok2")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000,
                                    deploy_on=["team1"])],
        rounds=RoundConfig(), agents={},
    )
    g = GameMgr(config=cfg, orchestrator=_StubOrchestrator())
    g._admin_token = "admintok"   # enable the admin-gated read surfaces
    return g, TestClient(build_app(g, asyncio.Event()))


# ── auth: every mirror rejects a bad/missing token with 401 ──────────────────


def test_all_endpoints_401_without_token():
    _, tc = _app()
    assert tc.get("/game/attacks").status_code == 401
    assert tc.post("/game/report/ready").status_code == 401
    assert tc.post("/game/report/deploy-complete", json={"service": "notes"}).status_code == 401
    assert tc.post("/game/restart", json={"service": "notes"}).status_code == 401
    # wrong token too
    assert tc.get("/game/attacks", headers={"X-Team-Token": "nope"}).status_code == 401


# ── attacks (own-team, token-scoped) ─────────────────────────────────────────


def test_attacks_scoped_to_token_team():
    g, tc = _app()
    r = tc.get("/game/attacks", headers=HDR)
    assert r.status_code == 200
    assert r.json()["team_id"] == "team1"


# ── report ready / deploy-complete ───────────────────────────────────────────


def test_report_ready_records_event():
    g, tc = _app()
    before = len(g.timeline.history())
    assert tc.post("/game/report/ready", headers=HDR).json() == {"ok": True}
    assert len(g.timeline.history()) == before + 1


def test_report_deploy_complete_validates_service():
    g, tc = _app()
    assert tc.post("/game/report/deploy-complete", json={"service": "ghost"},
                   headers=HDR).status_code == 400
    assert tc.post("/game/report/deploy-complete", json={"service": "notes"},
                   headers=HDR).json() == {"ok": True}


# ── restart (ValueError -> 400) ──────────────────────────────────────────────


def test_restart_unknown_service_400():
    g, tc = _app()
    r = tc.post("/game/restart", json={"service": "ghost"}, headers=HDR)
    assert r.status_code == 400
    assert "unknown service" in r.json()["detail"]


# ── MCP query events (panel visibility for agent polling) ────────────────────


def test_attacks_query_records_event():
    """GET /game/attacks records MCP_ATTACKS_QUERY so the live panel shows the
    agent polling its breach count; payload carries the returned counts."""
    from gamemgr.events import EventType

    g, tc = _app()
    before = len(g.timeline.history())
    r = tc.get("/game/attacks", headers=HDR)
    assert r.status_code == 200
    evs = [e for e in g.timeline.history() if e.type == EventType.MCP_ATTACKS_QUERY.value]
    assert len(g.timeline.history()) == before + 1
    assert evs and evs[-1].team_id == "team1"
    assert "total" in evs[-1].payload and "this_round" in evs[-1].payload


def test_game_state_endpoint_records_event_and_mirrors_status():
    """GET /game/state (agent get_game_state) is token-scoped, records
    MCP_STATE_QUERY, and returns the same shape as /game/status."""
    from gamemgr.events import EventType

    g, tc = _app()
    assert tc.get("/game/state").status_code == 401  # token required
    r = tc.get("/game/state", headers=HDR)
    assert r.status_code == 200
    assert r.json() == tc.get("/game/status").json()  # identical content
    evs = [e for e in g.timeline.history() if e.type == EventType.MCP_STATE_QUERY.value]
    assert evs and evs[-1].team_id == "team1"


def test_facility_status_poll_does_not_record_mcp_query():
    """The tokenless /game/status (facility polls) must NOT emit MCP_STATE_QUERY."""
    from gamemgr.events import EventType

    g, tc = _app()
    before = len(g.timeline.history())
    tc.get("/game/status")
    assert all(e.type != EventType.MCP_STATE_QUERY.value
               for e in g.timeline.history()[before:])


# ── panel/spectator read surface: admin-gated + team-scoped ──────────────────


def test_agents_and_metrics_require_a_token():
    """No token, or a wrong token, is 401 on both per-team read endpoints — a
    tokenless player can no longer read the opponent's transcript off
    /game/agents (the f1b2… cross-team flag-scrape channel)."""
    _, tc = _app()
    for path in ("/game/agents", "/game/metrics"):
        assert tc.get(path).status_code == 401, path
        assert tc.get(path, headers={"X-Team-Token": "nope"}).status_code == 401, path


def test_agents_metrics_open_to_admin_and_own_team():
    """Admin token and a valid team token are both accepted (200); content
    scoping is asserted below. Pre-run (run_dir None) returns the empty shape."""
    _, tc = _app()
    for path in ("/game/agents", "/game/metrics"):
        assert tc.get(path, headers=ADMIN).status_code == 200, path
        assert tc.get(path, headers=HDR).status_code == 200, path


def test_agents_team_token_sees_only_its_own_transcript(monkeypatch, tmp_path):
    """A team's X-Team-Token filters /game/agents to that team; admin sees all."""
    from gamemgr import trajectory
    g, tc = _app()
    g.run_dir = tmp_path
    monkeypatch.setattr(trajectory, "merge_by_team", lambda _t: [
        {"team_id": "team1", "events": []}, {"team_id": "team2", "events": []}])
    admin = [t["team_id"] for t in tc.get("/game/agents", headers=ADMIN).json()["transcripts"]]
    assert sorted(admin) == ["team1", "team2"]
    t1 = [t["team_id"] for t in tc.get("/game/agents", headers=HDR).json()["transcripts"]]
    assert t1 == ["team1"]
    t2 = [t["team_id"] for t in tc.get("/game/agents", headers=HDR2).json()["transcripts"]]
    assert t2 == ["team2"]


def test_metrics_team_token_sees_only_its_own(monkeypatch, tmp_path):
    """A team's X-Team-Token filters /game/metrics to that team; admin sees all."""
    import json
    g, tc = _app()
    g.run_dir = tmp_path
    monkeypatch.setattr(g, "persist_router_usage", lambda: None)  # no docker in tests
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "metrics.json").write_text(
        json.dumps({"team1": {"provider": "claude"}, "team2": {"provider": "codex"}}))
    assert sorted(tc.get("/game/metrics", headers=ADMIN).json()["teams"]) == ["team1", "team2"]
    assert list(tc.get("/game/metrics", headers=HDR).json()["teams"]) == ["team1"]
    assert list(tc.get("/game/metrics", headers=HDR2).json()["teams"]) == ["team2"]


def test_panel_and_topology_are_admin_only():
    """/panel and /topology need the admin token; a team token is NOT enough
    (403). /topology past the gate hits the run-not-started 409, proving the
    403 came from the gate and not from missing docker."""
    _, tc = _app()
    assert tc.get("/panel").status_code == 403
    assert tc.get("/panel", headers=HDR).status_code == 403
    assert tc.get("/panel", headers=ADMIN).status_code == 200
    assert tc.get("/topology", headers=HDR).status_code == 403
    assert tc.get("/topology", headers=ADMIN).status_code == 409


def test_status_open_but_events_admin_only():
    """/game/status stays tokenless (global phase/round); /game/events is
    admin-only now (the timeline carries both teams' scoring + activity)."""
    _, tc = _app()
    assert tc.get("/game/status").status_code == 200
    assert tc.get("/game/events").status_code == 403          # no token
    assert tc.get("/game/events", headers=HDR).status_code == 403   # team token not enough
    assert tc.get("/game/events", headers=ADMIN).status_code == 200


def test_events_stream_ws_rejects_without_admin():
    """The /events/stream WS (same timeline as /game/events) refuses a tokenless
    or player connection — closed 1008 on the handshake. (The admin-accept path
    is covered by test_m6_panel.test_events_stream_closes_when_shutting_down.)"""
    from starlette.websockets import WebSocketDisconnect

    _, tc = _app()
    for hdr in ({}, HDR):   # no token, then a team token — both refused
        with pytest.raises(WebSocketDisconnect):
            with tc.websocket_connect("/events/stream", headers=hdr) as ws:
                ws.receive_json()
