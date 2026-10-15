"""v3: GameMgr ↔ flag-service wiring — config parse, configure + rotate-signal,
and the submit FORWARD. The flag-service is stubbed with httpx.MockTransport.
GameMgr is always containerized (no in-process flag fallback)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from gamemgr.config import (
    ChallengeConfig,
    FlagServiceConfig,
    GameConfig,
    RoundConfig,
    _from_dict,
)
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


class _StubOrchestrator:
    network = None


def _game(*, base_url="http://fs"):
    teams = [Team(id="team1", dev="d1", prod="p1", token="tok1"),
             Team(id="team2", dev="d2", prod="p2", token="tok2")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000,
                                    deploy_on=["team1", "team2"])],
        rounds=RoundConfig(), agents={},
        flag_service=FlagServiceConfig(base_url=base_url),
    )
    g = GameMgr(config=cfg, orchestrator=_StubOrchestrator())
    g._facility_token = "fac"
    return g


# ── config parse ───────────────────────────────────────────────────────────


def test_config_parse_flag_service():
    cfg = _from_dict({
        "teams": [{"id": "team1", "dev": "d", "prod": "p"}],
        "flag_service": {"base_url": "http://127.0.0.1:9001", "container": "flag_service"},
    })
    assert cfg.flag_service.base_url == "http://127.0.0.1:9001"
    assert cfg.flag_service.container == "flag_service"


def test_config_flag_service_defaults_empty():
    cfg = _from_dict({"teams": [{"id": "team1", "dev": "d", "prod": "p"}]})
    assert cfg.flag_service.base_url == ""


# ── configure + rotate-signal (the service plants over SSH) ──────────────────


def test_plant_initial_configures_then_signals_rotate0():
    calls: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "fs"
        assert req.headers.get("x-facility-token") == "fac"
        calls.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True})

    g = _game()
    g._flag_http = httpx.Client(transport=httpx.MockTransport(handler))
    g.plant_initial_flags()  # v3: configure + signal rotate(0); GameMgr holds no flags

    paths = [p for p, _ in calls]
    assert paths == ["/flags/configure", "/flags/rotate"]
    cfg = calls[0][1]
    assert cfg["tokens"] == {"tok1": "team1", "tok2": "team2"}
    assert cfg["hosts"] == {"team1": "p1", "team2": "p2"}
    assert {(d["team_id"], d["service"]) for d in cfg["deployment"]} == \
        {("team1", "notes"), ("team2", "notes")}
    assert all("flag_store_specs" in d and "flag_stores" not in d
               for d in cfg["deployment"])
    assert calls[1][1] == {"round_id": 0}


def test_rotate_flags_signals_service():
    calls: list[str] = []

    def handler(req): calls.append(req.url.path); return httpx.Response(200, json={"ok": True})
    g = _game()
    g._flag_http = httpx.Client(transport=httpx.MockTransport(handler))
    g.rotate_flags(3)
    assert calls == ["/flags/rotate"]


def test_audit_flags_signals_service():
    calls: list[tuple[str, dict]] = []

    def handler(req):
        calls.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True})
    g = _game()
    g._flag_http = httpx.Client(transport=httpx.MockTransport(handler))
    g.audit_flags(5)
    assert calls == [("/flags/audit", {"round_id": 5})]


def test_on_round_stop_triggers_flag_audit():
    # The round-loop wiring: on_round_stop must run the end-of-round audit for the
    # round that just ended (records ROUND_STOP in-process, then signals
    # /flags/audit). Its only HTTP call is that audit signal.
    calls: list[tuple[str, dict]] = []

    def handler(req):
        calls.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True})

    g = _game()
    g.artifacts = None  # skip _persist_service_logs (docker) — not under test here
    g._flag_http = httpx.Client(transport=httpx.MockTransport(handler))
    asyncio.run(g.on_round_stop(round_id=7, phase="ATTACK"))
    assert calls == [("/flags/audit", {"round_id": 7})]


def test_rotation_failure_is_observable():
    """Configure/rotate now FAIL LOUD: a round the flag-service could not fully
    set up must never be published as attackable. The audit stays best-effort —
    it only records, it never gates a round."""
    def handler(req): return httpx.Response(503)
    g = _game()
    g._flag_http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="configuration failed"):
        g.plant_initial_flags()
    with pytest.raises(RuntimeError, match="rotation failed"):
        g.rotate_flags(2)
    g.audit_flags(2)


def _orch_with_fake_container(log_bytes: bytes):
    """A stub orchestrator whose docker client returns a container that logs
    `log_bytes` — enough to exercise _persist_flag_service_log without Docker."""
    container = SimpleNamespace(logs=lambda timestamps=False: log_bytes)
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: container))
    return SimpleNamespace(network=None, containers=SimpleNamespace(_client=client))


def test_persist_flag_service_log_dumps_docker_logs(tmp_path):
    """Finding 4: on a flag-service failure the container's docker logs (where a
    500's traceback lives, and which vanish on teardown) are snapshotted into the
    run artifacts."""
    g = _game()
    g.orchestrator = _orch_with_fake_container(b"Traceback: plant_one exploded on mlflow-lfi")
    g._flag_service = FlagServiceConfig(base_url="http://fs", container="flag_service")
    g.run_dir = tmp_path

    g._persist_flag_service_log()

    f = tmp_path / "artifacts" / "flag-service.log"
    assert f.exists()
    assert "plant_one exploded" in f.read_text()


def test_persist_flag_service_log_best_effort_noop_without_container(tmp_path):
    """No container name configured → nothing to snapshot, no crash."""
    g = _game()
    g.orchestrator = _orch_with_fake_container(b"unused")
    g._flag_service = FlagServiceConfig(base_url="http://fs", container="")
    g.run_dir = tmp_path
    g._persist_flag_service_log()
    assert not (tmp_path / "artifacts" / "flag-service.log").exists()


def test_configure_failure_dumps_flag_service_log(tmp_path):
    """The fail-loud configure path snapshots the flag-service container log
    before raising, so the 500's traceback survives in the run artifacts."""
    g = _game()
    g._flag_http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    g.orchestrator = _orch_with_fake_container(b"flag-service crashed: KeyError 'flag'")
    g._flag_service = FlagServiceConfig(base_url="http://fs", container="flag_service")
    g.run_dir = tmp_path

    with pytest.raises(RuntimeError, match="configuration failed"):
        g.plant_initial_flags()

    assert (tmp_path / "artifacts" / "flag-service.log").read_text() == \
        "flag-service crashed: KeyError 'flag'"


