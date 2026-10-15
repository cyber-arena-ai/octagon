"""Tests for `challenges.check` — the challenge sanity-checker.

Covers the static (default) tier end-to-end on the in-tree challenges,
plus targeted unit tests for each rule using small synthetic fakes, and
the wiring checks. The --build tier needs Docker + heavy image builds, so
it's validated manually, not here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from challenges.check import (
    docker_env,
    engine,
    flag_checks,
    resolve,
    runtime_phase,
    source_scan,
    static_phase,
)
from challenges.check.cli import main
from challenges.check.report import Check, Report, Status, as_dict, render_text
from challenges.interface import (
    Challenge,
    ChallengeDocs,
    CheckResult,
    Flag,
    FlagHandler,
    FlagObservation,
    FlagStoreSpec,
    FunctionalityTest,
    ObservationStatus,
    OracleKind,
    RestartHandler,
    StateReadResult,
    StateReadStatus,
    StateRegionSpec,
    VulboxImage,
    VulboxTarget,
    exec_read_observe,
    state_scan_observe,
)


# ── synthetic fakes ───────────────────────────────────────────────────


class _FT(FunctionalityTest):
    def __init__(self, ports=("service",)):
        self._ports = ports

    @property
    def name(self) -> str:
        return "fake-ft"

    @property
    def required_ports(self) -> tuple[str, ...]:
        return self._ports

    def run(self, target: VulboxTarget) -> CheckResult:
        # touch the exec callable like a real probe would
        target.meta["exec_in_container"](target.host, "true")
        return CheckResult(name="fake", passed=True)


class _FH(FlagHandler):
    def __init__(self, *, plant_exc: Exception | None = None,
                 retrieve_exc: Exception | None = None):
        self._plant_exc = plant_exc
        self._retrieve_exc = retrieve_exc
        self.planted: Mapping[str, Flag] | None = None
        self.plant_meta: dict | None = None
        self.retrieved: list[str] = []
        self.retrieve_meta: list[dict] = []

    @property
    def name(self) -> str:
        return "fake-fh"

    def plant(self, target: VulboxTarget,
              flags: Mapping[str, Flag]) -> Mapping[str, str]:
        if self._plant_exc is not None:
            raise self._plant_exc
        self.plant_meta = dict(target.meta)
        self.planted = flags
        return {store: f"/flag/{store}" for store in flags}

    def retrieve(self, target: VulboxTarget, handle: str,
                 expected: str | None = None) -> FlagObservation:
        if self._retrieve_exc is not None:
            raise self._retrieve_exc
        self.retrieve_meta.append(dict(target.meta))
        self.retrieved.append(handle)
        return FlagObservation(ObservationStatus.NOT_FOUND)


class _RH(RestartHandler):
    @property
    def name(self) -> str:
        return "fake-rh"

    @property
    def required_ports(self) -> tuple[str, ...]:
        return ("service",)

    def run(self, target: VulboxTarget) -> CheckResult:
        return CheckResult(name="fake-restart", passed=True)


def make_challenge(*, name="good-chal", restart=True, ports=None, ft=None, fh=None,
                   reference="ref:1", stores=None, specs=None, regions=None):
    ports = {"service": 4001} if ports is None else ports
    _ft, _fh, _stores, _specs, _regions = ft, fh, stores, specs, regions

    class _C(Challenge):
        @property
        def name(self) -> str:
            return name

        @property
        def vulbox(self) -> VulboxImage:
            return VulboxImage(reference=reference, exposed_ports=dict(ports))

        @property
        def functionality_test(self):
            return _ft or _FT()

        @property
        def flag_handler(self):
            return _fh or _FH()

        def flag_stores(self) -> list[str]:
            return list(_stores) if _stores is not None else super().flag_stores()

        def flag_store_specs(self) -> list[FlagStoreSpec]:
            return list(_specs) if _specs is not None else super().flag_store_specs()

        def authoritative_state_regions(self) -> list[StateRegionSpec]:
            return list(_regions) if _regions is not None else []

        @property
        def restart_handler(self):
            return _RH() if restart else None

        @property
        def docs(self) -> ChallengeDocs:
            return ChallengeDocs(intro="intro", agent_prompts={}, func_test_spec="spec")

    return _C()


def _by_name(rep: Report, group: str, name: str) -> Check:
    for c in rep.checks:
        if c.group == group and c.name == name:
            return c
    raise AssertionError(f"no check {group}/{name} in {[(c.group, c.name) for c in rep.checks]}")


# ── static tier, end-to-end on real challenges ────────────────────────


def test_nautilus_ico_static_passes():
    rep = engine.check_one("nautilus_ico", build=False)
    assert rep.ok, [(c.group, c.name, c.detail) for c in rep.checks if c.status is Status.FAIL]


def test_notes_static_fails_on_restart_gate():
    """notes is a framework smoke test (restart_handler=None) — the checker
    must flag it as not-a-real-challenge. This is the negative case that
    proves the gate works."""
    rep = engine.check_one("notes", build=False)
    assert not rep.ok
    assert _by_name(rep, "interface", "restart_handler").status is Status.FAIL


def test_unlocatable_challenge_reports_not_located():
    rep = engine.check_one("does-not-exist", build=False)
    assert rep.located is False
    assert rep.ok is False


def test_check_one_accepts_a_path():
    # Challenges live in the challenge-warehouse submodule; a filesystem path to
    # the real package dir is accepted (resolved back via challenge_dir).
    from challenges.check.constants import challenge_dir
    path = str(challenge_dir("nautilus_ico"))
    rep = engine.check_one(path, build=False)
    assert rep.challenge == "nautilus_ico"
    assert rep.ok


# ── interface rules (synthetic) ───────────────────────────────────────


def test_interface_good_challenge_clean():
    rep = Report("x")
    static_phase.check_interface(make_challenge(), rep)
    assert not any(c.status is Status.FAIL for c in rep.checks)


def test_interface_rejects_non_hyphenated_name():
    rep = Report("x")
    static_phase.check_interface(make_challenge(name="Bad_Name"), rep)
    assert _by_name(rep, "interface", "name").status is Status.FAIL


def test_interface_rejects_none_restart_handler():
    rep = Report("x")
    static_phase.check_interface(make_challenge(restart=False), rep)
    assert _by_name(rep, "interface", "restart_handler").status is Status.FAIL


def test_interface_rejects_invalid_flag_stores():
    """`flag_store_specs()` is the one checked view; the default wraps
    `flag_stores()`, so a bad name there still fails through the spec row."""
    rep = Report("x")
    static_phase.check_interface(
        make_challenge(stores=["duplicate", "duplicate"]), rep
    )
    assert _by_name(rep, "interface", "flag_store_specs").status is Status.FAIL


def test_interface_requires_region_for_write_store_and_marks_semantics_unverified():
    specs = [
        FlagStoreSpec("ledger", OracleKind.WRITE,
                      scope="app.cross_boundary.write"),
        FlagStoreSpec("shell", OracleKind.EXECUTE,
                      scope="host.command.execute"),
    ]
    rep = Report("x")
    static_phase.check_interface(make_challenge(specs=specs), rep)

    regions = _by_name(rep, "interface", "authoritative_state_regions")
    assert regions.status is Status.FAIL
    assert "at least one region" in regions.detail
    assert _by_name(rep, "capability proof", "WRITE positive claim").status is Status.NOTE
    assert _by_name(
        rep, "capability proof", "EXECUTE service-context execution"
    ).status is Status.NOTE


def test_interface_accepts_write_store_with_authoritative_region():
    rep = Report("x")
    static_phase.check_interface(
        make_challenge(
            specs=[FlagStoreSpec(
                "ledger", OracleKind.WRITE, scope="app.cross_boundary.write")],
            regions=[StateRegionSpec("ledger", "/var/lib/app/ledger")],
        ),
        rep,
    )
    assert _by_name(
        rep, "interface", "authoritative_state_regions"
    ).status is Status.PASS


def test_write_mark_checker_covers_only_write_and_rejects_false_positive():
    challenge = make_challenge(
        specs=[
            FlagStoreSpec("note"),
            FlagStoreSpec("ledger", OracleKind.WRITE,
                          scope="app.cross_boundary.write"),
            FlagStoreSpec("shell", OracleKind.EXECUTE,
                          scope="host.command.execute"),
        ],
        regions=[StateRegionSpec("ledger", "/var/lib/app/ledger")],
    )
    marks = flag_checks.make_check_marks(challenge)
    assert set(marks) == {"ledger"}

    ok, _detail = flag_checks.check_marks_absent(
        challenge.flag_handler, static_phase.fake_target(challenge),
        {"ledger": "handle"}, marks,
    )
    assert ok

    class _FalsePositiveFH(_FH):
        def retrieve(self, target, handle, expected=None):
            return FlagObservation(ObservationStatus.PRESENT, value=expected)

    ok, detail = flag_checks.check_marks_absent(
        _FalsePositiveFH(), static_phase.fake_target(challenge),
        {"ledger": "handle"}, marks,
    )
    assert not ok and "PRESENT" in detail


def test_write_mark_checker_requires_authoritative_claim_reader():
    target = VulboxTarget(
        host="prod",
        ports={},
        meta={
            "exec_in_container": lambda _host, _cmd: (0, "clean"),
            "read_authoritative_state": lambda _region: StateReadResult(
                StateReadStatus.OK, files={"ledger": b""}),
        },
    )

    class _ExecOnlyFH(_FH):
        def retrieve(self, target, handle, expected=None):
            return exec_read_observe(target, "cat /tmp/ledger", expected)

    ok, detail = flag_checks.check_marks_absent(
        _ExecOnlyFH(), target, {"ledger": "handle"}, {"ledger": "MARK{x}"},
    )
    assert ok, detail

    ok, detail = flag_checks.check_marks_absent(
        _ExecOnlyFH(), target, {"ledger": "handle"}, {"ledger": "MARK{x}"},
        require_authoritative_read=True,
    )
    assert not ok
    assert "did not read authoritative state" in detail

    class _AuthoritativeFH(_FH):
        def retrieve(self, target, handle, expected=None):
            return state_scan_observe(target, "ledger", expected)

    ok, detail = flag_checks.check_marks_absent(
        _AuthoritativeFH(), target, {"ledger": "handle"}, {"ledger": "MARK{x}"},
        require_authoritative_read=True,
    )
    assert ok, detail


def test_probes_reject_required_port_not_exposed():
    rep = Report("x")
    ft = _FT(ports=("nope",))  # required port absent from exposed_ports
    static_phase.check_probes(make_challenge(ft=ft), rep)
    assert _by_name(rep, "probes", "functionality_test.required_ports").status is Status.FAIL


# ── dry-exec classification ───────────────────────────────────────────


def test_dry_exec_structural_exception_is_fail():
    rep = Report("x")
    fh = _FH(plant_exc=KeyError("missing meta key"))
    static_phase.check_dry_exec(make_challenge(fh=fh), rep)
    assert _by_name(rep, "dry-exec", "flag_handler.plant").status is Status.FAIL


def test_dry_exec_domain_exception_is_note():
    """A handler that validates exec output raises on the canned (0,'') fake exec
    — that's advisory, not a failure (the --build run is authoritative)."""
    rep = Report("x")
    fh = _FH(plant_exc=RuntimeError("unexpected reply ''"))
    static_phase.check_dry_exec(make_challenge(fh=fh), rep)
    assert _by_name(rep, "dry-exec", "flag_handler.plant").status is Status.NOTE


