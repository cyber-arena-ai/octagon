"""challenges.registry — auto-discovery of challenges from challenges/*/.

Pins the discovery contract: every well-formed challenge package is found by
its `name` class attribute; malformed names / duplicate names / multi-class
packages fail loud; un-importable packages are skipped (not fatal) and resurface
on lookup; image builds resolve by the `<pkg>.build.build_image()` convention.
"""

from __future__ import annotations

import types

import pytest

from challenges.interface import (
    Challenge,
    ChallengeDocs,
    RestartScriptInitialStart,
    VulboxImage,
    VulboxTarget,
)
from challenges.nautilus_ico import IcoChallenge
from challenges.notes import NotesChallenge
from challenges import registry

# The stdlib-only challenges always importable in CI. Others (faust-*) carry
# third-party deps and may be skipped depending on the environment, so we only
# assert these are a subset of what discovery returns.
_ALWAYS = {
    "notes", "nautilus-ico", "mlflow-lfi", "aiohttp-lfi",
    "gradio-lfi", "pyload-rce", "marimo-rce", "jenkins-controller-write",
}


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    """Discovery is cached; clear it so monkeypatched-discovery tests start
    from a clean slate and don't poison the real cache for later tests."""
    registry._cache = None
    registry._broken = {}
    yield
    registry._cache = None
    registry._broken = {}


# ── test doubles ──────────────────────────────────────────────────────


class _FakeBase(Challenge):
    """Concrete Challenge for tests — implements the abstract surface in the
    class body so subclasses are non-abstract (hence discoverable)."""
    name = "fake"

    @property
    def vulbox(self):
        return VulboxImage(reference="x", exposed_ports={"service": 1})

    @property
    def functionality_test(self):  # pragma: no cover — never called
        raise NotImplementedError

    @property
    def flag_handler(self):  # pragma: no cover — never called
        raise NotImplementedError

    @property
    def docs(self):
        return ChallengeDocs(intro="", agent_prompts={}, func_test_spec="")


def _concrete(name: str, module: str = "challenges.fake.challenge"):
    """A concrete Challenge subclass with a controllable name + __module__."""
    clsname = "Fake_" + name.replace("-", "_")
    cls = type(clsname, (_FakeBase,), {"name": name})
    cls.__module__ = module
    return cls


def _pkg_module(pkg: str, *classes) -> types.ModuleType:
    m = types.ModuleType(f"challenges.{pkg}")
    for c in classes:
        setattr(m, c.__name__, c)
    return m


def _target(host: str = "team1_prod", *, port: int = 5000) -> VulboxTarget:
    return VulboxTarget(host=host, ports={"service": port}, meta={})


def _record_exec():
    calls: list[tuple[str, str]] = []
    queue: list[tuple[int, str]] = []

    def fake(host: str, cmd: str) -> tuple[int, str]:
        calls.append((host, cmd))
        return queue.pop(0) if queue else (0, "")

    return fake, calls, queue


# ── build_challenge / known_challenge_names ───────────────────────────


def test_build_challenge_returns_instance():
    assert isinstance(registry.build_challenge("notes"), NotesChallenge)
    assert isinstance(registry.build_challenge("nautilus-ico"), IcoChallenge)


def test_challenge_package_is_derived_from_discovered_class():
    assert registry.challenge_package("notes") == "notes"
    assert registry.challenge_package("nautilus-ico") == "nautilus_ico"


def test_challenge_package_unknown_raises():
    with pytest.raises(KeyError, match="unknown challenge"):
        registry.challenge_package("bogus")


def test_known_challenge_names_sorted_and_superset():
    names = registry.known_challenge_names()
    assert names == sorted(names)
    assert _ALWAYS <= set(names)


def test_build_challenge_unknown_raises_with_known_list():
    # NB: assert on str(exc), not pytest's `match` — KeyError's repr escapes
    # quotes, and the message embeds quoted names.
    with pytest.raises(KeyError) as ei:
        registry.build_challenge("bogus")
    msg = str(ei.value)
    assert "unknown challenge" in msg and "bogus" in msg and "known" in msg


def test_factories_return_fresh_instances():
    a = registry.build_challenge("notes")
    b = registry.build_challenge("notes")
    assert a is not b


