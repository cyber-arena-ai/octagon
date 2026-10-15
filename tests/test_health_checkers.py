"""health.checkers — load_checkers (auto-detect the two forms) and
run_checker_leaves (independent run, leaf naming, head+tail truncation)."""

from __future__ import annotations

import importlib
import sys
import textwrap

import pytest

import challenges
from challenges.interface import CheckResult, FunctionalityTest, VulboxTarget
from health import checkers

_FT_ONE = """
from challenges.interface import CheckResult, FunctionalityTest
class C(FunctionalityTest):
    @property
    def name(self): return "c"
    def run(self, target): return CheckResult(name="checker", passed=True)
"""


@pytest.fixture
def make_pkg(tmp_path):
    root = tmp_path / "warehouse"
    root.mkdir()
    challenges.__path__.append(str(root))
    created: list[str] = []

    def _make(name: str, files: dict[str, str]) -> str:
        d = root / name
        (d / "image").mkdir(parents=True)
        (d / "__init__.py").write_text("")
        for fname, body in files.items():
            (d / fname).write_text(textwrap.dedent(body))
        created.append(name)
        importlib.invalidate_caches()
        return name

    yield _make

    challenges.__path__.remove(str(root))
    for name in created:
        for mod in list(sys.modules):
            if mod == f"challenges.{name}" or mod.startswith(f"challenges.{name}."):
                del sys.modules[mod]
    importlib.invalidate_caches()


def _target(exec_stdin=None) -> VulboxTarget:
    meta = {} if exec_stdin is None else {"exec_stdin": exec_stdin}
    return VulboxTarget(host="h", ports={}, meta=meta)


# ── load_checkers ─────────────────────────────────────────────────────────────

def test_load_checkers_both_present(make_pkg):
    make_pkg("c_both", {
        "checker.py": _FT_ONE,
        "image/checker.sh": "#!/bin/bash\necho hi\n",
    })
    probe, sh = checkers.load_checkers("c_both")
    assert isinstance(probe, FunctionalityTest)
    assert sh == b"#!/bin/bash\necho hi\n"


def test_load_checkers_neither_present(make_pkg):
    make_pkg("c_none", {"functionality_test.py": _FT_ONE})
    probe, sh = checkers.load_checkers("c_none")
    assert probe is None and sh is None


def test_load_checkers_only_sh(make_pkg):
    make_pkg("c_sh", {"image/checker.sh": "echo ok\n"})
    probe, sh = checkers.load_checkers("c_sh")
    assert probe is None and sh == b"echo ok\n"


# ── run_checker_leaves ────────────────────────────────────────────────────────

class _FT(FunctionalityTest):
    def __init__(self, tree):
        self._t = tree

    @property
    def name(self):
        return "x"

    def run(self, target):
        return self._t


def test_single_form_named_checker():
    leaves = checkers.run_checker_leaves(
        _target(), _FT(CheckResult(name="whatever", passed=True)), None)
    # checker.py leaf keeps its author name (here "whatever"); no relabel.
    assert [l.name for l in leaves] == ["whatever"]


def test_sh_only_named_checker():
    leaves = checkers.run_checker_leaves(
        _target(lambda s: (0, "ok", "", False)), None, b"x")
    assert [(l.name, l.passed) for l in leaves] == [("checker", True)]


def test_both_present_sh_named_checker_sh():
    leaves = checkers.run_checker_leaves(
        _target(lambda s: (0, "ok", "", False)),
        _FT(CheckResult(name="checker", passed=True)), b"x")
    assert [l.name for l in leaves] == ["checker", "checker.sh"]


def test_sh_timeout_failed_leaf():
    leaves = checkers.run_checker_leaves(
        _target(lambda s: (-1, "", "", True)), None, b"x")
    assert leaves[0].passed is False and "timed out" in leaves[0].detail


def test_sh_no_transport_failed_leaf():
    leaves = checkers.run_checker_leaves(_target(exec_stdin=None), None, b"x")
    assert leaves[0].passed is False and "exec_stdin" in leaves[0].detail


def test_head_tail_truncation_keeps_leading_error():
    lead = "FATAL: parse error at line 1"
    body = lead + " " + ("retry " * 200)

    leaves = checkers.run_checker_leaves(
        _target(lambda s: (1, "", body, False)), None, b"x")
    detail = leaves[0].detail
    assert "FATAL: parse error" in detail  # leading error survived
    assert "…truncated…" in detail