def test_configure_failure_reason_carries_status_and_body():
    """Finding 4: a flag-service failure must leave a recoverable diagnostic. The
    status + a bounded response-body excerpt are captured and folded into the
    fail-loud reason (which lands on MATCH_FAILED), not just a bare orche-log
    line — so the 500 that used to be un-diagnosable is now in the timeline."""
    def handler(req):
        return httpx.Response(500, text="boom: plant_one exploded on mlflow-lfi")
    g = _game()
    g._flag_http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="configuration failed") as ei:
        g.plant_initial_flags()
    msg = str(ei.value)
    assert "HTTP 500" in msg                       # the status is captured
    assert "plant_one exploded" in msg             # and a bounded body excerpt
    assert g._last_facility_error and "/flags/configure" in g._last_facility_error


def test_configure_failure_reason_scrubs_secrets_in_body():
    """The flag-service error body is SCRUBBED before it lands in the (broadly
    visible, downloadable) MATCH_FAILED reason — a credential shape echoed in the
    body is redacted while the status survives."""
    def handler(req):
        return httpx.Response(500, text="crash: leaked key sk-ant-ABCDEFGHIJKLMNOP1234567890")
    g = _game()
    g._flag_http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError) as ei:
        g.plant_initial_flags()
    msg = str(ei.value)
    assert "sk-ant-ABCDEFGHIJKLMNOP1234567890" not in msg   # redacted
    assert "HTTP 500" in msg                                  # status still there


# ── submit forward ───────────────────────────────────────────────────────────


def test_submit_forwards_to_service_as_team():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/game/flag/submit"
        seen["host"] = req.url.host
        seen["token"] = req.headers.get("x-team-token")
        seen["flag"] = json.loads(req.content)["flag"]
        return httpx.Response(200, json={"accepted": True, "reason": "ACCEPTED",
                                         "detail": "submission accepted"})

    g = _game()
    g._flag_http = httpx.Client(transport=httpx.MockTransport(handler))
    res = asyncio.run(g.submit_flag("team1", "FLAG{stolen}"))

    # forwarded to the single service, as team1 (its token)
    assert seen == {"host": "fs", "token": "tok1", "flag": "FLAG{stolen}"}
    assert res.accepted is True and res.reason == "ACCEPTED"
    assert res.detail == "submission accepted"


def test_submit_forward_failure_returns_error():
    def handler(req): return httpx.Response(500)
    g = _game()
    g._flag_http = httpx.Client(transport=httpx.MockTransport(handler))
    # a forward failure must NOT validate locally (no in-process store; would risk
    # double-scoring) — it returns ERROR (retry hint).
    res = asyncio.run(g.submit_flag("team1", "FLAG{live}"))
    assert res.accepted is False and res.reason == "ERROR"
    assert "retry" in res.detail


def test_submit_no_base_url_returns_error():
    g = _game(base_url="")  # misconfigured
    g._flag_http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    res = asyncio.run(g.submit_flag("team1", "FLAG{x}"))
    assert res.accepted is False and res.reason == "ERROR"