# ── _challenge_subclass_in ────────────────────────────────────────────


def test_subclass_in_returns_sole_concrete():
    cls = _concrete("foo", module="challenges.foo.challenge")
    mod = _pkg_module("foo", cls)
    assert registry._challenge_subclass_in(mod, "foo") is cls


def test_subclass_in_returns_none_when_no_challenge():
    mod = _pkg_module("foo")  # exports nothing
    assert registry._challenge_subclass_in(mod, "foo") is None


def test_subclass_in_ignores_abstract_and_mixins():
    """The ABC and the RestartScriptInitialStart mixin are not concrete
    challenges, so a module exposing only them yields None."""
    mod = _pkg_module("foo")
    mod.Challenge = Challenge
    mod.RestartScriptInitialStart = RestartScriptInitialStart
    assert registry._challenge_subclass_in(mod, "foo") is None


def test_subclass_in_ignores_foreign_module_classes():
    """A class defined under a different package (re-imported) doesn't count."""
    foreign = _concrete("bar", module="challenges.bar.challenge")
    mod = _pkg_module("foo", foreign)
    assert registry._challenge_subclass_in(mod, "foo") is None


def test_subclass_in_raises_on_multiple():
    a = _concrete("a", module="challenges.foo.challenge")
    b = _concrete("b", module="challenges.foo.challenge")
    mod = _pkg_module("foo", a, b)
    with pytest.raises(ValueError, match="exactly one concrete Challenge"):
        registry._challenge_subclass_in(mod, "foo")


# ── name / interval validation ────────────────────────────────────────


@pytest.mark.parametrize("bad", [None, "", "   ", 123])
def test_validate_name_rejects_bad(bad):
    cls = _concrete("ok")
    cls.name = bad
    with pytest.raises(ValueError, match="name"):
        registry._validate_name(cls)


def test_validate_name_rejects_non_kebab():
    cls = _concrete("Not_Kebab")
    with pytest.raises(ValueError, match="kebab"):
        registry._validate_name(cls)


def test_validate_name_accepts_kebab():
    assert registry._validate_name(_concrete("nautilus-ico")) == "nautilus-ico"


@pytest.mark.parametrize("bad", [0, -5, -0.1, True, "30"])
def test_validate_interval_rejects_bad(bad):
    cls = _concrete("foo")
    cls.health_interval_secs = bad
    with pytest.raises(ValueError, match="positive"):
        registry._validate_interval(cls)


def test_validate_interval_accepts_positive_and_default():
    cls = _concrete("foo")
    cls.health_interval_secs = 30.0
    registry._validate_interval(cls)  # no raise
    # The inherited property default (None) is left alone.
    registry._validate_interval(_concrete("bar"))


# ── discovery: skip-on-import-failure + duplicate ─────────────────────


def _patch_discovery(monkeypatch, infos, modules):
    monkeypatch.setattr(
        registry.pkgutil, "iter_modules",
        lambda path, prefix="": [
            types.SimpleNamespace(name=prefix + n, ispkg=p) for n, p in infos
        ],
    )
    monkeypatch.setattr(
        registry.importlib, "import_module",
        lambda name: modules[name.rsplit(".", 1)[1]],
    )


def test_discover_skips_unimportable_package(monkeypatch):
    class _Boom(dict):  # import_module raises when this pkg is requested
        pass

    def fake_import(name):
        pkg = name.rsplit(".", 1)[1]
        if pkg == "broken":
            raise ModuleNotFoundError("No module named 'requests'")
        return _pkg_module("good", _concrete("good", "challenges.good.challenge"))

    monkeypatch.setattr(
        registry.pkgutil, "iter_modules",
        lambda path, prefix="": [
            types.SimpleNamespace(name=prefix + "broken", ispkg=True),
            types.SimpleNamespace(name=prefix + "good", ispkg=True),
        ],
    )
    monkeypatch.setattr(registry.importlib, "import_module", fake_import)
    found = registry._discover()
    assert "good" in found
    assert "broken" in registry._broken
    # Referencing the skipped package surfaces the import reason.
    registry._cache = found
    with pytest.raises(KeyError, match="skipped on import failure"):
        registry.build_challenge("broken")


