"""CHECKER runner — the deep gameplay-correctness probe(s) the health framework
runs OUTSIDE prod's trust boundary (the defender agent has root on its own prod
box, so anything baked in prod is tamperable).

Two auto-detected, complementary forms per challenge:

  checker.py    an OPTIONAL second `FunctionalityTest` module: a network probe
                run entirely in the Health Poller (over target.host/ports). It
                never execs into prod, so root-on-prod can't touch it. Preferred.
  checker.sh    the existing script, run by shipping its BYTES over stdin to
                `bash -s` in prod (via target.meta["exec_stdin"]) — un-baked from
                the image, so nothing tamperable lands on prod disk. Only for
                checks that genuinely must run inside the prod container.

Both run independently — no short-circuit, each its own leaf, AND-combined by the
caller — so a failure localizes to the network layer (checker.py) vs the in-prod
local layer (checker.sh). `checker.sh` protects checker SOURCE integrity, not
EXECUTION integrity (a root agent can still fake the service); checker.py is the
stronger form.
"""

from __future__ import annotations

import logging
import traceback

from challenges.interface import CheckResult, FunctionalityTest, VulboxTarget
from challenges.registry import load_optional_probe, package_dir
from health.checker import leaves

log = logging.getLogger(__name__)

_HEAD = 120
_TAIL = 120


def load_checkers(package: str) -> tuple[FunctionalityTest | None, bytes | None]:
    """Resolve a challenge's two optional checker forms at configure time.
    Returns `(checker_probe, checker_sh_bytes)`; either may be None. Cache the
    result on the poll shim — do NOT re-read per poll."""
    checker_py = load_optional_probe(package, "checker", FunctionalityTest)
    checker_sh: bytes | None = None
    d = package_dir(package)
    if d is not None:
        p = d / "image" / "checker.sh"
        if p.is_file():
            checker_sh = p.read_bytes()
    return checker_py, checker_sh


def run_checker_leaves(
    target: VulboxTarget,
    checker_py: FunctionalityTest | None,
    checker_sh: bytes | None,
) -> list[CheckResult]:
    """Run every present checker form INDEPENDENTLY (no short-circuit) and return
    a flat list of leaves. Emit order is checker.py-then-checker.sh (a display
    convention only). Each form is individually guarded so one failing/raising
    checker never skips or crashes the other."""
    both = checker_py is not None and checker_sh is not None
    out: list[CheckResult] = []
    if checker_py is not None:
        out.extend(_run_checker_py(target, checker_py))
    if checker_sh is not None:
        out.append(_run_checker_sh(target, checker_sh, name="checker.sh" if both else "checker"))
    return out


def _run_checker_py(target: VulboxTarget, probe: FunctionalityTest) -> list[CheckResult]:
    # Its leaves are merged AS-IS (author-named, no relabel), exactly like the
    # main functionality_test. On exception we can't know which author leaf it
    # maps to, so emit ONE fixed "checker.py" leaf (also disambiguates from a
    # checker.sh-only "checker") carrying the exception TYPE for diagnosis.
    try:
        result = probe.run(target)
    except Exception as e:  # noqa: BLE001 — a broken checker must not crash the poll
        log.warning("checker.py raised: %s", traceback.format_exc())
        return [CheckResult(
            name="checker.py",
            passed=False,
            detail=f"{type(e).__name__}: {e}"[:200],
        )]
    return leaves(result)


def _run_checker_sh(target: VulboxTarget, script: bytes, *, name: str) -> CheckResult:
    exec_stdin = target.meta.get("exec_stdin")
    if exec_stdin is None:
        return CheckResult(name=name, passed=False,
                           detail="no exec_stdin transport wired for checker.sh")
    try:
        rc, out, err, timed_out = exec_stdin(script)
    except Exception as e:  # noqa: BLE001
        log.warning("checker.sh exec failed: %s", traceback.format_exc())
        return CheckResult(name=name, passed=False,
                           detail=f"{type(e).__name__}: {e}"[:200])
    if timed_out:
        return CheckResult(name=name, passed=False, detail="checker.sh timed out")
    body = (err.strip() or out.strip() or f"rc={rc}")
    return CheckResult(name=name, passed=(rc == 0), detail=_truncate(body))


def _truncate(s: str) -> str:
    """head+tail truncation: a leading error line survives even when a chatty
    checker floods the tail with retry noise (a naive last-200 would cut it)."""
    if len(s) <= _HEAD + _TAIL + 16:
        return s[-200:]
    return f"{s[:_HEAD]} …truncated… {s[-_TAIL:]}"
