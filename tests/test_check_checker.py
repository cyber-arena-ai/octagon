"""static_phase.check_checker — the optional CHECKER tier: PASS for each present
form, a NOTE when a defense-capable challenge has neither, FAIL for a broken
checker.py, and a dry-exec of checker.py's run()."""

from __future__ import annotations

import importlib
import sys
import textwrap
from types import SimpleNamespace

import pytest

import challenges
from challenges.check import static_phase
from challenges.check.report import Report, Status

# A trivial network checker that does NOT connect, so dry-exec is deterministic.
_CHECKER_OK = """
from challenges.interface import CheckResult, FunctionalityTest
class C(FunctionalityTest):
    @property
    def name(self): return "c"
    def run(self, target): return CheckResult(name="checker", passed=True)
"""

_CHECKER_BROKEN = "import totally_missing_dep_qqq\n"


@pytest.fixture
def make_pkg(tmp_path):
    root = tmp_path / "warehouse"
    root.mkdir()
    challenges.__path__.append(str(root))
    created: list[str] = []

    def _make(name: str, files: dict[str, str]):
        d = root / name
        (d / "image").mkdir(parents=True)
        (d / "__init__.py").write_text("")
        for fname, body in files.items():
            (d / fname).write_text(textwrap.dedent(body))
        created.append(name)
        importlib.invalidate_caches()
        return name, d

    yield _make

    challenges.__path__.remove(str(root))
    for name in created:
        for mod in list(sys.modules):
            if mod == f"challenges.{name}" or mod.startswith(f"challenges.{name}."):
                del sys.modules[mod]
    importlib.invalidate_caches()


def _ch(defense_capable=True):
    return SimpleNamespace(
        name="x",
        restart_handler=object() if defense_capable else None,
        vulbox=SimpleNamespace(exposed_ports={"service": 1}),
    )


def _status(rep, name):
    es = [c for c in rep.checks if c.group == "checker" and c.name == name]
    return es[0].status if es else None


def test_note_when_defense_capable_and_neither(make_pkg):
    snake, d = make_pkg("chk_none", {"functionality_test.py": _CHECKER_OK})
    rep = Report(challenge=snake)
    static_phase.check_checker(snake, d, _ch(defense_capable=True), rep)
    assert _status(rep, "presence") == Status.NOTE


def test_no_note_when_not_defense_capable(make_pkg):
    snake, d = make_pkg("chk_smoke", {"functionality_test.py": _CHECKER_OK})
    rep = Report(challenge=snake)
    static_phase.check_checker(snake, d, _ch(defense_capable=False), rep)
    assert _status(rep, "presence") is None  # framework smoke challenge → no advisory


def test_checker_sh_present_passes(make_pkg):
    snake, d = make_pkg("chk_sh", {"image/checker.sh": "echo ok\n"})
    rep = Report(challenge=snake)
    static_phase.check_checker(snake, d, _ch(), rep)
    assert _status(rep, "checker.sh") == Status.PASS
    assert _status(rep, "presence") is None  # has a checker → no NOTE


def test_checker_py_present_passes_and_dry_execs(make_pkg):
    snake, d = make_pkg("chk_py", {"checker.py": _CHECKER_OK})
    rep = Report(challenge=snake)
    static_phase.check_checker(snake, d, _ch(), rep)
    assert _status(rep, "checker.py") == Status.PASS
    assert _status(rep, "checker.py.run") == Status.PASS  # returns CheckResult
    assert _status(rep, "presence") is None


def test_broken_checker_py_fails(make_pkg):
    snake, d = make_pkg("chk_broken", {"checker.py": _CHECKER_BROKEN})
    rep = Report(challenge=snake)
    static_phase.check_checker(snake, d, _ch(), rep)
    assert _status(rep, "checker.py") == Status.FAIL
