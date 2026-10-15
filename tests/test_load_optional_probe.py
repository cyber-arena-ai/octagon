"""challenges.registry.load_optional_probe + package_dir.

load_optional_probe returns None ONLY when the checker module itself is absent
(`e.name == modname`); every other failure — missing dependency, missing relative
submodule, plain ImportError, SyntaxError, import-time side-effect — must
propagate loudly so a broken checker is never masked as "absent". These build
throwaway packages stitched into the `challenges` namespace via `__path__`.
"""

from __future__ import annotations

import importlib
import sys
import textwrap

import pytest

from challenges import registry
import challenges

_FT_ONE = """
from challenges.interface import CheckResult, FunctionalityTest
class C(FunctionalityTest):
    @property
    def name(self): return "c"
    def run(self, target): return CheckResult(name="checker", passed=True)
"""

_FT_TWO = """
from challenges.interface import CheckResult, FunctionalityTest
class A(FunctionalityTest):
    @property
    def name(self): return "a"
    def run(self, target): return CheckResult(name="checker", passed=True)
class B(FunctionalityTest):
    @property
    def name(self): return "b"
    def run(self, target): return CheckResult(name="checker", passed=True)
"""


@pytest.fixture
def make_pkg(tmp_path):
    """Return a factory that writes challenges.<name> under a tmp dir stitched
    into the challenges namespace, and cleans up sys.modules after."""
    root = tmp_path / "warehouse"
    root.mkdir()
    challenges.__path__.append(str(root))
    created: list[str] = []

    def _make(name: str, files: dict[str, str]) -> str:
        d = root / name
        d.mkdir()
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


def _load(pkg):
    from challenges.interface import FunctionalityTest
    return registry.load_optional_probe(pkg, "checker", FunctionalityTest)


def test_package_missing_returns_none():
    assert _load("does_not_exist_pkg_xyz") is None


def test_checker_module_missing_returns_none(make_pkg):
    make_pkg("chk_nochecker", {"functionality_test.py": _FT_ONE})
    assert _load("chk_nochecker") is None


def test_happy_path_returns_instance(make_pkg):
    from challenges.interface import FunctionalityTest
    make_pkg("chk_ok", {"checker.py": _FT_ONE})
    obj = _load("chk_ok")
    assert isinstance(obj, FunctionalityTest)


def test_two_concrete_classes_raises_valueerror(make_pkg):
    make_pkg("chk_two", {"checker.py": _FT_TWO})
    with pytest.raises(ValueError, match="exactly one concrete"):
        _load("chk_two")


def test_missing_third_party_dep_reraises(make_pkg):
    make_pkg("chk_dep", {"checker.py": "import totally_missing_dep_zzz\n"})
    with pytest.raises(ModuleNotFoundError) as ei:
        _load("chk_dep")
    assert ei.value.name == "totally_missing_dep_zzz"  # not masked as absent


def test_missing_relative_submodule_reraises(make_pkg):
    # `from . import X` for a missing submodule raises ImportError (not MNFE);
    # either way it must propagate, never be swallowed as "checker absent".
    make_pkg("chk_rel", {"checker.py": "from . import _missing_sibling\n"})
    with pytest.raises(ImportError) as ei:
        _load("chk_rel")
    assert "_missing_sibling" in str(ei.value)


def test_plain_import_error_propagates(make_pkg):
    make_pkg("chk_imperr", {"checker.py": "raise ImportError('boom-not-mnfe')\n"})
    with pytest.raises(ImportError) as ei:
        _load("chk_imperr")
    assert "boom-not-mnfe" in str(ei.value)


def test_syntax_error_propagates(make_pkg):
    make_pkg("chk_syntax", {"checker.py": "def broken(:\n    pass\n"})
    with pytest.raises(SyntaxError):
        _load("chk_syntax")


def test_import_side_effect_exception_propagates(make_pkg):
    make_pkg("chk_side", {"checker.py": "raise RuntimeError('side-effect boom')\n"})
    with pytest.raises(RuntimeError, match="side-effect boom"):
        _load("chk_side")


def test_package_dir_resolves(make_pkg):
    name = make_pkg("chk_dir", {"checker.py": _FT_ONE})
    d = registry.package_dir(name)
    assert d is not None and d.name == "chk_dir" and (d / "checker.py").is_file()


def test_package_dir_none_when_missing():
    assert registry.package_dir("no_such_pkg_qqq") is None