def test_dry_exec_notimplemented_is_fail():
    # A stubbed `raise NotImplementedError` handler is unimplemented -> FAIL, not NOTE.
    rep = Report("x")
    fh = _FH(plant_exc=NotImplementedError("todo"))
    static_phase.check_dry_exec(make_challenge(fh=fh), rep)
    assert _by_name(rep, "dry-exec", "flag_handler.plant").status is Status.FAIL


def test_dry_exec_exercises_every_declared_flag_store():
    rep = Report("x")
    fh = _FH()
    static_phase.check_dry_exec(
        make_challenge(fh=fh, stores=["document", "profile"]), rep
    )

    assert fh.planted is not None
    assert set(fh.planted) == {"document", "profile"}
    assert all(isinstance(flag, Flag) for flag in fh.planted.values())
    assert len({flag.value for flag in fh.planted.values()}) == 2
    assert all(
        re.fullmatch(r"FLAG\{[A-Z0-9]{32}\}", flag.value)
        for flag in fh.planted.values()
    )
    assert re.fullmatch(
        r"[0-9a-f]{64}", fh.plant_meta["round_context_seed"])
    assert all("round_context_seed" not in meta for meta in fh.retrieve_meta)
    assert set(fh.retrieved) == {"/flag/document", "/flag/profile"}
    assert _by_name(rep, "dry-exec", "flag_handler.plant").status is Status.PASS
    assert _by_name(rep, "dry-exec", "flag_handler.retrieve").status is Status.PASS


