"""B-0: facility HTTP boundary — HttpStatusPort / HttpEventSink adapters and the
GameMgr event-ingest endpoint (facility-token auth + idempotency). Offline."""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from challenges.interface import (
    StateReadStatus,
    StateRegionSpec,
    VulboxTarget,
)
from facility.adapters_http import (
    HttpAuthoritativeStatePort,
    HttpEventSink,
    HttpStatusPort,
)
from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


class _StubOrchestrator:
    network = None


def _game() -> GameMgr:
    teams = [Team(id="team1", dev="d1", prod="p1", token="tok1"),
             Team(id="team2", dev="d2", prod="p2", token="tok2")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000,
                                    deploy_on=["team1", "team2"])],
        rounds=RoundConfig(), agents={},
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


# ── GameMgr ingest + auth (unit) ──────────────────────────────────────────


def test_facility_token_fail_closed_when_unset():
    g = _game()  # no CYBERARENA_FACILITY_TOKEN
    assert g.facility_token_ok("anything") is False
    assert g.facility_token_ok(None) is False


def test_facility_token_match_and_reject():
    g = _game()
    g._facility_token = "fac-tok"
    assert g.facility_token_ok("fac-tok") is True
    assert g.facility_token_ok("nope") is False
    assert g.facility_token_ok(g.config.topology.teams[0].token) is False  # player token ≠ facility


def test_admin_token_fail_closed_when_unset():
    g = _game()  # no CYBERARENA_ADMIN_TOKEN
    assert g.admin_token_ok("anything") is False
    assert g.admin_token_ok(None) is False


def test_admin_token_match_and_reject():
    g = _game()
    g._admin_token = "adm-tok"
    g._facility_token = "fac-tok"
    assert g.admin_token_ok("adm-tok") is True
    assert g.admin_token_ok("nope") is False
    assert g.admin_token_ok(g.config.topology.teams[0].token) is False  # player token ≠ admin
    assert g.admin_token_ok("fac-tok") is False  # facility token ≠ admin


def test_ingest_event_idempotent():
    g = _game()
    before = len(g.timeline.history())
    assert g.ingest_event(event_id="e1", type_="SERVICE_HEALTH_CHECK", team_id="team1") is True
    assert g.ingest_event(event_id="e1", type_="SERVICE_HEALTH_CHECK", team_id="team1") is False
    assert len(g.timeline.history()) == before + 1  # duplicate collapsed


# ── /facility/events endpoint (auth + record) ─────────────────────────────


def _app(g):
    import asyncio
    from gamemgr.api import build_app
    return TestClient(build_app(g, asyncio.Event()))


def test_facility_events_requires_token():
    g = _game(); g._facility_token = "fac-tok"
    tc = _app(g)
    before = len(g.timeline.history())
    r = tc.post("/facility/events", json={"event_id": "x", "type": "T"})  # no token
    assert r.status_code == 403
    r = tc.post("/facility/events", json={"event_id": "x", "type": "T"},
                headers={"X-Facility-Token": "wrong"})
    assert r.status_code == 403
    assert len(g.timeline.history()) == before  # nothing recorded


def test_facility_events_records_and_dedups():
    g = _game(); g._facility_token = "fac-tok"
    tc = _app(g)
    before = len(g.timeline.history())
    hdr = {"X-Facility-Token": "fac-tok"}
    body = {"event_id": "e42", "type": "SERVICE_HEALTH_CHECK", "team_id": "team1",
            "round_id": 2, "payload": {"service": "notes", "passed": True}}
    r1 = tc.post("/facility/events", json=body, headers=hdr)
    r2 = tc.post("/facility/events", json=body, headers=hdr)  # resend (idempotent)
    assert r1.json() == {"ok": True, "recorded": True}
    assert r2.json() == {"ok": True, "recorded": False}
    assert len(g.timeline.history()) == before + 1


# ── adapters drive the endpoints ──────────────────────────────────────────


def test_status_returns_empty_on_timeout():
    """#38: a slow/unreachable /game/status must NOT raise — status() returns {}
    (symmetric with record()) so callers degrade instead of 500ing."""
    def host(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=req)

    client = httpx.Client(transport=httpx.MockTransport(host))
    assert HttpStatusPort("http://host", client=client).status() == {}


