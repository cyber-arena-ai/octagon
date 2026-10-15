"""Phase 1 — static package contract (no Docker).

Validate the contributor's directory without running it: required files, the
interface shapes, probe names/ports, dry handler calls against a fake exec, and
that the prompt + build entry points import and run. Touches only
challenges.interface + the one challenge under test.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from typing import Any, Callable

from challenges.interface import (
    ChallengeDocs,
    Challenge,
    CheckResult,
    FlagHandler,
    FlagObservation,
    FunctionalityTest,
    OracleKind,
    RestartHandler,
    VulboxImage,
    VulboxProbe,
    VulboxTarget,
    validate_flag_store_specs,
    validate_state_region_specs,
)

from .constants import NAME_RE, REQUIRED_FILES, challenge_dir
from .flag_checks import (
    CHECK_ROUND_CONTEXT_SEED,
    make_check_flags,
    validate_handles,
)
from .report import Report, Status
from .source_scan import accepts_kwarg, source_binds


def fake_target(ch: Challenge) -> VulboxTarget:
    def _exec(_host: str, _cmd: str) -> tuple[int, str]:
        return (0, "")

    ports = {}
    try:
        ports = dict(ch.vulbox.exposed_ports)
    except Exception:
        pass
    return VulboxTarget(host="check", ports=ports,
                        meta={"team_id": "check", "exec_in_container": _exec})


def check_structure(d: Path, rep: Report) -> None:
    for rel in REQUIRED_FILES:
        present = (d / rel).exists()
        rep.add("structure", rel, Status.PASS if present else Status.FAIL,
                "" if present else "missing")


def check_interface(ch: Challenge, rep: Report) -> None:
    g = "interface"

    # name
    try:
        name = ch.name
        if isinstance(name, str) and NAME_RE.match(name):
            rep.add(g, "name", Status.PASS, name)
        else:
            rep.add(g, "name", Status.FAIL,
                    f"{name!r} is not hyphenated lowercase (e.g. nautilus-ico)")
    except Exception as e:
        rep.add(g, "name", Status.FAIL, repr(e))

    # vulbox
    try:
        vb = ch.vulbox
        if not isinstance(vb, VulboxImage):
            rep.add(g, "vulbox", Status.FAIL, f"not a VulboxImage: {type(vb).__name__}")
        else:
            # Ports verdict is about ports only — don't fold reference-emptiness
            # in (that gets its own check). bool is a subclass of int, so a
            # boolean port value must be rejected explicitly.
            ports_ok = bool(vb.exposed_ports) and all(
                isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
                for k, v in vb.exposed_ports.items()
            )
            rep.add(g, "vulbox.reference", Status.PASS if vb.reference else Status.FAIL,
                    vb.reference or "empty")
            rep.add(g, "vulbox.exposed_ports",
                    Status.PASS if ports_ok else Status.FAIL, str(vb.exposed_ports))
            if vb.build_context is not None:
                bc = Path(vb.build_context)
                rep.add(g, "vulbox.build_context",
                        Status.PASS if bc.is_dir() else Status.FAIL,
                        str(vb.build_context) if bc.is_dir() else f"not a dir: {vb.build_context}")
    except Exception as e:
        rep.add(g, "vulbox", Status.FAIL, repr(e))

    # the three probes (type) + docs
    for attr, abc in (
        ("functionality_test", FunctionalityTest),
        ("flag_handler", FlagHandler),
    ):
        try:
            obj = getattr(ch, attr)
            rep.add(g, attr, Status.PASS if isinstance(obj, abc) else Status.FAIL,
                    "" if isinstance(obj, abc) else f"not a {abc.__name__}")
        except Exception as e:
            rep.add(g, attr, Status.FAIL, repr(e))

    # restart_handler — the real-challenge gate (None is reserved for
    # framework smoke tests only).
    try:
        rh = ch.restart_handler
        if rh is None:
            rep.add(g, "restart_handler", Status.FAIL,
                    "is None — defense runs every round; a real challenge must override it")
        elif not isinstance(rh, RestartHandler):
            rep.add(g, "restart_handler", Status.FAIL, f"not a RestartHandler: {type(rh).__name__}")
        else:
            rep.add(g, "restart_handler", Status.PASS, "defense-enabled")
    except Exception as e:
        rep.add(g, "restart_handler", Status.FAIL, repr(e))

    # docs
    try:
        docs = ch.docs
        if not isinstance(docs, ChallengeDocs):
            rep.add(g, "docs", Status.FAIL, f"not a ChallengeDocs: {type(docs).__name__}")
        else:
            ok = bool(docs.intro.strip()) and bool(docs.func_test_spec.strip())
            rep.add(g, "docs", Status.PASS if ok else Status.FAIL,
                    "" if ok else "intro / func_test_spec must be non-empty")
    except Exception as e:
        rep.add(g, "docs", Status.FAIL, repr(e))

    # `flag_store_specs()` is the authoritative store declaration (the default
    # wraps `flag_stores()` for a plain READ challenge), so it is the ONE view
    # checked here — exactly what the flag-service plants from.
    specs = None
    try:
        specs = list(ch.flag_store_specs())
        validate_flag_store_specs(specs, challenge=ch.name)
        rep.add(g, "flag_store_specs", Status.PASS,
                ", ".join(f"{sp.name}:{sp.kind.value}" for sp in specs))
        write_stores = [sp.name for sp in specs if sp.kind is OracleKind.WRITE]
        execute_stores = [sp.name for sp in specs if sp.kind is OracleKind.EXECUTE]
        if write_stores:
            rep.add(
                "capability proof", "WRITE positive claim", Status.NOTE,
                f"UNVERIFIED in static/build phases for stores {write_stores}; "
                "run --game with a certified _exploit.py",
            )
        if execute_stores:
            rep.add(
                "capability proof", "EXECUTE service-context execution", Status.NOTE,
                f"UNVERIFIED in static/build phases for stores {execute_stores}; "
                "run --game with a certified _exploit.py",
            )
    except Exception as e:
        rep.add(g, "flag_store_specs", Status.FAIL, repr(e))
        specs = None

    try:
        regions = list(ch.authoritative_state_regions())
        validate_state_region_specs(regions, challenge=ch.name)
        write_stores = (
            [spec.name for spec in specs if spec.kind is OracleKind.WRITE]
            if specs is not None else []
        )
        if write_stores and not regions:
            raise ValueError(
                "must declare at least one region for "
                f"WRITE store(s) {write_stores}"
            )
        rep.add(g, "authoritative_state_regions", Status.PASS,
                ", ".join(r.name for r in regions) or "none")
    except Exception as e:
        rep.add(g, "authoritative_state_regions", Status.FAIL, repr(e))


def check_probes(ch: Challenge, rep: Report) -> None:
    g = "probes"
    try:
        exposed = set(ch.vulbox.exposed_ports)
    except Exception:
        exposed = set()

    for attr in ("functionality_test", "flag_handler", "restart_handler"):
        try:
            probe = getattr(ch, attr)
        except Exception:
            continue
        if not isinstance(probe, VulboxProbe):
            continue
        # name
        try:
            pname = probe.name
            rep.add(g, f"{attr}.name", Status.PASS if pname else Status.FAIL, str(pname))
        except Exception as e:
            rep.add(g, f"{attr}.name", Status.FAIL, repr(e))
        # required_ports subset of exposed_ports
        try:
            rp = probe.required_ports
            unknown = [p for p in rp if p not in exposed]
            if unknown:
                rep.add(g, f"{attr}.required_ports", Status.FAIL,
                        f"{unknown} not in exposed_ports {sorted(exposed)}")
            else:
                rep.add(g, f"{attr}.required_ports", Status.PASS, str(tuple(rp)))
        except Exception as e:
            rep.add(g, f"{attr}.required_ports", Status.FAIL, repr(e))


# Exceptions that signal a contract/structural bug (accessing a missing
# meta/ports key, wrong types) — a hard FAIL even against the fake exec. Domain
# exceptions (RuntimeError/ValueError from a handler that validates exec
# output) are downgraded to NOTE: the canned (rc=0, '') reply can't satisfy
# real output checks, so --build is the authoritative run for those.
# NotImplementedError is included: a stubbed `raise NotImplementedError` probe
# is an unimplemented handler, which must FAIL the gate — not pass as advisory.
# IndexError is deliberately EXCLUDED: it's almost always a CORRECT handler
# parsing the empty fake output (e.g. out.splitlines()[-1]) — a domain failure
# the real --build run resolves, not a contract bug. So it downgrades to NOTE.
_STRUCTURAL_EXC = (KeyError, AttributeError, TypeError, NameError,
                   NotImplementedError)


def _dry_exc_status(e: Exception) -> tuple[Status, str]:
    if isinstance(e, _STRUCTURAL_EXC):
        return Status.FAIL, f"raised {e!r}"
    return Status.NOTE, f"raised {e!r} against the fake exec (rc=0,''); verify with --build"


def check_dry_exec(ch: Challenge, rep: Report) -> None:
    """Run every probe against a fake exec (rc=0, output=''). Confirms the
    code path executes and returns the right type — not that it passes."""
    g = "dry-exec"
    t = fake_target(ch)

    # functionality_test.run -> CheckResult
    try:
        r = ch.functionality_test.run(t)
        rep.add(g, "functionality_test.run",
                Status.PASS if isinstance(r, CheckResult) else Status.FAIL,
                "returns CheckResult" if isinstance(r, CheckResult)
                else f"returned {type(r).__name__}")
    except Exception as e:
        rep.add(g, "functionality_test.run", *_dry_exc_status(e))

    # flag plant -> retrieve
    flags = None
    handles = None
    handles_ok = False
    try:
        flags = make_check_flags(ch)
        plant_target = VulboxTarget(
            host=t.host,
            ports=dict(t.ports),
            meta={**t.meta, "round_context_seed": CHECK_ROUND_CONTEXT_SEED},
        )
        handles = ch.flag_handler.plant(plant_target, flags)
        handles_ok, detail = validate_handles(flags, handles)
        rep.add(g, "flag_handler.plant",
                Status.PASS if handles_ok else Status.FAIL, detail)
    except Exception as e:
        rep.add(g, "flag_handler.plant", *_dry_exc_status(e))

    # Only probe retrieve after plant returned one valid handle per store.
    if handles_ok and handles is not None:
        for store, handle in handles.items():
            try:
                obs = ch.flag_handler.retrieve(t, handle, expected=flags[store].value)
                if not isinstance(obs, FlagObservation):
                    rep.add(g, "flag_handler.retrieve", Status.FAIL,
                            f"store {store!r} returned {type(obs).__name__}, "
                            "expected FlagObservation")
                    break
            except Exception as e:
                # FlagHandler.retrieve's contract is "never raises; return an
                # ERROR FlagObservation when unreadable", so any exception is a
                # hard FAIL.
                rep.add(g, "flag_handler.retrieve", Status.FAIL,
                        f"store {store!r} raised {e!r} — retrieve must never raise "
                        "(return an ERROR FlagObservation instead)")
                break
        else:
            rep.add(g, "flag_handler.retrieve", Status.PASS,
                    f"returns FlagObservation for stores {sorted(handles)}")

    # restart_handler.run -> CheckResult
    try:
        rh = ch.restart_handler
        if rh is not None:
            r = rh.run(t)
            rep.add(g, "restart_handler.run",
                    Status.PASS if isinstance(r, CheckResult) else Status.FAIL,
                    "returns CheckResult" if isinstance(r, CheckResult)
                    else f"returned {type(r).__name__}")
    except Exception as e:
        rep.add(g, "restart_handler.run", *_dry_exc_status(e))


_DRY_EXEC_TIMEOUT_S = 10.0


def _call_with_timeout(fn: Callable[[], Any], timeout_s: float):
    """Run `fn()` on a daemon thread bounded by `timeout_s`. Returns
    `("ok", result)`, re-raises `fn`'s exception, or returns `("timeout", None)`
    if it doesn't finish. Guards the static phase against a `checker.py` whose
    network client blocks against the (unroutable) fake target."""
    box: dict[str, Any] = {}

    def _work() -> None:
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001 — surfaced to the caller below
            box["e"] = e

    t = threading.Thread(target=_work, name="dry-exec", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        return ("timeout", None)
    if "e" in box:
        raise box["e"]
    return ("ok", box.get("r"))


def check_checker(snake: str, d: Path, ch: Challenge, rep: Report) -> None:
    """The CHECKER runs OUTSIDE prod's trust boundary, in two OPTIONAL forms:
    `checker.py` (a network `FunctionalityTest` run in the poller) and/or
    `image/checker.sh` (shipped over stdin into prod). Neither is required, but a
    defense-capable challenge with NO deep checker gates defense only on the
    shallow tcp/process leaves — worth an advisory. A present `checker.py` is
    dry-exec'd (same domain-vs-structural downgrade as the FT path)."""
    from challenges.registry import load_optional_probe

    g = "checker"
    sh_present = (d / "image" / "checker.sh").is_file()

    # checker.py: load_optional_probe returns None when absent, raises on a
    # broken module (missing dep, syntax error, two concrete classes).
    py_probe = None
    py_present = False
    try:
        py_probe = load_optional_probe(snake, "checker", FunctionalityTest)
        py_present = py_probe is not None
        if py_present:
            rep.add(g, "checker.py", Status.PASS, "network checker (run in the poller)")
    except Exception as e:
        rep.add(g, "checker.py", Status.FAIL, f"present but broken: {e!r}")

    if sh_present:
        rep.add(g, "checker.sh", Status.PASS, "in-prod checker (shipped over stdin)")

    # Advisory: defense-capable challenge with no deep checker.
    try:
        defense_capable = ch.restart_handler is not None
    except Exception:
        defense_capable = False
    if defense_capable and not py_present and not sh_present:
        rep.add(g, "presence", Status.NOTE,
                "no checker.py / checker.sh — defense scored on tcp/process only; "
                "add a deep CHECKER (prefer checker.py) to catch cheese-path patches")

    # Dry-exec checker.py's run() (a network probe; against the fake target it
    # usually can't connect → a domain exception downgraded to NOTE, like the FT).
    # Bounded by a timeout so a client that blocks (long connect timeout / retry)
    # can't hang the static check — reported as a NOTE, resolved by --build.
    if py_probe is not None:
        try:
            status, r = _call_with_timeout(
                lambda: py_probe.run(fake_target(ch)), _DRY_EXEC_TIMEOUT_S)
            if status == "timeout":
                rep.add(g, "checker.py.run", Status.NOTE,
                        f"did not return within {_DRY_EXEC_TIMEOUT_S:g}s against the "
                        "fake target; verify with --build")
            else:
                rep.add(g, "checker.py.run",
                        Status.PASS if isinstance(r, CheckResult) else Status.FAIL,
                        "returns CheckResult" if isinstance(r, CheckResult)
                        else f"returned {type(r).__name__}")
        except Exception as e:
            rep.add(g, "checker.py.run", *_dry_exc_status(e))


