"""FlagServer.audit — the end-of-round flag-persistence pass.

Per (team, service, store) it reads the planted flag back (read-only) and records
one FLAG_PERSISTENCE event; on AUTH_REJECTED it repairs (the only write) and
re-reads. Record-only and MUST never raise — one bad box can't abort the pass or
500 the round-end signal.
"""

from __future__ import annotations

from challenges.interface import (
    Flag,
    FlagObservation,
    FlagStoreSpec,
    ObservationStatus,
    RepairResult,
    SingleFlagHandler,
    VulboxTarget,
)
from facility import FacilityContext, LocalEventSink, LocalStatusPort
from flag import FlagServer
from flag.store import FlagRecord
from gamemgr.events import EventType
from gamemgr.timeline import Timeline


def _ctx(timeline: Timeline, *, round: int = 7, phase: str = "ATTACK") -> FacilityContext:
    return FacilityContext(
        status=LocalStatusPort(lambda: {"round": round, "phase": phase}),
        events=LocalEventSink(timeline),
    )


def _target_factory(team_id: str, _challenge) -> VulboxTarget:
    return VulboxTarget(
        host=f"{team_id}_prod", ports={},
        meta={"team_id": team_id, "exec_in_container": lambda *a: (0, "")},
    )


class _Ch:
    """Minimal challenge shim: audit only touches `.flag_handler`."""

    def __init__(self, handler) -> None:
        self.flag_handler = handler

    def flag_store_specs(self):
        return [FlagStoreSpec("default")]


class _AuditHandler(SingleFlagHandler):
    """Scriptable read/repair. `reads` is a queue consumed by successive
    retrieve() calls — each item is a FlagObservation to return, or an Exception
    to raise. `repair_result` is returned by repair() (or raised, if an
    Exception)."""

    def __init__(self, reads, *, repair_result=RepairResult.REPAIRED) -> None:
        self._reads = list(reads)
        self._repair_result = repair_result
        self.retrieve_calls: list[tuple[str, str | None]] = []
        self.repair_calls: list[str] = []

    @property
    def name(self) -> str:
        return "audit-stub"

    def plant_one(self, target, value):  # pragma: no cover — store populated directly
        return "h"

    def retrieve(self, target, handle, expected=None):
        self.retrieve_calls.append((handle, expected))
        r = self._reads.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def repair(self, target, handle):
        self.repair_calls.append(handle)
        if isinstance(self._repair_result, Exception):
            raise self._repair_result
        return self._repair_result


def _server(handler, *, teams=("team1",), service="svc", store="default"):
    timeline = Timeline()
    ch = _Ch(handler)
    cbo = {(t, service): ch for t in teams}
    server = FlagServer(_ctx(timeline), challenge_by_owner=cbo,
                        target_factory=_target_factory)
    for t in teams:
        server.store.planted[(t, service, store)] = FlagRecord(
            flag=Flag(value=f"FLAG{{{t}}}"), handle=f"h-{t}", round_id=1)
    return server, timeline


def _events(tl: Timeline):
    return [e for e in tl.history() if e.type == EventType.FLAG_PERSISTENCE.value]


# ── status matrix (read-only) ─────────────────────────────────────────────────

def test_audit_present_is_read_only_and_passes_expected():
    h = _AuditHandler([FlagObservation(ObservationStatus.PRESENT, value="FLAG{team1}")])
    server, tl = _server(h)
    server.audit(round_id=3)
    (e,) = _events(tl)
    assert e.round_id == 3 and e.team_id == "team1"
    assert e.payload == {"service": "svc", "store": "default",
                         "initial_status": "PRESENT", "status": "PRESENT",
                         "detail": None, "repair": None}
    assert h.repair_calls == []                       # read-only on PRESENT
    assert h.retrieve_calls == [("h-team1", "FLAG{team1}")]  # expected forwarded


def test_audit_not_found_and_mismatch_and_error():
    for st in (ObservationStatus.NOT_FOUND, ObservationStatus.MISMATCH,
               ObservationStatus.ERROR):
        h = _AuditHandler([FlagObservation(st, detail="d")])
        server, tl = _server(h)
        server.audit(round_id=1)
        (e,) = _events(tl)
        assert e.payload["status"] == st.value
        assert e.payload["initial_status"] == st.value   # no repair → initial==final
        assert e.payload["repair"] is None
        assert h.repair_calls == []