def test_dry_exec_rejects_missing_store_handle_without_cascading_retrieve():
    class _MissingHandleFH(_FH):
        def plant(self, target: VulboxTarget,
                  flags: Mapping[str, Flag]) -> Mapping[str, str]:
            self.planted = flags
            return {"document": "/flag/document"}

    rep = Report("x")
    fh = _MissingHandleFH()
    static_phase.check_dry_exec(
        make_challenge(fh=fh, stores=["document", "profile"]), rep
    )

    assert _by_name(rep, "dry-exec", "flag_handler.plant").status is Status.FAIL
    assert "flag_handler.retrieve" not in [c.name for c in rep.checks]


def test_static_phase_annotations_resolve():
    from typing import get_type_hints

    assert get_type_hints(static_phase._check_prompt_runs)


def test_load_challenge_class_flags_multiple_subclasses(monkeypatch):
    # >1 CONCRETE Challenge subclass is a hard FAIL (the registry rejects it),
    # not a NOTE. Abstract subclasses don't count, so use concrete ones (subclass
    # the concrete _C that make_challenge builds).
    import sys
    import types

    mod = types.ModuleType("challenges._multi")
    concrete = type(make_challenge())  # a concrete _C(Challenge)

    class A(concrete):
        pass

    class B(concrete):
        pass

    A.__module__ = B.__module__ = "challenges._multi"
    mod.A, mod.B = A, B
    monkeypatch.setitem(sys.modules, "challenges._multi", mod)

    cls, note, status = resolve.load_challenge_class("_multi")
    assert status is Status.FAIL
    assert "multiple Challenge subclasses" in note
    assert cls in (A, B)


