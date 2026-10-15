"""v3 health-poller: probes prod over SSH (ExecPort), records SERVICE_HEALTH_CHECK
via the EventSink. GameMgr /poller/configure's it (hosts + cadence), then drives
attack-enter/exit/trigger-now. Host stubbed with httpx.MockTransport; exec port +
challenge are fakes (no real SSH/prod)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from challenges.interface import CheckResult
from facility.ports import ExecResult
from gamemgr.config import (
    ChallengeConfig,
    GameConfig,
    HealthPollerConfig,
    RoundConfig,
    _from_dict,
)
from gamemgr.game_mgr import GameMgr
from health.poller import HealthPoller
from health.poller_handle import HealthPollerConfigureError, RemoteHealthPollerHandle
from health.service_app import build_health_poller_app
from models.topology import Team, TopologyGraph

HDR = {"X-Facility-Token": "fac"}


class _FakeExec:
    def __init__(self):
        self.calls = []

    def exec(self, team_id, role, cmd, *, timeout_s=120.0):
        self.calls.append((team_id, role, cmd))
        return ExecResult(exit_code=0, stdout="ok", stderr="")


class _FakeFuncTest:
    def run(self, target):
        # touch the exec path so the SSH probe is exercised
        target.meta["exec_in_container"](target.host, "pgrep -f svc")
        return CheckResult(name="root", passed=True, children=[
            CheckResult(name="process", passed=True),
            CheckResult(name="tcp", passed=True),
        ])


def _service():
    events: list = []

    def host(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/game/status":
            return httpx.Response(200, json={"phase": "ATTACK", "round": 2})
        if req.url.path == "/facility/events":
            events.append(json.loads(req.content))
            return httpx.Response(200, json={"ok": True, "recorded": True})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(host))
    ex = _FakeExec()
    app = build_health_poller_app(
        gm_base_url="http://host", facility_token="fac", client=client,
        exec_port=ex, probe_loader=lambda package: _FakeFuncTest(),
    )
    return TestClient(app), ex, events


def _configure(tc):
    # GameMgr passes package + named ports; the poller loads only functionality_test.
    return tc.post("/poller/configure", headers=HDR, json={
        "hosts": {"team1": "team1_prod"},
        "targets": [{"team_id": "team1", "service": "notes", "interval_secs": 5,
                     "package": "notes", "ports": {"service": 5000}}],
    })


# ── configure + trigger probes via SSH ───────────────────────────────────────


def test_configure_builds_targets():
    tc, _, _ = _service()
    assert _configure(tc).json()["targets"] == 1
    assert tc.get("/healthz").json()["targets"] == 1


def test_trigger_now_probes_over_exec_and_records():
    tc, ex, events = _service()
    _configure(tc)
    r = tc.post("/poller/trigger-now", headers=HDR,
                json={"team_id": "team1", "service": "notes"})
    assert r.status_code == 200
    # probed prod over the exec port (role prod)
    assert ex.calls and ex.calls[-1][1] == "prod"
    # recorded SERVICE_HEALTH_CHECK per leaf, stamped with host round/phase
    shc = [e for e in events if e["type"] == "SERVICE_HEALTH_CHECK"]
    assert {e["payload"]["level"] for e in shc} == {"process", "tcp"}
    assert all(e["round_id"] == 2 and e["payload"]["trigger"] == "restart" for e in shc)


def test_attack_enter_before_configure_409():
    tc, _, _ = _service()
    assert tc.post("/poller/attack-enter", headers=HDR, json={"round_id": 1}).status_code == 409


def test_control_endpoints_require_token():
    tc, _, _ = _service()
    for path, body in [("/poller/configure", {}), ("/poller/attack-enter", {"round_id": 1}),
                       ("/poller/attack-exit", {}),
                       ("/poller/trigger-now", {"team_id": "team1", "service": "notes"})]:
        assert tc.post(path, json=body).status_code == 403


# ── host-side handle ─────────────────────────────────────────────────────────


def test_configure_retries_then_raises_on_persistent_error(monkeypatch):
    """Finding 2: a persistent configure failure (container down / load-induced
    500) is fail-loud — retried, then raised as HealthPollerConfigureError, never
    swallowed. A swallowed configure is what let a run report `succeeded` with
    zero probes."""
    monkeypatch.setattr("health.poller_handle.time.sleep", lambda _s: None)
    posts: list[str] = []

    def host(req):
        posts.append(req.url.path)
        return httpx.Response(500, json={"error": "overloaded"})

    client = httpx.Client(transport=httpx.MockTransport(host))
    h = RemoteHealthPollerHandle("http://hp", "fac", [], {}, client=client)

    with pytest.raises(HealthPollerConfigureError):
        h.configure()
    assert len(posts) == RemoteHealthPollerHandle._CONFIGURE_RETRIES   # retried, not one-shot


def test_configure_succeeds_after_transient_error(monkeypatch):
    """A transient 500 (the Finding-1 cold-start blip) is absorbed by the retry:
    configure recovers and does NOT raise once a later attempt returns 200."""
    monkeypatch.setattr("health.poller_handle.time.sleep", lambda _s: None)
    attempts = {"n": 0}

    def host(req):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(500, json={"error": "overloaded"})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(host))
    h = RemoteHealthPollerHandle("http://hp", "fac", [], {}, client=client)
    h.configure()                       # no raise
    assert attempts["n"] == 2           # one retry after the transient 500


def test_build_health_facility_fails_match_on_configure_error(monkeypatch):
    """Finding 2 at the GameMgr seam: a configure that can't be pushed fails the
    MATCH (fail-loud) and leaves no poller handle, instead of standing up an
    unconfigured poller and marching on to a fake `succeeded`."""
    def _boom(self):
        raise HealthPollerConfigureError("/poller/configure failed after 3 attempts")
    monkeypatch.setattr(RemoteHealthPollerHandle, "configure", _boom)
    g = _game()
    g._build_health_facility()
    assert g.match_failed is True
    assert "health-poller configure failed" in g.fatal_reason
    assert g.health_poller is None      # not stood up on a doomed match


def test_handle_configure_and_control():
    posted: list[tuple[str, dict]] = []

    def host(req):
        posted.append((req.url.path, json.loads(req.content) if req.content else {}))
        assert req.headers.get("x-facility-token") == "fac"
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(host))
    specs = [{"team_id": "team1", "service": "notes", "interval_secs": 5}]
    hosts = {"team1": "team1_prod"}
    h = RemoteHealthPollerHandle("http://hp", "fac", specs, hosts, client=client)

    h.configure()
    assert h.on_attack_enter(3) == []
    h.on_attack_exit()
    asyncio.run(h.trigger_now("team1", "notes"))

    assert [p for p, _ in posted] == [
        "/poller/configure", "/poller/attack-enter", "/poller/attack-exit",
        "/poller/trigger-now"]
    assert posted[0][1] == {"hosts": hosts, "targets": specs}
    assert posted[1][1] == {"round_id": 3}


# ── GameMgr remote-vs-local branch (now also calls configure) ────────────────


class _StubOrchestrator:
    network = None


def _game():
    teams = [Team(id="team1", dev="d1", prod="p1", token="tok1")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000,
                                    deploy_on=["team1"])],
        rounds=RoundConfig(), agents={},
        health_poller=HealthPollerConfig(base_url="http://hp"),
    )
    g = GameMgr(config=cfg, orchestrator=_StubOrchestrator())
    g._facility_token = "fac"
    return g


def test_gamemgr_builds_remote_handle_and_configures(monkeypatch):
    configured = {"n": 0}
    monkeypatch.setattr(RemoteHealthPollerHandle, "configure",
                        lambda self: configured.__setitem__("n", configured["n"] + 1))
    g = _game()
    g._build_health_facility()
    assert isinstance(g.health_poller, RemoteHealthPollerHandle)
    assert configured["n"] == 1


def test_config_parse_health_poller():
    cfg = _from_dict({
        "teams": [{"id": "team1", "dev": "d", "prod": "p"}],
        "health_poller": {"base_url": "http://127.0.0.1:9201", "container": "health_poller"},
    })
    assert cfg.health_poller.base_url == "http://127.0.0.1:9201"
    assert cfg.health_poller.container == "health_poller"