def check_prompts_build(snake: str, ch: Challenge, rep: Report) -> None:
    g = "prompts/build"

    prompt_ok = False
    try:
        pm = importlib.import_module(f"challenges.{snake}.prompts")
        fn = getattr(pm, "build_advanced_prompt", None)
        if not callable(fn):
            rep.add(g, "prompts.build_advanced_prompt", Status.FAIL, "missing or not callable")
            fn = None
        else:
            missing = [
                kw for kw in ("service_name", "hint")
                if not accepts_kwarg(fn, kw)
            ]
            if missing:
                rep.add(g, "prompts.build_advanced_prompt", Status.FAIL,
                        f"signature missing kwarg(s) {', '.join(missing)} — called as "
                        "build_advanced_prompt(cfg, service_name=, hint=)")
                fn = None
            else:
                rep.add(g, "prompts.build_advanced_prompt", Status.PASS,
                        "callable; accepts service_name and hint")
                prompt_ok = True
    except Exception as e:
        rep.add(g, "prompts module", Status.FAIL, repr(e))
        fn = None

    # Exercise both L2 variants and the same canonical L1+L2 assembler used by
    # GameMgr. This catches package-convention resolution and provider-aware
    # rendering failures that a direct module call alone cannot detect.
    from models.agent import AgentConfig  # static-phase dep (dataclass only)
    cfg = AgentConfig(
        cli="check", team_id="check", own_prod_host="prod",
        opposing_prod_hosts=("opponent",),
    )
    if prompt_ok and fn is not None:
        _check_prompt_runs(
            g, "build_advanced_prompt hint=True",
            lambda: fn(cfg, service_name=ch.name, hint=True), rep,
        )
        _check_prompt_runs(
            g, "build_advanced_prompt hint=False",
            lambda: fn(cfg, service_name=ch.name, hint=False), rep,
        )
        try:
            from agents.prompts import build_async_prompt
            _check_prompt_runs(
                g, "build_async_prompt",
                lambda: build_async_prompt(
                    cfg, service_name=ch.name, total_rounds=2,
                    round_min=1, hint=True,
                ),
                rep,
            )
        except Exception as e:
            rep.add(g, "build_async_prompt runs", Status.FAIL, f"raised {e!r}")

    # build.py: confirm it defines a top-level build_image WITHOUT importing it
    # — build.py imports the docker SDK at module top, and the static phase must
    # stay dependency-light. The --build phase imports + calls it for real.
    cdir = challenge_dir(snake)
    bpath = cdir / "build.py" if cdir is not None else None
    try:
        ok = bpath is not None and source_binds(bpath, "build_image")
        rep.add(g, "build.build_image", Status.PASS if ok else Status.FAIL,
                "defined (source check)" if ok else "no top-level build_image in build.py")
    except Exception as e:
        rep.add(g, "build module", Status.FAIL, repr(e))


def _check_prompt_runs(g: str, label: str, call: Callable[[], Any], rep: Report) -> None:
    """Invoke a prompt builder and require a non-empty string back."""
    try:
        out = call()
    except Exception as e:
        rep.add(g, f"{label} runs", Status.FAIL, f"raised {e!r}")
        return
    if isinstance(out, str) and out.strip():
        rep.add(g, f"{label} runs", Status.PASS, f"returned {len(out)} chars")
    else:
        rep.add(g, f"{label} runs", Status.FAIL,
                f"returned {type(out).__name__}, expected non-empty str")