# ── wiring checks ─────────────────────────────────────────────────────


def test_wiring_passes_for_registered_challenge():
    from challenges.nautilus_ico import IcoChallenge

    rep = Report("nautilus_ico")
    runtime_phase.check_wiring("nautilus_ico", IcoChallenge(), rep)
    fails = [c.name for c in rep.checks if c.status is Status.FAIL]
    assert fails == [], fails


def test_wiring_fails_for_unregistered_challenge():
    rep = Report("ghost")
    runtime_phase.check_wiring("ghost", make_challenge(name="ghost-chal"), rep)
    assert _by_name(rep, "wiring", "registry discovery").status is Status.FAIL
    assert _by_name(rep, "wiring", "prompt auto-discovery").status is Status.FAIL


# ── rendering ─────────────────────────────────────────────────────────


def test_render_text_and_json_shape():
    rep = engine.check_one("nautilus_ico", build=False)
    assert len(rep.checks) > 0          # guard: "checked nothing" must not look like success
    text = render_text(rep)
    assert "challenge: nautilus_ico" in text
    assert "PASS" in text
    d = as_dict(rep)
    assert d["challenge"] == "nautilus_ico" and d["ok"] is True
    assert len(d["checks"]) > 0
    assert all({"group", "name", "status", "detail"} <= set(c) for c in d["checks"])


# ── correctness-fix coverage ──────────────────────────────────────────


def test_interface_rejects_bool_port():
    # bool is a subclass of int — a boolean port value must not pass (C7).
    rep = Report("x")
    static_phase.check_interface(make_challenge(ports={"service": True}), rep)
    assert _by_name(rep, "interface", "vulbox.exposed_ports").status is Status.FAIL


def test_empty_reference_does_not_taint_ports_verdict():
    # An empty reference must FAIL only the reference check, not exposed_ports (C10).
    rep = Report("x")
    static_phase.check_interface(make_challenge(reference=""), rep)
    assert _by_name(rep, "interface", "vulbox.reference").status is Status.FAIL
    assert _by_name(rep, "interface", "vulbox.exposed_ports").status is Status.PASS


def test_dry_exec_retrieve_raising_is_hard_fail():
    # FlagHandler.retrieve's contract is "never raises" — a raise is a FAIL, not a NOTE (C6).
    rep = Report("x")
    static_phase.check_dry_exec(make_challenge(fh=_FH(retrieve_exc=ValueError("boom"))), rep)
    assert _by_name(rep, "dry-exec", "flag_handler.retrieve").status is Status.FAIL


def test_dry_exec_skips_retrieve_when_plant_fails():
    # No fabricated-handle retrieve probe after a failed plant (C12).
    rep = Report("x")
    static_phase.check_dry_exec(make_challenge(fh=_FH(plant_exc=RuntimeError("no"))), rep)
    names = [c.name for c in rep.checks]
    assert "flag_handler.plant" in names
    assert "flag_handler.retrieve" not in names


def test_resolve_surfaces_broken_factory(monkeypatch):
    # A registered name whose factory raises surfaces the real error, not "not found" (C3).
    from challenges import registry

    def boom(_name):
        raise RuntimeError("factory blew up")

    monkeypatch.setattr(registry, "build_challenge", boom)
    snake, d, err = resolve.locate("some-registered-name")
    assert snake is None and "discovery failed" in err and "factory blew up" in err


def test_resolve_unknown_name_is_not_found():
    # Genuinely unknown (KeyError from build_challenge) → the "not found" message.
    snake, d, err = resolve.locate("totally-unknown-xyz")
    assert snake is None and "cannot locate" in err


def test_resolve_rejects_path_outside_challenges(tmp_path):
    # A challenge.py-containing dir that isn't importable as challenges.<name>
    # (not in the warehouse) is rejected (C13).
    d = tmp_path / "mychal"
    d.mkdir()
    (d / "challenge.py").write_text("")
    snake, path, err = resolve.locate(str(d))
    assert snake is None and "not importable" in err


