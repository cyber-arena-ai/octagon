"""FlagHandler interface pieces that back the structured, read-only `retrieve()`:
the shared `exec_read_observe` helper (reachability-aware docker-exec reads), the
`FlagObservation` value type, and the base `repair()` default."""

from __future__ import annotations

import dataclasses

import pytest

from challenges.interface import (
    FlagObservation,
    ObservationStatus,
    RepairResult,
    SingleFlagHandler,
    VulboxTarget,
    exec_read_observe,
)


def _target(replies, *, raises=False, no_exec=False):
    """VulboxTarget whose exec_in_container returns canned (rc, out) pairs in
    order. The FIRST call is the read; a `true` reachability probe (the second
    reply, when present) runs only when the read fails. `raises` makes the exec
    throw; `no_exec` omits the callable entirely."""
    q = list(replies)

    def fake(host, cmd):
        if raises:
            raise RuntimeError("docker exec died")
        return q.pop(0) if q else (0, "")

    meta = {} if no_exec else {"exec_in_container": fake}
    return VulboxTarget(host="h", ports={}, meta=meta)


# ── exec_read_observe status matrix ───────────────────────────────────────────

def test_present_expected_none():
    o = exec_read_observe(_target([(0, "FLAG{a}\n")]), "cat /f", None)
    assert o.status is ObservationStatus.PRESENT and o.value == "FLAG{a}"


def test_present_expected_match():
    o = exec_read_observe(_target([(0, "FLAG{a}")]), "cat /f", "FLAG{a}")
    assert o.status is ObservationStatus.PRESENT


def test_mismatch_is_exact():
    # substring-but-not-equal is a MISMATCH, not a lenient PRESENT
    o = exec_read_observe(_target([(0, "FLAG{a} trailing")]), "cat /f", "FLAG{a}")
    assert o.status is ObservationStatus.MISMATCH and o.value == "FLAG{a} trailing"


def test_empty_read_is_not_found():
    o = exec_read_observe(_target([(0, "   ")]), "cat /f", None)
    assert o.status is ObservationStatus.NOT_FOUND


def test_read_rc_nonzero_reachable_defaults_to_not_found():
    # read fails (rc!=0) but `true` succeeds → a cat's "no such file" = flag gone
    o = exec_read_observe(_target([(1, ""), (0, "")]), "cat /f", None)
    assert o.status is ObservationStatus.NOT_FOUND


def test_read_rc_nonzero_reachable_error_override():
    # a DB/service read where rc!=0 means unhealthy, not flag-gone
    o = exec_read_observe(_target([(1, ""), (0, "")]), "psql ...", None,
                          read_error_status=ObservationStatus.ERROR)
    assert o.status is ObservationStatus.ERROR


def test_unreachable_is_error():
    # read fails AND `true` fails → box unreachable
    o = exec_read_observe(_target([(1, ""), (1, "")]), "cat /f", None)
    assert o.status is ObservationStatus.ERROR


def test_read_exec_raises_is_error():
    o = exec_read_observe(_target([], raises=True), "cat /f", None)
    assert o.status is ObservationStatus.ERROR


def test_no_exec_in_container_is_error():
    o = exec_read_observe(_target([], no_exec=True), "cat /f", None)
    assert o.status is ObservationStatus.ERROR


def test_happy_path_is_single_exec():
    # the `true` probe must NOT run when the read succeeds
    calls = []

    def fake(host, cmd):
        calls.append(cmd)
        return (0, "FLAG{a}")

    t = VulboxTarget(host="h", ports={}, meta={"exec_in_container": fake})
    exec_read_observe(t, "cat /f", "FLAG{a}")
    assert calls == ["cat /f"]


# ── FlagObservation value type ─────────────────────────────────────────────────

def test_flagobservation_is_frozen():
    o = FlagObservation(ObservationStatus.PRESENT, value="v")
    with pytest.raises(dataclasses.FrozenInstanceError):
        o.status = ObservationStatus.ERROR  # type: ignore[misc]


# ── base repair() default ──────────────────────────────────────────────────────

class _H(SingleFlagHandler):
    @property
    def name(self):
        return "stub"

    def plant_one(self, target, value):  # pragma: no cover — unused
        return "handle"

    def retrieve(self, target, handle, expected=None):  # pragma: no cover — unused
        return FlagObservation(ObservationStatus.NOT_FOUND)


def test_default_repair_is_no_action():
    t = VulboxTarget(host="h", ports={}, meta={})
    assert _H().repair(t, "h") is RepairResult.NO_ACTION