def test_audit_retrieve_raising_is_error_never_propagates():
    h = _AuditHandler([RuntimeError("boom")])
    server, tl = _server(h)
    server.audit(round_id=1)                          # must not raise
    (e,) = _events(tl)
    assert e.payload["status"] == "ERROR"
    assert e.payload["detail"] == "retrieve raised"


# ── AUTH_REJECTED → repair → re-read ──────────────────────────────────────────

def test_audit_auth_rejected_repairs_then_rereads():
    h = _AuditHandler(
        [FlagObservation(ObservationStatus.AUTH_REJECTED, detail="cred rotated"),
         FlagObservation(ObservationStatus.PRESENT, value="FLAG{team1}")],
        repair_result=RepairResult.REPAIRED)
    server, tl = _server(h)
    server.audit(round_id=1)
    (e,) = _events(tl)
    assert e.payload["initial_status"] == "AUTH_REJECTED"  # raw evidence retained
    assert e.payload["status"] == "PRESENT"           # the post-repair re-read
    assert e.payload["repair"] == "REPAIRED"
    assert h.repair_calls == ["h-team1"]              # repaired exactly once
    assert len(h.retrieve_calls) == 2                 # initial + re-read


def test_audit_repair_raising_is_failed_and_still_rereads():
    h = _AuditHandler(
        [FlagObservation(ObservationStatus.AUTH_REJECTED),
         FlagObservation(ObservationStatus.NOT_FOUND)],
        repair_result=RuntimeError("docker exec died"))
    server, tl = _server(h)
    server.audit(round_id=1)                          # must not raise
    (e,) = _events(tl)
    assert e.payload["initial_status"] == "AUTH_REJECTED"
    assert e.payload["repair"] == "FAILED"
    assert e.payload["status"] == "NOT_FOUND"         # re-read still ran
    assert len(h.retrieve_calls) == 2


# ── iteration / robustness ────────────────────────────────────────────────────

def test_audit_records_one_event_per_planted_store():
    h = _AuditHandler([FlagObservation(ObservationStatus.PRESENT, value="x")] * 2)
    server, tl = _server(h, teams=("team1", "team2"))
    server.audit(round_id=1)
    ev = _events(tl)
    assert {e.team_id for e in ev} == {"team1", "team2"}


def test_audit_isolates_a_pre_read_failure_as_error():
    # A per-item failure OUTSIDE the guarded read/repair (here: building the
    # target raises) must NOT abort the pass or drop the other teams — it lands as
    # an ERROR event (initial_status=status=ERROR, no repair), and there's no
    # AUTH_REJECTED evidence to lose because it failed before the first read.
    h = _AuditHandler([FlagObservation(ObservationStatus.PRESENT, value="x")])
    timeline = Timeline()
    ch = _Ch(h)
    cbo = {("team1", "svc"): ch, ("boom", "svc"): ch}

    def target_factory(team_id, c):
        if team_id == "boom":
            raise RuntimeError("cannot resolve host")
        return _target_factory(team_id, c)

    server = FlagServer(_ctx(timeline), challenge_by_owner=cbo,
                        target_factory=target_factory)
    for t in ("team1", "boom"):
        server.store.planted[(t, "svc", "default")] = FlagRecord(
            flag=Flag(value=f"FLAG{{{t}}}"), handle=f"h-{t}", round_id=1)

    server.audit(round_id=1)  # must not raise
    ev = {e.team_id: e.payload for e in _events(timeline)}
    assert set(ev) == {"team1", "boom"}          # both recorded, neither dropped
    assert ev["team1"]["status"] == "PRESENT"    # healthy item still audited
    assert ev["boom"]["initial_status"] == "ERROR"
    assert ev["boom"]["status"] == "ERROR"
    assert ev["boom"]["repair"] is None
    assert ev["boom"]["detail"] == "audit item raised"


def test_audit_skips_store_without_a_challenge():
    h = _AuditHandler([FlagObservation(ObservationStatus.PRESENT, value="x")])
    server, tl = _server(h)
    # a planted store whose challenge is (unexpectedly) absent must be skipped,
    # not crash the pass.
    server.store.planted[("ghost", "svc", "default")] = FlagRecord(
        flag=Flag(value="g"), handle="hg", round_id=1)
    server.audit(round_id=1)
    assert {e.team_id for e in _events(tl)} == {"team1"}
