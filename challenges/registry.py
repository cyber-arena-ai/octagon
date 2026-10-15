"""Challenge registry — auto-discovers challenges from `challenges/*/`.

Public entry points:
  build_challenge(name)      instantiate the discovered Challenge
  challenge_package(name)    package directory name for the Challenge class
  known_challenge_names()    sorted list of discovered names
  build_images(cfg)          docker build for cfg's challenges

Discovery scans every package under `challenges/`, imports it, and registers
the single concrete `Challenge` subclass it exports by its `name` class
attribute. Adding a challenge is therefore: drop `challenges/<name>/` (a package
exporting exactly one concrete `Challenge` with a `build.py:build_image()`),
then reference it from `game.yaml`. No edit to this file.

Per-challenge behavior lives on the `Challenge` itself, not here:
  - first-start hook  -> `challenge.initial_start(target, exec_in)`
  - health interval   -> `challenge.health_interval_secs` (+ game.yaml override)
  - image build       -> `challenges.<pkg>.build.build_image()` (convention)
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import pkgutil
import re

import challenges
from challenges.interface import Challenge

log = logging.getLogger(__name__)

# kebab-case: lowercase alnum groups joined by single hyphens.
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

_cache: dict[str, type[Challenge]] | None = None
_broken: dict[str, str] = {}
"""package dir name -> import-error summary, for packages discovery skipped."""


# ── discovery ─────────────────────────────────────────────────────────


def _challenge_subclass_in(mod, pkg: str) -> type[Challenge] | None:
    """The one concrete `Challenge` subclass defined in package `pkg`.

    Returns None when the package exports none (not a challenge package).
    Raises when it exports more than one — discovery must not guess which is
    "the" challenge. Only counts classes *defined* under `challenges.<pkg>`, so
    a package re-importing another challenge's class doesn't false-positive.
    """
    prefix = f"challenges.{pkg}"
    found: list[type[Challenge]] = []
    for obj in vars(mod).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, Challenge)
            and obj is not Challenge
            and not inspect.isabstract(obj)
            and obj.__module__.startswith(prefix)
            and obj not in found
        ):
            found.append(obj)
    if len(found) > 1:
        raise ValueError(
            f"challenges.{pkg}: expected exactly one concrete Challenge "
            f"subclass, found {[c.__qualname__ for c in found]}"
        )
    return found[0] if found else None


def _validate_name(cls: type[Challenge]) -> str:
    where = f"{cls.__module__}.{cls.__qualname__}"
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where}: `name` must be a non-empty str, got {name!r}")
    if not _KEBAB.match(name):
        raise ValueError(f"{where}: challenge name {name!r} is not kebab-case")
    return name


def _validate_interval(cls: type[Challenge]) -> None:
    # `health_interval_secs` may be a plain class attr (override) or the
    # inherited property (default None). Only a concrete numeric override is
    # checkable without instantiating; a property descriptor is left to runtime.
    val = vars(cls).get("health_interval_secs")
    if val is None or isinstance(val, property):
        return
    if not isinstance(val, (int, float)) or isinstance(val, bool) or val <= 0:
        raise ValueError(
            f"{cls.__module__}.{cls.__qualname__}: health_interval_secs must be "
            f"a positive number, got {val!r}"
        )


def _discover() -> dict[str, type[Challenge]]:
    found: dict[str, type[Challenge]] = {}
    broken: dict[str, str] = {}
    for info in pkgutil.iter_modules(challenges.__path__, prefix="challenges."):
        pkg = info.name.rsplit(".", 1)[1]
        # `interface` and `check` are framework packages, not challenges.
        if pkg in ("interface", "check") or not info.ispkg:
            continue
        # A package that won't import (missing optional dep, syntax error) is
        # skipped, not fatal — so a stdlib-only deploy (e.g. notes) isn't
        # bricked by another challenge's deps. The failure resurfaces loudly if
        # something actually references the skipped challenge (see _unknown).
        try:
            mod = importlib.import_module(info.name)
        except Exception as e:  # noqa: BLE001 — record any import failure
            broken[pkg] = f"{type(e).__name__}: {e}"
            log.warning(
                "challenge package %r skipped — import failed (%s)",
                pkg, broken[pkg],
            )
            continue
        cls = _challenge_subclass_in(mod, pkg)
        if cls is None:
            continue
        name = _validate_name(cls)
        _validate_interval(cls)
        if name in found:
            raise ValueError(
                f"duplicate challenge name {name!r}: "
                f"{found[name].__module__} vs {cls.__module__}"
            )
        found[name] = cls
    global _broken
    _broken = broken
    return found


def _registry() -> dict[str, type[Challenge]]:
    global _cache
    if _cache is None:
        _cache = _discover()
    return _cache


def _unknown(name: str) -> KeyError:
    msg = f"unknown challenge: {name!r}; known: {known_challenge_names()}"
    if _broken:
        msg += f"; packages skipped on import failure: {_broken}"
    return KeyError(msg)


# ── public API ────────────────────────────────────────────────────────


def build_challenge(name: str) -> Challenge:
    """Instantiate the discovered Challenge. Raises KeyError if unknown."""
    cls = _registry().get(name)
    if cls is None:
        raise _unknown(name)
    return cls()


def challenge_package(name: str) -> str:
    """Return the snake-case package that defines a discovered challenge."""
    cls = _registry().get(name)
    if cls is None:
        raise _unknown(name)
    return cls.__module__.split(".", 2)[1]


def load_probe(package: str, kind: str, base: type):
    """Import ONLY `challenges.<package>.<kind>` and return the single concrete
    `base` subclass defined there, instantiated.

    For the facility containers (flag-service / health-poller): they run ONE
    probe, so they load just it — no registry discovery (which imports every
    challenge package), no Challenge instantiation (which can shell out, e.g.
    `git rev-parse` for an image ref). `package` is the snake dir
    (`nautilus_ico`); `kind` is `"flag_handler"` / `"functionality_test"`; `base`
    is the matching ABC. Parameterless construction (the project convention)."""
    mod = importlib.import_module(f"challenges.{package}.{kind}")
    cands = [
        o for o in vars(mod).values()
        if isinstance(o, type) and issubclass(o, base) and o is not base
        and not inspect.isabstract(o) and o.__module__ == mod.__name__
    ]
    if len(cands) != 1:
        raise ValueError(
            f"challenges.{package}.{kind}: expected exactly one concrete "
            f"{base.__name__} subclass, found {[c.__name__ for c in cands]}"
        )
    return cands[0]()


def load_optional_probe(package: str, kind: str, base: type):
    """Like `load_probe`, but returns `None` when the module
    `challenges.<package>.<kind>` itself does not exist (an OPTIONAL probe, e.g.
    `checker.py`). Any other failure propagates loudly — a missing *dependency*
    imported by the module (a `ModuleNotFoundError` whose `.name` is not this
    module), a missing relative submodule, a plain `ImportError`, a
    `SyntaxError`, or an import-time side-effect exception. Never mask a broken
    probe as "absent": the gate is strictly `e.name == modname`. The
    exactly-one-concrete-subclass rule still applies via `load_probe`."""
    modname = f"challenges.{package}.{kind}"
    try:
        importlib.import_module(modname)
    except ModuleNotFoundError as e:
        # None ONLY when the checker module itself — or an ANCESTOR package of it
        # — is absent (checker genuinely not provided). `e.name` is NOT always
        # `modname`: a missing PARENT package reports the parent (e.g. package
        # `challenges.<pkg>` missing → `e.name == "challenges.<pkg>"`), hence the
        # prefix check. Anything else is a BROKEN checker and must propagate: a
        # missing dependency (`nclib` → `e.name == "nclib"`), a missing
        # sibling/helper submodule, etc. — never masked as "absent".
        if e.name and (e.name == modname or modname.startswith(e.name + ".")):
            return None
        raise
    return load_probe(package, kind, base)


def package_dir(package: str):
    """Filesystem directory of the `challenges.<package>` package (wherever it
    physically lives), or `None` if it can't be located. importlib-based, so it
    resolves inside the lean facility images too — unlike
    `challenges.check.constants.challenge_dir`, which is authoring tooling not
    guaranteed to ship in the poller. Used to read `image/checker.sh` bytes."""
    from pathlib import Path

    try:
        spec = importlib.util.find_spec(f"challenges.{package}")
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    locs = list(spec.submodule_search_locations or [])
    if locs:
        return Path(locs[0]).resolve()
    if spec.origin and spec.origin != "namespace":
        return Path(spec.origin).resolve().parent
    return None


def known_challenge_names() -> list[str]:
    return sorted(_registry())


def build_images(cfg) -> list[str]:
    """Build (or cache-hit) every image referenced by cfg.challenges.

    Each challenge package must expose `build.py:build_image()` — discovery
    finds the package via the class's module. A missing convention is a hard,
    package-named error, never a bare ImportError/AttributeError.
    """
    tags: list[str] = []
    seen: set[str] = set()
    for chcfg in cfg.challenges:
        if chcfg.name in seen:
            continue
        seen.add(chcfg.name)
        cls = _registry().get(chcfg.name)
        if cls is None:
            raise _unknown(chcfg.name)
        pkg = ".".join(cls.__module__.split(".")[:2])  # challenges.<pkg>
        try:
            build_mod = importlib.import_module(f"{pkg}.build")
        except ModuleNotFoundError as e:
            raise RuntimeError(
                f"challenge {chcfg.name!r} ({pkg}): expected {pkg}.build with "
                f"build_image()"
            ) from e
        build_image = getattr(build_mod, "build_image", None)
        if build_image is None:
            raise RuntimeError(
                f"challenge {chcfg.name!r} ({pkg}): {pkg}.build has no build_image()"
            )
        tags.append(build_image())
    return tags