def test_discover_raises_on_duplicate_name(monkeypatch):
    a = _concrete("dup", module="challenges.one.challenge")
    b = _concrete("dup", module="challenges.two.challenge")
    modules = {"one": _pkg_module("one", a), "two": _pkg_module("two", b)}
    _patch_discovery(monkeypatch, [("one", True), ("two", True)], modules)
    with pytest.raises(ValueError, match="duplicate challenge name 'dup'"):
        registry._discover()


# ── initial_start (now on the Challenge) ──────────────────────────────


def test_initial_start_default_waits_for_service_port():
    """The default initial_start polls the primary service port via a /dev/tcp
    probe before returning — it is no longer a no-op (it gates the first flag
    plant against a still-booting service)."""
    exec_in, calls, _ = _record_exec()
    registry.build_challenge("notes").initial_start(_target(), exec_in)
    assert len(calls) == 1
    host, cmd = calls[0]
    assert host == "team1_prod"
    assert "/dev/tcp/127.0.0.1/5000" in cmd


def test_initial_start_ico_kicks_restart_script():
    exec_in, calls, _ = _record_exec()
    registry.build_challenge("nautilus-ico").initial_start(
        _target(host="team2_prod", port=4265), exec_in)
    # restart.sh first, then the readiness port-wait (same as the default path).
    assert calls[0] == ("team2_prod", "/arena/restart.sh")
    assert len(calls) == 2
    assert "/dev/tcp/127.0.0.1/4265" in calls[1][1]


def test_initial_start_ico_tolerates_failure():
    exec_in, _calls, queue = _record_exec()
    queue.append((1, "fpc: not found"))
    # No raise.
    registry.build_challenge("nautilus-ico").initial_start(_target(port=4265), exec_in)


def test_ico_health_interval_default():
    assert registry.build_challenge("nautilus-ico").health_interval_secs == 30.0


def test_notes_health_interval_default_is_none():
    assert registry.build_challenge("notes").health_interval_secs is None


# ── build_images ──────────────────────────────────────────────────────


class _Cfg:
    def __init__(self, *names: str) -> None:
        self.challenges = [type("_C", (), {"name": n})() for n in names]


def _fake_build_module(tag, calls=None):
    m = types.ModuleType("build")

    def build_image():
        if calls is not None:
            calls.append(tag)
        return tag

    m.build_image = build_image
    return m


def test_build_images_invokes_per_challenge_builder(monkeypatch):
    # Warm the real registry first, THEN patch import_module — so discovery
    # uses real packages but build resolution returns fakes (no docker import).
    registry._registry()
    fakes = {
        "challenges.notes.build": _fake_build_module("cyberarena/chal-notes:test"),
        "challenges.nautilus_ico.build": _fake_build_module("cyberarena/chal-nautilus-ico:test"),
    }
    monkeypatch.setattr(registry.importlib, "import_module", lambda n: fakes[n])
    tags = registry.build_images(_Cfg("notes", "nautilus-ico"))
    assert tags == [
        "cyberarena/chal-notes:test",
        "cyberarena/chal-nautilus-ico:test",
    ]


def test_build_images_dedupes(monkeypatch):
    registry._registry()
    calls: list[str] = []
    fakes = {"challenges.notes.build": _fake_build_module("tag", calls)}
    monkeypatch.setattr(registry.importlib, "import_module", lambda n: fakes[n])
    registry.build_images(_Cfg("notes", "notes"))
    assert calls == ["tag"]


def test_build_images_unknown_challenge_raises():
    with pytest.raises(KeyError, match="unknown challenge"):
        registry.build_images(_Cfg("bogus"))


def test_build_images_missing_build_module_raises(monkeypatch):
    """A discovered challenge whose package lacks build.py fails with a
    package-named error, not a bare ImportError."""
    cls = _concrete("needs-build", module="challenges.no_build_pkg.challenge")
    monkeypatch.setitem(registry._registry(), "needs-build", cls)
    with pytest.raises(RuntimeError, match="no_build_pkg.*build_image"):
        registry.build_images(_Cfg("needs-build"))


def test_build_images_empty():
    assert registry.build_images(_Cfg()) == []