def test_all_with_no_challenges_returns_nonzero(monkeypatch, capsys):
    # all([]) is True — "checked nothing" must not exit 0 (SWEEP-2).
    from challenges.check import cli
    monkeypatch.setattr(cli, "discover_all", lambda: [])
    assert main(["--all"]) == 1


def test_nonpositive_ready_timeout_rejected():
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        main(["nautilus_ico", "--build", "--ready-timeout", "0"])


def test_call_with_timeout_ok_error_timeout():
    import time as _t
    assert docker_env.call_with_timeout(lambda: 42, 1.0) == ("ok", 42)

    def _raise():
        raise ValueError("x")

    status, val = docker_env.call_with_timeout(_raise, 1.0)
    assert status == "error" and isinstance(val, ValueError)

    status, val = docker_env.call_with_timeout(lambda: _t.sleep(5), 0.1)
    assert status == "timeout" and val is None


def test_await_ready_is_bounded_when_probe_hangs():
    # A probe that blocks must not hang the run past the timeout (C5).
    import time as _t

    class _HangFT(FunctionalityTest):
        @property
        def name(self) -> str:
            return "hang"

        def run(self, target: VulboxTarget) -> CheckResult:
            _t.sleep(10)
            return CheckResult(name="x", passed=True)

    ch = make_challenge(ft=_HangFT())
    t0 = _t.monotonic()
    ready, last = docker_env.await_ready(ch, static_phase.fake_target(ch), timeout=0.3)
    assert ready is False
    assert _t.monotonic() - t0 < 3.0  # bounded, not 10s


def test_await_ready_surfaces_probe_exception():
    # A functionality_test that raises is reported with the exception, not a bare timeout.
    class _BoomFT(FunctionalityTest):
        @property
        def name(self) -> str:
            return "boom"

        def run(self, target: VulboxTarget) -> CheckResult:
            raise RuntimeError("probe exploded")

    ch = make_challenge(ft=_BoomFT())
    ready, detail = docker_env.await_ready(ch, static_phase.fake_target(ch), timeout=0.2)
    assert ready is False and "probe exploded" in detail


def test_await_ready_surfaces_wrong_return_type():
    # A probe returning a non-CheckResult must not crash _await_ready; surface the type.
    class _BadFT(FunctionalityTest):
        @property
        def name(self) -> str:
            return "bad"

        def run(self, target: VulboxTarget):
            return "not a CheckResult"

    ch = make_challenge(ft=_BadFT())
    ready, detail = docker_env.await_ready(ch, static_phase.fake_target(ch), timeout=0.2)
    assert ready is False and "expected CheckResult" in detail


def test_advanced_prompt_missing_hint_kwarg_fails(monkeypatch):
    import sys
    import types

    pmod = types.ModuleType("challenges._faketest.prompts")
    pmod.build_advanced_prompt = lambda cfg, *, service_name: "body"
    bmod = types.ModuleType("challenges._faketest.build")
    bmod.build_image = lambda: "tag"
    monkeypatch.setitem(sys.modules, "challenges._faketest.prompts", pmod)
    monkeypatch.setitem(sys.modules, "challenges._faketest.build", bmod)

    rep = Report("x")
    static_phase.check_prompts_build("_faketest", make_challenge(name="x", ports={"service": 4001}), rep)
    c = _by_name(rep, "prompts/build", "prompts.build_advanced_prompt")
    assert c.status is Status.FAIL
    assert "hint" in c.detail


def _fake_cfg(*, name="testchal", port=4001, deploy_on=("team1",),
              teams=(("team1", "team1_prod"),), node_images=None):
    """Duck-typed GameConfig for _verify_config_wiring (no YAML / env / schema)."""
    import types

    node_images = node_images or {"team1_prod": "repo:tag"}

    def node(node_id):
        return types.SimpleNamespace(id=node_id, image=node_images[node_id])

    topology = types.SimpleNamespace(
        teams=[types.SimpleNamespace(id=t, prod=p) for t, p in teams],
        node=node,
    )
    challenges = [types.SimpleNamespace(name=name, port=port, deploy_on=list(deploy_on))]
    return types.SimpleNamespace(challenges=challenges, topology=topology)


def test_config_wiring_clean_when_image_and_port_match():
    ch = make_challenge(name="testchal", reference="repo:tag", ports={"service": 4001})
    cfg = _fake_cfg(name="testchal", port=4001, node_images={"team1_prod": "repo:tag"})
    problems, image_issues = runtime_phase.verify_config_wiring(cfg, "testchal", ch)
    assert problems == [] and image_issues == []


def test_config_wiring_flags_port_mismatch():
    ch = make_challenge(name="testchal", reference="repo:tag", ports={"service": 4001})
    cfg = _fake_cfg(name="testchal", port=9999, node_images={"team1_prod": "repo:tag"})
    problems, _ = runtime_phase.verify_config_wiring(cfg, "testchal", ch)
    assert any("port 9999" in p for p in problems)


