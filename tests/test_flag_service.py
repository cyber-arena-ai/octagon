"""v3 flag-service: plants into prod over SSH (ExecPort), holds the FlagStore,
validates submits. GameMgr /flags/configure's it then signals /flags/rotate; the
service does the planting. Host (status + event-ingest) stubbed with
httpx.MockTransport; the exec port + challenge are fakes (no real SSH/prod)."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from challenges.interface import (
    FlagHandler,
    FlagObservation,
    ObservationStatus,
    SingleFlagHandler,
    StateReadResult,
    StateReadStatus,
    state_scan_observe,
)
from facility.ports import ExecResult
from flag.service_app import build_flag_service_app

HDR_FAC = {"X-Facility-Token": "fac"}
_FLAG_RE = re.compile(r"FLAG\{[A-Z0-9]+\}")


class _FakeExec:
    """Records exec calls; the flag-handler's plant command carries the flag,
    so this stands in for 'ran the plant over SSH into prod'."""
    def __init__(self):
        self.calls = []

    def exec(self, team_id, role, cmd, *, timeout_s=120.0):
        self.calls.append((team_id, role, cmd))
        return ExecResult(exit_code=0, stdout="", stderr="")


class _FakeFlagHandler(SingleFlagHandler):
    name = "fake"

    def __init__(self, planted):
        self._planted = planted

    def plant_one(self, target, flag):
        # exercise the exec path (prod write happens here in reality)
        target.meta["exec_in_container"](target.host, f"install-flag {flag}")
        self._planted.append((target.meta["team_id"], flag))
        return f"h:{flag[:6]}"

    def retrieve(self, target, handle, expected=None):
        return FlagObservation(ObservationStatus.NOT_FOUND)


class _FlagIdFakeHandler(_FakeFlagHandler):
    """Fake handler that also exposes a public flag-id, packed into the handle
    alongside the flag (which flag_id() must NOT return)."""

    def plant_one(self, target, flag):
        super().plant_one(target, flag)          # exercise the exec/plant path
        return f"user-{target.meta['team_id']}#{flag}"

    def flag_id(self, handle):
        return handle.split("#", 1)[0]


def _service(phase="ATTACK", handler_cls=_FakeFlagHandler, status=None):
    """Returns (TestClient, fake_exec, planted_list, events_list).

    `status` is the MUTABLE round clock GameMgr would serve; captures are only
    credited for the active round, so a test that rotates must advance it."""
    planted: list = []
    events: list = []
    clock = status if status is not None else {"phase": phase, "round": 1}

    def host(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/game/status":
            return httpx.Response(200, json=dict(clock))
        if req.url.path == "/facility/events":
            events.append(json.loads(req.content))
            return httpx.Response(200, json={"ok": True, "recorded": True})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(host))
    ex = _FakeExec()
    app = build_flag_service_app(
        gm_base_url="http://host", facility_token="fac", client=client,
        exec_port=ex, probe_loader=lambda package: handler_cls(planted),
    )
    return TestClient(app), ex, planted, events


def _configure(tc):
    # GameMgr passes package + named ports (resolved host-side); the service
    # loads only the flag_handler probe.
    return tc.post("/flags/configure", headers=HDR_FAC, json={
        "tokens": {"tok1": "team1", "tok2": "team2"},
        "hosts": {"team1": "team1_prod", "team2": "team2_prod"},
        "deployment": [
            {"team_id": "team1", "service": "notes", "package": "notes",
             "ports": {"service": 5000},
             "flag_store_specs": [{"name": "default", "kind": "read",
                                   "scope": None}]},
            {"team_id": "team2", "service": "notes", "package": "notes",
             "ports": {"service": 5000},
             "flag_store_specs": [{"name": "default", "kind": "read",
                                   "scope": None}]},
        ],
    })


def _current_flag(planted, team):
    return [f for t, f in planted if t == team][-1]


# ── regression: a slow/hung plant must NOT block concurrent submits ───────────


def test_submit_not_blocked_by_in_flight_plant():
    """The flag-service-wedge bug: rotate planted over SSH ON the event loop, so
    a hung plant froze every concurrent /game/flag/submit (and later rotations).
    rotate/submit now run off the loop — a submit issued while a plant is still
    blocked must return promptly instead of waiting for the plant."""
    import asyncio
    import threading

    started = threading.Event()   # set when a plant is in-flight
    release = threading.Event()   # the test releases the blocked plant

    class _BlockingExec:
        def exec(self, team_id, role, cmd, *, timeout_s=120.0):
            started.set()
            release.wait(5)       # simulate a slow/hung SSH plant
            return ExecResult(exit_code=0, stdout="", stderr="")

    def host(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/game/status":
            return httpx.Response(200, json={"phase": "ATTACK", "round": 1})
        if req.url.path == "/facility/events":
            return httpx.Response(200, json={"ok": True, "recorded": True})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(host))
    app = build_flag_service_app(
        gm_base_url="http://host", facility_token="fac", client=client,
        exec_port=_BlockingExec(), probe_loader=lambda package: _FakeFlagHandler([]),
    )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            cfg = await ac.post("/flags/configure", headers=HDR_FAC, json={
                "tokens": {"tok1": "team1"},
                "hosts": {"team1": "team1_prod", "team2": "team2_prod"},
                "deployment": [{"team_id": "team2", "service": "notes",
                                "package": "notes", "ports": {"service": 5000},
                                "flag_store_specs": [
                                    {"name": "default", "kind": "read",
                                     "scope": None},
                                ]}],
            })
            assert cfg.status_code == 200
            # rotate starts and blocks inside the plant (off the loop)
            rotate = asyncio.create_task(
                ac.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1}))
            await asyncio.to_thread(started.wait, 2)
            assert started.is_set(), "plant never started"
            # submit while the plant is still blocked -> must return promptly
            sub = await asyncio.wait_for(
                ac.post("/game/flag/submit", headers={"X-Team-Token": "tok1"},
                        json={"flag": "FLAG{NOTPLANTEDYET}"}), timeout=2.0)
            assert sub.status_code == 200          # not blocked by the plant
            assert sub.json()["reason"] == "UNKNOWN"
            assert not rotate.done()               # plant is still blocked
            release.set()
            await rotate

    asyncio.run(scenario())


# ── configure + rotate (plant over SSH) ──────────────────────────────────────


def test_configure_then_rotate_plants_over_exec():
    tc, ex, planted, events = _service()
    assert _configure(tc).json()["deployment"] == 2

    r = tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    assert r.json() == {"ok": True, "round_id": 1}

    # planted both teams via the exec port, and the plant command carried the flag
    assert {t for t, _ in planted} == {"team1", "team2"}
    assert all(role == "prod" for _, role, _ in ex.calls)
    assert any(_FLAG_RE.search(cmd) for _, _, cmd in ex.calls)
    # one FLAG_PLANTED per (team, service) to the host timeline; no flag leaks
    fp = [e for e in events if e["type"] == "FLAG_PLANTED"]
    assert len(fp) == 2
    assert not any(_FLAG_RE.search(json.dumps(e)) for e in fp)


def test_plant_retries_failed_box_then_succeeds(monkeypatch):
    """#1: a box briefly down at the plant instant (agent restart_service racing
    the round tick) is RETRIED for the failed subset — the flag still rotates,
    FLAG_PLANTED still fires for it, and no FLAG_PLANT_FAILED is emitted."""
    monkeypatch.setattr("flag.server._PLANT_RETRY_BACKOFF_S", (0, 0, 0, 0))

    class FlakyHandler(_FakeFlagHandler):
        def __init__(self, planted):
            super().__init__(planted)
            self._attempts: dict = {}

        def plant_one(self, target, flag):
            tid = target.meta["team_id"]
            self._attempts[tid] = self._attempts.get(tid, 0) + 1
            if tid == "team1" and self._attempts[tid] == 1:   # down on first try
                raise RuntimeError("ssh: connect to team1_prod port 22: refused")
            return super().plant_one(target, flag)

    tc, _, planted, events = _service(handler_cls=FlakyHandler)
    _configure(tc)
    assert tc.post("/flags/rotate", headers=HDR_FAC,
                   json={"round_id": 1}).json() == {"ok": True, "round_id": 1}

    assert {t for t, _ in planted} == {"team1", "team2"}      # team1 planted on retry
    fp = {e["team_id"]: e["payload"] for e in events if e["type"] == "FLAG_PLANTED"}
    assert set(fp) == {"team1", "team2"}
    # observability: attempt count + latency ride along on FLAG_PLANTED
    assert fp["team2"]["attempt"] == 1                        # first try
    assert fp["team1"]["attempt"] == 2                        # succeeded on the retry
    assert all(isinstance(p["latency_ms"], int) and p["latency_ms"] >= 0
               for p in fp.values())
    assert not [e for e in events if e["type"] == "FLAG_PLANT_FAILED"]


def test_plant_permanent_failure_emits_event_not_silent(monkeypatch):
    """#3: a box down for the whole plant window (retries exhausted) emits
    FLAG_PLANT_FAILED instead of silently dropping — and the OTHER team still
    plants, but the rotate FAILS so GameMgr cannot publish a partial round."""
    monkeypatch.setattr("flag.server._PLANT_RETRY_BACKOFF_S", (0, 0))

    async def inline(fn, *args):
        return fn(*args)

    # The assertion is about endpoint failure propagation, not thread scheduling.
    monkeypatch.setattr("flag.service_app.asyncio.to_thread", inline)

    class DeadBoxHandler(_FakeFlagHandler):
        def plant_one(self, target, flag):
            if target.meta["team_id"] == "team1":
                raise RuntimeError("ssh: connect to team1_prod port 22: refused")
            return super().plant_one(target, flag)

    tc, _, planted, events = _service(handler_cls=DeadBoxHandler)
    _configure(tc)
    with pytest.raises(RuntimeError, match="did not rotate every target"):
        tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})

    assert {t for t, _ in planted} == {"team2"}               # team2 fine, team1 dropped
    assert [e["team_id"] for e in events if e["type"] == "FLAG_PLANTED"] == ["team2"]
    fpf = [e for e in events if e["type"] == "FLAG_PLANT_FAILED"]
    assert len(fpf) == 1 and fpf[0]["team_id"] == "team1" and fpf[0]["round_id"] == 1
    assert fpf[0]["payload"]["attempts"] == 3                 # backoff (0,0) → 3 tries
    assert "refused" in fpf[0]["payload"]["error"]


def test_configure_and_rotate_require_facility_token():
    tc, _, _, _ = _service()
    assert tc.post("/flags/configure", json={}).status_code == 403
    assert tc.post("/flags/rotate", json={"round_id": 1}).status_code == 403


def test_invalid_configure_does_not_replace_the_published_server():
    tc, _ex, planted, _events = _service()
    assert _configure(tc).status_code == 200
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    victim_flag = _current_flag(planted, "team1")

    with pytest.raises(KeyError, match="kind"):
        tc.post("/flags/configure", headers=HDR_FAC, json={
            "tokens": {"new-token": "team9"},
            "hosts": {"team9": "team9_prod"},
            "deployment": [
                {"team_id": "team9", "service": "notes", "package": "notes",
                 "flag_store_specs": [{"name": "default"}]},
            ],
        })

    result = tc.post(
        "/game/flag/submit", headers={"X-Team-Token": "tok2"},
        json={"flag": victim_flag},
    ).json()
    assert result["accepted"] is True


def test_rotate_before_configure_409():
    tc, _, _, _ = _service()
    assert tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1}).status_code == 409


def test_audit_requires_facility_token_and_configure():
    tc, _, _, _ = _service()
    assert tc.post("/flags/audit", json={"round_id": 1}).status_code == 403  # no token
    assert tc.post("/flags/audit", headers=HDR_FAC,
                   json={"round_id": 1}).status_code == 409                  # not configured


def test_configure_rotate_then_audit_records_persistence():
    tc, _ex, _planted, events = _service()
    _configure(tc)
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    events.clear()  # drop FLAG_PLANTED so we assert only the audit's output
    r = tc.post("/flags/audit", headers=HDR_FAC, json={"round_id": 1})
    assert r.status_code == 200 and r.json()["round_id"] == 1
    persistence = [e for e in events if e["type"] == "FLAG_PERSISTENCE"]
    # one per planted (team, service); the fake handler reads back NOT_FOUND
    assert {(e["team_id"], e["payload"]["service"]) for e in persistence} == \
        {("team1", "notes"), ("team2", "notes")}
    assert all(e["payload"]["status"] == "NOT_FOUND" for e in persistence)


# ── submit validates against what the service planted ────────────────────────


def test_submit_accepted_own_duplicate():
    tc, _, planted, events = _service()
    _configure(tc)
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    victim_flag = _current_flag(planted, "team1")

    acc = tc.post("/game/flag/submit", json={"flag": victim_flag},
                  headers={"X-Team-Token": "tok2"}).json()
    assert acc["accepted"] is True and acc["reason"] == "ACCEPTED"

    own = tc.post("/game/flag/submit", json={"flag": victim_flag},
                  headers={"X-Team-Token": "tok1"}).json()
    assert own["reason"] == "OWN"

    dup = tc.post("/game/flag/submit", json={"flag": victim_flag},
                  headers={"X-Team-Token": "tok2"}).json()
    assert dup["reason"] == "DUPLICATE"

    subs = [e for e in events if e["type"] == "FLAG_SUBMITTED"]
    assert subs and subs[0]["payload"]["victim_team_id"] == "team1"


def test_submit_rejects_when_status_times_out():
    """A slow/hung GameMgr `/game/status` must still not 500 (#38) — but an
    UNKNOWN status cannot bind evidence to an active round, so the service
    records a rejection rather than crediting a capture through what might be a
    rotation window. The agent simply retries once status is back."""
    planted: list = []
    events: list = []

    def host(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/game/status":
            raise httpx.ReadTimeout("timed out", request=req)   # GameMgr stalled
        if req.url.path == "/facility/events":
            events.append(json.loads(req.content))
            return httpx.Response(200, json={"ok": True, "recorded": True})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(host))
    app = build_flag_service_app(
        gm_base_url="http://host", facility_token="fac", client=client,
        exec_port=_FakeExec(), probe_loader=lambda package: _FakeFlagHandler(planted),
    )
    tc = TestClient(app)
    _configure(tc)                                    # rotate/plant don't need status
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    victim = _current_flag(planted, "team1")

    r = tc.post("/game/flag/submit", json={"flag": victim},
                headers={"X-Team-Token": "tok2"})
    assert r.status_code == 200                        # NOT 500 (the bug)
    assert r.json()["accepted"] is False and r.json()["reason"] == "WRONG_PHASE"
    subs = [e for e in events if e["type"] == "FLAG_SUBMITTED"]
    assert subs and subs[-1]["payload"]["accepted"] is False    # still recorded
    assert subs[-1]["payload"]["phase"] == ""          # status unknown → blank


def test_submit_bad_token_and_unconfigured():
    tc, _, planted, _ = _service()
    # before configure: ERROR
    assert tc.post("/game/flag/submit", json={"flag": "x"}).json()["reason"] == "ERROR"
    _configure(tc)
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    # no token: BAD_TOKEN
    assert tc.post("/game/flag/submit", json={"flag": "x"}).json()["reason"] == "BAD_TOKEN"


def test_submit_wrong_phase():
    tc, _, planted, _ = _service(phase="DEFENSE")
    _configure(tc)
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    f = _current_flag(planted, "team1")
    r = tc.post("/game/flag/submit", json={"flag": f}, headers={"X-Team-Token": "tok2"})
    assert r.json()["reason"] == "WRONG_PHASE"


# ── attack-info: opponents' public flag-ids ──────────────────────────────────


def test_attack_info_returns_opponent_flag_ids():
    tc, _, planted, _ = _service(handler_cls=_FlagIdFakeHandler)
    _configure(tc)
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})

    # team2 (tok2) asks -> sees only team1's public id + host, never a flag value
    r = tc.post("/game/attack-info", headers={"X-Team-Token": "tok2"})
    assert r.status_code == 200
    tgts = r.json()["targets"]
    assert {t["team_id"] for t in tgts} == {"team1"}
    assert tgts[0]["flag_id"] == "user-team1" and tgts[0]["host"] == "team1_prod"
    assert not any(_FLAG_RE.search(json.dumps(t)) for t in tgts)


def test_attack_info_bad_token_and_unconfigured():
    tc, _, _, _ = _service(handler_cls=_FlagIdFakeHandler)
    # before configure -> empty (nothing planted yet, no principals issued)
    assert tc.post("/game/attack-info").json() == {"targets": [], "principals": []}
    _configure(tc)
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    # missing token -> BAD_TOKEN (the gateway maps this to Unauthorized)
    assert tc.post("/game/attack-info").json()["error"] == "BAD_TOKEN"


def test_rotation_invalidates_old_flag():
    clock = {"phase": "ATTACK", "round": 1}
    tc, _, planted, _ = _service(status=clock)
    _configure(tc)
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    old = _current_flag(planted, "team1")
    clock["round"] = 2
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 2})
    new = _current_flag(planted, "team1")
    assert old != new
    stale = tc.post("/game/flag/submit", json={"flag": old}, headers={"X-Team-Token": "tok2"})
    fresh = tc.post("/game/flag/submit", json={"flag": new}, headers={"X-Team-Token": "tok2"})
    assert stale.json()["reason"] == "UNKNOWN"
    assert fresh.json()["accepted"] is True


# ── WRITE stores: claim over the live rail ───────────────────────────────────


class _FakeStatePort:
    """Stands in for GameMgr's `/facility/state/read` — the trusted, prod-free
    read a WRITE handler may use so a root defender cannot rewrite the observer."""

    def __init__(self):
        self.content = b""
        self.calls = []

    def read(self, team_id, service, region):
        self.calls.append((team_id, service, region))
        return StateReadResult(StateReadStatus.OK, files={"config.xml": self.content})


class _WriteHandler(FlagHandler):
    """WRITE store served entirely by plant + retrieve.

    `plant` puts the facility integrity token in its own anchor and returns a
    handle carrying it; `retrieve` reads the anchor when asked for that token
    (the audit) and scans the writable region otherwise (a claim)."""

    name = "write-store"

    def __init__(self):
        self.anchor: dict[str, str] = {}

    def plant(self, target, flags):
        handles = {}
        for store, f in flags.items():
            self.anchor[store] = f.value
            handles[store] = f"job-1#{f.value}"
        return handles

    def retrieve(self, target, handle, expected=None):
        _target_id, token = handle.split("#", 1)
        if expected == token:
            return FlagObservation(ObservationStatus.PRESENT, value=token)
        return state_scan_observe(target, "job-config", expected)

    def flag_id(self, handle):
        return handle.split("#", 1)[0]


def _write_service(state_port):
    events = []
    handlers = []

    def host(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/game/status":
            return httpx.Response(200, json={"phase": "ATTACK", "round": 1})
        if req.url.path == "/facility/events":
            events.append(json.loads(req.content))
            return httpx.Response(200, json={"ok": True, "recorded": True})
        return httpx.Response(404)

    app = build_flag_service_app(
        gm_base_url="http://host", facility_token="fac",
        client=httpx.Client(transport=httpx.MockTransport(host)),
        exec_port=_FakeExec(), state_port=state_port,
        probe_loader=lambda _package: _new_handler(handlers),
    )
    tc = TestClient(app)
    tc.post("/flags/configure", headers=HDR_FAC, json={
        "tokens": {"tok1": "team1", "tok2": "team2"},
        "hosts": {"team1": "team1_prod", "team2": "team2_prod"},
        "deployment": [
            {"team_id": team, "service": "notes", "package": "notes",
             "ports": {"service": 5000},
             # Specs alone: the configure payload carries ONE store view.
             "flag_store_specs": [{"name": "job-config", "kind": "write",
                                   "scope": "app.cross_boundary.write"}]}
            for team in ("team1", "team2")
        ],
    })
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    return tc, events, handlers


def _new_handler(sink):
    h = _WriteHandler()
    sink.append(h)
    return h


def _claim(tc, token="tok2", victim="team1"):
    return tc.post("/game/flag/claim", headers={"X-Team-Token": token},
                   json={"victim_team_id": victim, "service": "notes",
                         "store": "job-config"}).json()


def test_claim_scores_a_write_store_through_the_scoped_state_port():
    state = _FakeStatePort()
    tc, events, _handlers = _write_service(state)

    info = tc.post("/game/attack-info", headers={"X-Team-Token": "tok2"}).json()
    row = next(r for r in info["targets"] if r["team_id"] == "team1")
    assert row["oracle"] == "claim" and row["outcome"] == "write"
    assert row["flag_id"] == "job-1" and row["mark"].startswith("MARK{")

    assert _claim(tc)["reason"] == "NOT_OBSERVED"      # nothing placed yet

    state.content = f"prefix {row['mark']} suffix".encode()
    assert _claim(tc)["accepted"] is True
    assert _claim(tc)["reason"] == "DUPLICATE"

    # The read is addressed by identities + an opaque region name only.
    assert state.calls[-1] == ("team1", "notes", "job-config")
    payload = [e for e in events if e["type"] == "FLAG_SUBMITTED"][-1]["payload"]
    assert payload["outcome"] == "write" and payload["scope"] == "app.cross_boundary.write"
    assert "MARK{" not in json.dumps(payload)     # marks never reach the timeline


def test_a_write_stores_integrity_token_is_never_submittable():
    """The token IS planted on the victim and a root attacker could read it —
    it just isn't worth anything, because a WRITE store's value never enters the
    submittable index."""
    state = _FakeStatePort()
    tc, _events, handlers = _write_service(state)
    tokens = {h.anchor["job-config"] for h in handlers}
    assert tokens and all(t.startswith("FLAG{") for t in tokens)
    for token in tokens:
        r = tc.post("/game/flag/submit", headers={"X-Team-Token": "tok2"},
                    json={"flag": token})
        assert r.json() == {"accepted": False, "reason": "UNKNOWN",
                            "detail": r.json()["detail"]}


def test_claim_requires_a_team_token():
    state = _FakeStatePort()
    tc, _events, _handlers = _write_service(state)
    assert tc.post("/game/flag/claim", json={}).json()["reason"] == "BAD_TOKEN"