def test_http_adapters_against_live_endpoints():
    g = _game(); g._facility_token = "fac-tok"
    tc = _app(g)
    # base_url "" + TestClient → relative requests against the app.
    assert HttpStatusPort("", client=tc).status() == g.status()

    before = len(g.timeline.history())
    sink = HttpEventSink("", "fac-tok", client=tc)
    out = sink.record("FLAG_SUBMITTED", team_id="team1", round_id=1,
                      payload={"accepted": True})
    assert out and out["id"]
    assert len(g.timeline.history()) == before + 1
    last = g.timeline.history()[-1]
    assert last.type == "FLAG_SUBMITTED" and last.payload.get("accepted") is True


# ── authoritative-state read (the optional prod-free retrieve backend) ───────


class _StateContainers:
    def __init__(self):
        self.calls = []

    def read_files(self, container, path, *, max_bytes):
        self.calls.append((container, path, max_bytes))
        return {"config.xml": b"<description>MARK{ABC}</description>"}


def test_authoritative_state_endpoint_is_scoped_and_facility_only():
    g = _game()
    g._facility_token = "fac-tok"
    state_containers = _StateContainers()
    g.orchestrator.containers = state_containers
    g._state_regions[("team1", "notes", "job-config")] = StateRegionSpec(
        "job-config", "/srv/app/config.xml", max_bytes=4096,
    )
    tc = _app(g)
    body = {"team_id": "team1", "service": "notes", "region": "job-config"}

    # A player token (or none) never reaches prod state.
    assert tc.post("/facility/state/read", json=body).status_code == 403
    assert state_containers.calls == []

    port = HttpAuthoritativeStatePort("", "fac-tok", client=tc)
    result = port.read("team1", "notes", "job-config")
    assert result.status is StateReadStatus.OK
    assert result.files == {"config.xml": b"<description>MARK{ABC}</description>"}
    # The PATH came from trusted challenge metadata, not from the request.
    assert state_containers.calls == [("p1", "/srv/app/config.xml", 4096)]


def test_authoritative_state_reader_rejects_unknown_identity_without_reading():
    g = _game()
    g._facility_token = "fac-tok"
    state_containers = _StateContainers()
    g.orchestrator.containers = state_containers
    g._state_regions[("team1", "notes", "allowed")] = StateRegionSpec(
        "allowed", "/srv/app/private",
    )
    port = HttpAuthoritativeStatePort("", "fac-tok", client=_app(g))

    assert port.read("team2", "notes", "allowed").status is StateReadStatus.ERROR
    assert port.read("team1", "other", "allowed").status is StateReadStatus.ERROR
    # A path-shaped "region" is just an unknown name — never a path.
    assert port.read("team1", "notes", "/srv/app/private").status is StateReadStatus.ERROR
    assert state_containers.calls == []


def test_authoritative_state_http_adapter_fails_closed_on_bad_payload():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda _req: httpx.Response(200, json={"status": "OK", "files": {"x": "***"}})
    ))
    result = HttpAuthoritativeStatePort(
        "http://host", "fac-tok", client=client,
    ).read("team1", "notes", "region")
    assert result.status is StateReadStatus.ERROR


def test_deploy_registers_declared_regions_for_each_victim(monkeypatch):
    from challenges.registry import build_challenge
    import gamemgr.game_mgr as game_mgr_module

    challenge = build_challenge("notes")
    monkeypatch.setattr(
        challenge, "authoritative_state_regions",
        lambda: [StateRegionSpec("private-note", "/srv/notes/private")],
    )
    monkeypatch.setattr(challenge, "initial_start", lambda *_args: None)
    monkeypatch.setattr(game_mgr_module, "build_challenge", lambda _name: challenge)

    game = _game()
    monkeypatch.setattr(
        game, "target_for",
        lambda team_id, _challenge: VulboxTarget(host=team_id, ports={}),
    )
    game.deploy_challenges()

    assert set(game._state_regions) == {
        ("team1", "notes", "private-note"),
        ("team2", "notes", "private-note"),
    }