def test_config_wiring_flags_unknown_deploy_on_team():
    ch = make_challenge(name="testchal", reference="repo:tag", ports={"service": 4001})
    cfg = _fake_cfg(name="testchal", deploy_on=("ghostteam",))
    problems, _ = runtime_phase.verify_config_wiring(cfg, "testchal", ch)
    assert any("ghostteam" in p for p in problems)


def test_config_wiring_stale_tag_is_a_problem():
    # A resolved same-repo tag drift FAILs: --build validated `repo:right` but the
    # config would deploy `repo:stale` — code the gate never exercised.
    ch = make_challenge(name="testchal", reference="repo:right", ports={"service": 4001})
    cfg = _fake_cfg(name="testchal", node_images={"team1_prod": "repo:stale"})
    problems, image_issues = runtime_phase.verify_config_wiring(cfg, "testchal", ch)
    assert any("repo:stale" in p and "repo:right" in p for p in problems)


def test_config_wiring_debug_variant_tag_is_a_note():
    # A debug/smoke variant config legitimately pins its own tag -> advisory NOTE.
    ch = make_challenge(name="testchal", reference="repo:right", ports={"service": 4001})
    cfg = _fake_cfg(name="testchal", node_images={"team1_prod": "repo:smoke"})
    problems, image_issues = runtime_phase.verify_config_wiring(cfg, "testchal", ch)
    assert problems == []
    assert any("repo:smoke" in i for i in image_issues)


def test_config_wiring_digest_pin_is_unverifiable_note():
    # A digest-pinned prod image (no tag) can't be tag-compared -> NOTE, not a false FAIL.
    ch = make_challenge(name="testchal", reference="repo:v1", ports={"service": 4001})
    cfg = _fake_cfg(name="testchal", node_images={"team1_prod": "repo@sha256:abc123"})
    problems, image_issues = runtime_phase.verify_config_wiring(cfg, "testchal", ch)
    assert problems == []
    assert any("unverifiable" in i for i in image_issues)


def test_config_wiring_tag_with_debug_substring_still_fails():
    # ':v2-debugsymbols' merely CONTAINS 'debug' but isn't a debug variant -> FAIL.
    ch = make_challenge(name="testchal", reference="repo:right", ports={"service": 4001})
    cfg = _fake_cfg(name="testchal", node_images={"team1_prod": "repo:v2-debugsymbols"})
    problems, image_issues = runtime_phase.verify_config_wiring(cfg, "testchal", ch)
    assert any("repo:v2-debugsymbols" in p for p in problems)


def test_config_wiring_unpinned_reference_is_unverified_note():
    # An :unpinned reference can't be checked against the config tag.
    ch = make_challenge(name="testchal", reference="repo:unpinned", ports={"service": 4001})
    cfg = _fake_cfg(name="testchal", node_images={"team1_prod": "repo:whatever"})
    problems, image_issues = runtime_phase.verify_config_wiring(cfg, "testchal", ch)
    assert problems == []
    assert any("unverified" in i for i in image_issues)


def test_config_wiring_different_repo_is_a_problem():
    # A prod image in a *different repository* deploys an unrelated image —
    # that's a deployability bug (FAIL), unlike a same-repo tag difference.
    ch = make_challenge(name="testchal", reference="cyberarena/chal-x:tag", ports={"service": 4001})
    cfg = _fake_cfg(name="testchal", node_images={"team1_prod": "someoneelse/chal-x:tag"})
    problems, image_issues = runtime_phase.verify_config_wiring(cfg, "testchal", ch)
    assert any("different image" in p for p in problems)


def test_image_repo_strips_tag_and_digest_but_not_registry_port():
    assert source_scan.image_repo("cyberarena/chal-nautilus-ico:366520c0cc43") == "cyberarena/chal-nautilus-ico"
    assert source_scan.image_repo("repo:tag") == "repo"
    assert source_scan.image_repo("localhost:5000/foo") == "localhost:5000/foo"   # host:port, no tag
    assert source_scan.image_repo("localhost:5000/foo:v1") == "localhost:5000/foo"
    assert source_scan.image_repo("cyberarena/chal-nautilus-ico@sha256:abc") == "cyberarena/chal-nautilus-ico"


def test_config_wiring_returns_none_when_not_a_challenge_entry():
    ch = make_challenge(name="testchal", reference="repo:tag")
    cfg = _fake_cfg(name="someoneelse")  # no entry for testchal
    problems, image_issues = runtime_phase.verify_config_wiring(cfg, "testchal", ch)
    assert problems is None and image_issues is None


