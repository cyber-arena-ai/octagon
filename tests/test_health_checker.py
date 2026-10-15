"""HealthChecker unit tests.

Stage 4 (challenge interface v2): HealthChecker is a thin wrapper that
delegates to `challenge.functionality_test.run(target)` and records one
SERVICE_HEALTH_CHECK per CheckResult leaf. Per-mechanism branching is
gone from the framework — these tests pin the wrapper contract.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from challenges.interface import (
    Challenge,
    ChallengeDocs,
    CheckResult,
    FlagHandler,
    FlagObservation,
    FunctionalityTest,
    ObservationStatus,
    VulboxImage,
    VulboxTarget,
)
from facility import FacilityContext, LocalEventSink, LocalStatusPort
from gamemgr.events import EventType
from gamemgr.timeline import Timeline
from health.checker import HealthChecker


def _checker(tl: Timeline, *, round: int = 1, phase: str = "ATTACK") -> HealthChecker:
    return HealthChecker(
        FacilityContext(
            status=LocalStatusPort(lambda: {"round": round, "phase": phase}),
            events=LocalEventSink(tl),
        )
    )


class _StubFT(FunctionalityTest):
    """Returns a pre-built CheckResult tree. Tests configure the tree
    per scenario."""

    def __init__(self, tree: CheckResult) -> None:
        self._tree = tree

    @property
    def name(self) -> str:
        return "stub-ft"

    def run(self, target: VulboxTarget) -> CheckResult:
        return self._tree


class _StubFH(FlagHandler):
    @property
    def name(self) -> str:
        return "stub-fh"

    def plant(self, target, flag):  # pragma: no cover — unused here
        raise NotImplementedError

    def retrieve(self, target, handle, expected=None):
        return FlagObservation(ObservationStatus.NOT_FOUND)


class _StubCh(Challenge):
    def __init__(self, tree: CheckResult) -> None:
        self._ft = _StubFT(tree)
        self._fh = _StubFH()

    @property
    def name(self) -> str:
        return "stub"

    @property
    def vulbox(self) -> VulboxImage:
        return VulboxImage(reference="stub", exposed_ports={"service": 1})

    @property
    def functionality_test(self) -> FunctionalityTest:
        return self._ft

    @property
    def flag_handler(self) -> FlagHandler:
        return self._fh

    @property
    def docs(self) -> ChallengeDocs:
        return ChallengeDocs(intro="", agent_prompts={}, func_test_spec="")


def _target() -> VulboxTarget:
    return VulboxTarget(host="team1_prod", ports={"service": 1}, meta={})


def _checks(tl: Timeline) -> list:
    return [e for e in tl.history() if e.type == EventType.SERVICE_HEALTH_CHECK.value]


def _run(checker, **kwargs):
    return asyncio.run(checker.run(**kwargs))


def test_run_emits_one_event_per_leaf_in_pre_order():
    """4-leaf tree (the standard process/tcp/protocol/checker shape)
    yields 4 events whose `level` payload matches the leaf names."""
    tree = CheckResult(name="root", passed=True, children=[
        CheckResult(name="process", passed=True, detail="pgrep rc=0"),
        CheckResult(name="tcp", passed=True, detail="ok"),
        CheckResult(name="protocol", passed=True, detail="ok"),
        CheckResult(name="checker", passed=True, detail="checker ok"),
    ])
    tl = Timeline()
    checker = _checker(tl)
    _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))

    levels = [e.payload["level"] for e in _checks(tl)]
    assert levels == ["process", "tcp", "protocol", "checker"]
    assert all(e.payload["passed"] for e in _checks(tl))


def test_run_propagates_failure_per_leaf():
    tree = CheckResult(name="root", passed=False, children=[
        CheckResult(name="process", passed=True, detail=""),
        CheckResult(name="tcp", passed=False, detail="connection refused"),
    ])
    tl = Timeline()
    checker = _checker(tl)
    _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))

    by_level = {e.payload["level"]: e.payload for e in _checks(tl)}
    assert by_level["process"]["passed"] is True
    assert by_level["tcp"]["passed"] is False
    assert "connection refused" in by_level["tcp"]["detail"]


def test_run_returns_root_check_result():
    tree = CheckResult(name="root", passed=True, children=[
        CheckResult(name="single", passed=True, detail=""),
    ])
    tl = Timeline()
    checker = _checker(tl)
    result = _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))
    assert result is tree


def test_run_handles_single_leaf_tree():
    """A challenge whose FunctionalityTest returns a single leaf (no
    children) should still emit one event."""
    tree = CheckResult(name="alive", passed=True, detail="ok")
    tl = Timeline()
    checker = _checker(tl)
    _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))
    checks = _checks(tl)
    assert len(checks) == 1
    assert checks[0].payload["level"] == "alive"


def test_run_walks_nested_trees():
    """A deeper tree emits one event per leaf, parent rollups omitted."""
    tree = CheckResult(name="root", passed=True, children=[
        CheckResult(name="net", passed=True, children=[
            CheckResult(name="tcp", passed=True),
            CheckResult(name="tls", passed=True),
        ]),
        CheckResult(name="checker", passed=True),
    ])
    tl = Timeline()
    checker = _checker(tl)
    _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))
    levels = [e.payload["level"] for e in _checks(tl)]
    # No "net" or "root" — those are internal rollups.
    assert levels == ["tcp", "tls", "checker"]


def test_status_resolver_stamps_round_and_phase():
    tree = CheckResult(name="single", passed=True)
    tl = Timeline()
    checker = _checker(tl, round=7)
    _run(checker, team_id="team1", service="x", target=_target(), challenge=_StubCh(tree))
    ev = _checks(tl)[0]
    assert ev.round_id == 7
    assert ev.phase == "ATTACK"


def test_trigger_defaults_to_periodic():
    tl = Timeline()
    checker = _checker(tl)
    _run(checker, team_id="team1", service="x", target=_target(),
         challenge=_StubCh(CheckResult(name="single", passed=True)))
    assert _checks(tl)[0].payload["trigger"] == "periodic"


def test_explicit_trigger_is_recorded():
    tl = Timeline()
    checker = _checker(tl)
    _run(checker, team_id="team1", service="x", target=_target(),
         challenge=_StubCh(CheckResult(name="single", passed=True)), trigger="restart")
    assert _checks(tl)[0].payload["trigger"] == "restart"


# ── CHECKER composition (checker.py / checker.sh) ─────────────────────────────
#
# The poll shim may carry `.checker` (a FunctionalityTest) and/or `.checker_sh`
# (bytes). HealthChecker runs them OUTSIDE prod, merges their leaves under a new
# root, AND-combining into the overall pass. Both run independently.


def _ft(tree: CheckResult) -> _StubFT:
    return _StubFT(tree)


def _shim(tree: CheckResult, *, checker=None, checker_sh=None):
    return SimpleNamespace(
        functionality_test=_ft(tree), checker=checker, checker_sh=checker_sh)


def _target_stdin(fn) -> VulboxTarget:
    """Target whose exec_stdin(script)->(rc,out,err,timed_out) is a fake."""
    return VulboxTarget(host="team1_prod", ports={"service": 1},
                        meta={"exec_stdin": fn})


_FT_OK = CheckResult(name="ft", passed=True, children=[
    CheckResult(name="tcp", passed=True, detail="ok")])


def test_checker_py_only_leaves_merged_as_is():
    tl = Timeline()
    checker_probe = _ft(CheckResult(name="checker", passed=True, detail="round-trip ok"))
    shim = _shim(_FT_OK, checker=checker_probe)
    result = _run(_checker(tl), team_id="team1", service="x",
                  target=_target(), challenge=shim)
    levels = [e.payload["level"] for e in _checks(tl)]
    assert levels == ["tcp", "checker"]  # FT leaves first, then checker.py's own
    assert result.passed is True


def test_checker_py_multi_leaf_tree_all_merged():
    """No single-leaf restriction: a checker.py returning a tree contributes all
    its leaves with author names."""
    tl = Timeline()
    checker_probe = _ft(CheckResult(name="checker", passed=True, children=[
        CheckResult(name="auth", passed=True),
        CheckResult(name="upload", passed=True),
    ]))
    shim = _shim(_FT_OK, checker=checker_probe)
    _run(_checker(tl), team_id="team1", service="x", target=_target(), challenge=shim)
    levels = [e.payload["level"] for e in _checks(tl)]
    assert levels == ["tcp", "auth", "upload"]


def test_checker_sh_only_named_checker():
    tl = Timeline()
    calls = {}

    def exec_stdin(script):
        calls["script"] = script
        return (0, "CHECKER ok\n", "", False)

    shim = _shim(_FT_OK, checker_sh=b"#!/bin/bash\necho ok\n")
    result = _run(_checker(tl), team_id="team1", service="x",
                  target=_target_stdin(exec_stdin), challenge=shim)
    levels = [e.payload["level"] for e in _checks(tl)]
    assert levels == ["tcp", "checker"]  # sole checker form → familiar "checker" chip
    assert calls["script"] == b"#!/bin/bash\necho ok\n"
    assert result.passed is True


def test_both_present_names_split_and_order():
    tl = Timeline()
    checker_probe = _ft(CheckResult(name="checker", passed=True))

    def exec_stdin(script):
        return (0, "ok", "", False)

    shim = _shim(_FT_OK, checker=checker_probe, checker_sh=b"x")
    _run(_checker(tl), team_id="team1", service="x",
         target=_target_stdin(exec_stdin), challenge=shim)
    levels = [e.payload["level"] for e in _checks(tl)]
    # FT leaves, then checker.py (author name), then checker.sh — py before sh.
    assert levels == ["tcp", "checker", "checker.sh"]


def test_checker_py_raises_named_checker_py_with_type_and_sh_still_runs():
    tl = Timeline()

    class _Boom(FunctionalityTest):
        @property
        def name(self):
            return "boom"

        def run(self, target):
            raise ValueError("network unreachable")

    sh_calls = {}

    def exec_stdin(script):
        sh_calls["ran"] = True
        return (0, "ok", "", False)

    shim = SimpleNamespace(functionality_test=_ft(_FT_OK), checker=_Boom(),
                           checker_sh=b"x")
    result = _run(_checker(tl), team_id="team1", service="x",
                  target=_target_stdin(exec_stdin), challenge=shim)
    by_level = {e.payload["level"]: e.payload for e in _checks(tl)}
    assert by_level["checker.py"]["passed"] is False
    assert by_level["checker.py"]["detail"].startswith("ValueError: network unreachable")
    assert sh_calls.get("ran") is True  # checker.py failure did NOT skip checker.sh
    assert result.passed is False


def test_checker_sh_nonzero_fails_and_ands_root():
    tl = Timeline()

    def exec_stdin(script):
        return (3, "", "cheese-path detected", False)

    shim = _shim(_FT_OK, checker_sh=b"x")
    result = _run(_checker(tl), team_id="team1", service="x",
                  target=_target_stdin(exec_stdin), challenge=shim)
    by_level = {e.payload["level"]: e.payload for e in _checks(tl)}
    assert by_level["checker"]["passed"] is False
    assert "cheese-path detected" in by_level["checker"]["detail"]
    assert result.passed is False  # FT passed, checker.sh failed → AND is False


def test_no_checkers_returns_ft_tree_unchanged():
    tl = Timeline()
    tree = CheckResult(name="ft", passed=True, children=[
        CheckResult(name="tcp", passed=True)])
    shim = _shim(tree)  # checker=None, checker_sh=None
    result = _run(_checker(tl), team_id="team1", service="x",
                  target=_target(), challenge=shim)
    assert result is tree  # untouched — no synthetic root wrap
