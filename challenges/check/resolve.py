"""Locate and import a challenge by name or path.

`locate` and `resolve_challenge` are the two ways a phase finds its subject:
`locate` returns the package directory (for the static structure checks);
`resolve_challenge` returns a live `Challenge` instance (for the mini-game).
Both accept a kebab `Challenge.name`, a `challenges/<snake>` path, or a bare
`<snake>` dir name.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from challenges.interface import Challenge

from .constants import challenge_dir
from .report import Status


def locate(target: str) -> tuple[str | None, Path | None, str]:
    """Map a name-or-path to (snake_dir, abs_path, error). error == '' on success.

    Challenges live in the selected challenge-warehouse checkout and are surfaced under
    `challenges.<snake>` via challenges.__path__, so the directory is resolved
    through the package (challenge_dir), not a fixed `challenges/<snake>/` path.
    """
    # An explicit path to a challenge dir (e.g. inside the warehouse checkout).
    p = Path(target)
    if p.is_dir() and (p / "challenge.py").exists():
        resolved = p.resolve()
        snake = resolved.name
        # It still has to be importable as `challenges.<snake>` and resolve back
        # to THIS dir, else a stray same-named dir would shadow the real one.
        d = challenge_dir(snake)
        if d is None or d != resolved:
            return None, None, (
                f"challenge dir {target!r} is not importable as challenges.{snake} "
                f"(challenges live in the selected challenge warehouse; got {resolved})"
            )
        return snake, resolved, ""

    # A bare snake dir name — resolve via the package path.
    d = challenge_dir(target)
    if d is not None and (d / "challenge.py").exists():
        return target, d, ""

    # Maybe a registered Challenge.name (kebab) — derive its dir from the module.
    try:
        from challenges import registry
        ch = registry.build_challenge(target)
    except KeyError:
        ch = None  # not a registered name — genuinely unknown
    except Exception as e:
        # build_challenge() triggers discovery of EVERY challenge package, so a
        # class-level defect in ANOTHER challenge (duplicate name, >1 subclass,
        # a `name` that isn't a class attribute) aborts discovery here. Surface
        # the error without claiming `target` itself is broken — the cause may
        # be a different package.
        return None, None, (
            f"registry discovery failed while resolving {target!r}: {e!r} "
            f"(this may be a defect in another challenge package, not {target!r})"
        )
    if ch is not None:
        snake = type(ch).__module__.split(".")[1]
        d = challenge_dir(snake)
        if d is not None:
            return snake, d, ""

    return None, None, f"cannot locate challenge {target!r} (no challenges.{target} package)"


def discover_all() -> list[str]:
    """Every challenge package surfaced under `challenges` — the framework dir plus
    each selected warehouse contributor dir (via challenges.__path__) — that ships
    a challenge.py, minus the framework packages. Snake names, like the registry."""
    import pkgutil
    import challenges
    out = []
    for info in pkgutil.iter_modules(challenges.__path__):
        if not info.ispkg or info.name in ("interface", "check"):
            continue
        d = challenge_dir(info.name)
        if d is not None and (d / "challenge.py").exists():
            out.append(info.name)
    return sorted(out)


def load_challenge_class(snake: str) -> tuple[type[Challenge] | None, str, Status]:
    """Import challenges.<snake> and return (class, message, severity).

    Success is (cls, "", Status.PASS). A hard problem returns Status.FAIL with a
    message; cls is None when there is nothing usable to continue with, or the
    preferred class when the report can still be produced (multiple subclasses).
    """
    try:
        mod = importlib.import_module(f"challenges.{snake}")
    except Exception as e:  # noqa: BLE001 — surface any import error verbatim
        return None, f"import challenges.{snake} failed: {e!r}", Status.FAIL

    found = [
        obj
        for obj in vars(mod).values()
        if isinstance(obj, type)
        and issubclass(obj, Challenge)
        and obj is not Challenge
        # Count only CONCRETE subclasses, matching the auto-registry
        # (registry._challenge_subclass_in), so an abstract intermediate isn't
        # mistaken for a second challenge.
        and not inspect.isabstract(obj)
        and (
            getattr(obj, "__module__", "") == f"challenges.{snake}"
            or getattr(obj, "__module__", "").startswith(f"challenges.{snake}.")
        )
    ]
    if not found:
        return None, f"no Challenge subclass exported from challenges.{snake}", Status.FAIL
    if len(found) > 1:
        names = ", ".join(c.__name__ for c in found)
        # Prefer one named in the package's __all__; else the first.
        exported = getattr(mod, "__all__", None) or []
        preferred = next((c for c in found if c.__name__ in exported), found[0])
        # The auto-registry REQUIRES exactly one concrete Challenge subclass per
        # package (registry._challenge_subclass_in raises on >1), so this is an
        # undeployable challenge -> hard FAIL, not an advisory NOTE. Continue
        # with the preferred class so the rest of the report still gets produced.
        return preferred, (
            f"multiple Challenge subclasses ({names}) — the registry requires "
            f"exactly one; using {preferred.__name__} for the rest of this report"
        ), Status.FAIL
    return found[0], "", Status.PASS


def resolve_challenge(target: str) -> tuple[str | None, Challenge | None, str]:
    """Map a challenge name (kebab) or package dir to (name, instance, error).

    The mini-game needs a live instance, not just a directory. Prefers the
    auto-registry (so a registered name resolves the canonical way), then falls
    back to importing the package directly by dir name.
    """
    from challenges import registry

    try:
        return target, registry.build_challenge(target), ""
    except KeyError:
        known = registry.known_challenge_names()
    # Fall back to a package/dir name like 'nautilus_ico' or 'challenges/nautilus_ico'.
    snake = Path(target).name
    cls, _note, _status = load_challenge_class(snake)
    if cls is not None:
        try:
            return cls.name, cls(), ""
        except Exception as e:  # noqa: BLE001 — surface instantiation failure
            return getattr(cls, "name", target), None, f"instantiation failed: {e!r}"
    return None, None, f"unknown challenge {target!r}; known: {known}"