def test_prompts_build_survives_raising_vulbox():
    # A malformed vulbox (raises) is recorded as a FAIL by _check_interface; the
    # later _check_prompts_build must NOT crash on it (which would abort --all) —
    # it falls back to port 0 instead of dereferencing the bad vulbox.
    class _BadVulbox(Challenge):
        @property
        def name(self) -> str:
            return "bad-chal"

        @property
        def vulbox(self) -> VulboxImage:
            raise RuntimeError("boom")

        @property
        def functionality_test(self):
            return _FT()

        @property
        def flag_handler(self):
            return _FH()

        @property
        def restart_handler(self):
            return _RH()

        @property
        def docs(self) -> ChallengeDocs:
            return ChallengeDocs(intro="i", agent_prompts={}, func_test_spec="s")

    rep = Report("bad-chal")
    static_phase.check_prompts_build("bad-chal", _BadVulbox(), rep)  # must not raise


def test_advanced_prompt_full_signature_and_canonical_assembly_pass(monkeypatch):
    import sys
    import types
    import agents.prompts

    pmod = types.ModuleType("challenges._faketest2.prompts")
    pmod.build_advanced_prompt = lambda cfg, *, service_name, hint=True: (
        f"SERVICE {service_name} " + ("Hint: useful" if hint else "There is no hint for this challenge.")
    )
    bmod = types.ModuleType("challenges._faketest2.build")
    bmod.build_image = lambda: "tag"
    monkeypatch.setitem(sys.modules, "challenges._faketest2.prompts", pmod)
    monkeypatch.setitem(sys.modules, "challenges._faketest2.build", bmod)
    monkeypatch.setattr(agents.prompts.registry, "challenge_package", lambda _name: "_faketest2")

    rep = Report("x")
    static_phase.check_prompts_build("_faketest2", make_challenge(name="x", ports={"service": 4001}), rep)
    assert _by_name(rep, "prompts/build", "prompts.build_advanced_prompt").status is Status.PASS
    assert _by_name(rep, "prompts/build", "build_advanced_prompt hint=True runs").status is Status.PASS
    assert _by_name(rep, "prompts/build", "build_advanced_prompt hint=False runs").status is Status.PASS
    assert _by_name(rep, "prompts/build", "build_async_prompt runs").status is Status.PASS


def test_advanced_prompt_missing_service_name_fails(monkeypatch):
    import sys
    import types

    pmod = types.ModuleType("challenges._faketest5.prompts")
    pmod.build_advanced_prompt = lambda cfg, *, hint=True: "body"
    monkeypatch.setitem(sys.modules, "challenges._faketest5.prompts", pmod)

    rep = Report("x")
    static_phase.check_prompts_build("_faketest5", make_challenge(name="x", ports={"service": 4001}), rep)
    c = _by_name(rep, "prompts/build", "prompts.build_advanced_prompt")
    assert c.status is Status.FAIL and "service_name" in c.detail


def _install_fake_prompts(monkeypatch, snake, *, builder):
    import sys
    import types
    import agents.prompts

    pmod = types.ModuleType(f"challenges.{snake}.prompts")
    pmod.build_advanced_prompt = builder
    monkeypatch.setitem(sys.modules, f"challenges.{snake}.prompts", pmod)
    monkeypatch.setattr(agents.prompts.registry, "challenge_package", lambda _name: snake)


def test_prompt_builder_that_raises_is_fail(monkeypatch):
    def boom(cfg, *, service_name, hint=True):
        raise RuntimeError("kaboom")

    _install_fake_prompts(monkeypatch, "_faketest3", builder=boom)
    rep = Report("x")
    static_phase.check_prompts_build("_faketest3", make_challenge(name="x", ports={"service": 4001}), rep)
    c = _by_name(rep, "prompts/build", "build_advanced_prompt hint=True runs")
    assert c.status is Status.FAIL and "kaboom" in c.detail


def test_prompt_builder_returning_non_string_is_fail(monkeypatch):
    _install_fake_prompts(
        monkeypatch, "_faketest4",
        builder=lambda cfg, *, service_name, hint=True: None,
    )
    rep = Report("x")
    static_phase.check_prompts_build("_faketest4", make_challenge(name="x", ports={"service": 4001}), rep)
    c = _by_name(rep, "prompts/build", "build_advanced_prompt hint=True runs")
    assert c.status is Status.FAIL and "expected non-empty str" in c.detail


def test_source_binds_detects_top_level_build_image(tmp_path):
    f = tmp_path / "build.py"
    f.write_text("import docker\n\ndef build_image():\n    return 'x'\n")
    assert source_scan.source_binds(f, "build_image") is True
    g = tmp_path / "nobuild.py"
    g.write_text("import docker\n\ndef other():\n    pass\n")
    assert source_scan.source_binds(g, "build_image") is False


# ── negative health probe (Phase 2 break_service step) ─────────────────


def _probe_target():
    return VulboxTarget(host="check", ports={"service": 4001},
                        meta={"team_id": "check",
                              "exec_in_container": lambda h, c: (0, "")})


class _StateFT(FunctionalityTest):
    """functionality_test whose verdict tracks a shared broken/healthy state —
    a FAITHFUL checker. faithful=False models a no-op that never goes red;
    raise_when_broken=True models a checker that RAISES on the down service
    (which the real poller swallows, keeping it green)."""
    def __init__(self, state, *, faithful=True, raise_when_broken=False):
        self._state, self._faithful, self._raise = state, faithful, raise_when_broken

    @property
    def name(self) -> str:
        return "state-ft"

    @property
    def required_ports(self) -> tuple[str, ...]:
        return ("service",)

    def run(self, target: VulboxTarget) -> CheckResult:
        if self._raise and self._state["broken"]:
            raise ConnectionRefusedError("service down")
        passed = True if not self._faithful else not self._state["broken"]
        return CheckResult(name="state", passed=passed)


def _breakable(*, faithful=True, raises=False, break_exc=None, has_break=True):
    state = {"broken": False}
    ft = _StateFT(state, faithful=faithful, raise_when_broken=raises)

    class _RH2(RestartHandler):
        @property
        def name(self): return "rh"

        @property
        def required_ports(self): return ("service",)

        def run(self, target):
            state["broken"] = False  # restart heals the break
            return CheckResult(name="r", passed=True)

    class _C(Challenge):
        name = "breakable"

        @property
        def vulbox(self): return VulboxImage(reference="r:1", exposed_ports={"service": 4001})

        @property
        def functionality_test(self): return ft

        @property
        def flag_handler(self): return _FH()

        @property
        def restart_handler(self): return _RH2()

        @property
        def docs(self): return ChallengeDocs(intro="i", agent_prompts={}, func_test_spec="s")

        def break_service(self, target, exec_in):
            if break_exc is not None:
                raise break_exc
            if not has_break:
                return None
            state["broken"] = True
            return "stopped the app"

    return _C(), state


def test_break_service_default_is_none():
    # The ABC default: a challenge that ships no hook returns None.
    assert make_challenge().break_service(_probe_target(), lambda h, c: (0, "")) is None


def test_negative_probe_notes_when_no_break_hook():
    rep = Report("x")
    runtime_phase._negative_health_probe(
        make_challenge(), _probe_target(), lambda h, c: (0, ""), rep, "build",
        ready_timeout=2)
    c = _by_name(rep, "build", "negative health probe")
    assert c.status is Status.NOTE and "no break_service hook" in c.detail


def test_negative_probe_passes_and_recovers_when_checker_is_faithful():
    ch, state = _breakable(faithful=True)
    rep = Report("x")
    runtime_phase._negative_health_probe(
        ch, _probe_target(), lambda h, c: (0, ""), rep, "build", ready_timeout=5)
    assert _by_name(rep, "build", "negative health probe").status is Status.PASS
    assert _by_name(rep, "build", "recovery after break").status is Status.PASS
    assert state["broken"] is False  # restart healed it


def test_negative_probe_fails_when_checker_stays_green():
    # break_service really broke the service, but the checker never notices.
    ch, _ = _breakable(faithful=False)
    rep = Report("x")
    runtime_phase._negative_health_probe(
        ch, _probe_target(), lambda h, c: (0, ""), rep, "build", ready_timeout=5)
    c = _by_name(rep, "build", "negative health probe")
    assert c.status is Status.FAIL and "does not detect" in c.detail


def test_negative_probe_fails_when_checker_raises_on_down_service():
    # A checker that RAISES on the down service is a FAIL, not a pass: the real
    # poller swallows a raise and keeps the service green, so it would never flag
    # the down service in-game.
    ch, _ = _breakable(raises=True)
    rep = Report("x")
    runtime_phase._negative_health_probe(
        ch, _probe_target(), lambda h, c: (0, ""), rep, "build", ready_timeout=5)
    c = _by_name(rep, "build", "negative health probe")
    assert c.status is Status.FAIL and "raised" in c.detail


def test_negative_probe_notes_when_break_raises():
    ch, _ = _breakable(break_exc=RuntimeError("boom"))
    rep = Report("x")
    runtime_phase._negative_health_probe(
        ch, _probe_target(), lambda h, c: (0, ""), rep, "build", ready_timeout=2)
    c = _by_name(rep, "build", "negative health probe")
    assert c.status is Status.NOTE and "break_service raised" in c.detail
