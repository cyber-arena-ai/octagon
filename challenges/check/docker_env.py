"""Runtime helpers shared by the runtime and mini-game phases.

Image building, a thread-bounded call wrapper (the only way to time-box a
blocking docker-exec), the readiness poll, and a free-port helper. None of
these import the docker SDK at module top — the static phase imports this
package indirectly and must stay dependency-light.
"""

from __future__ import annotations

import socket
import threading
from typing import Any, Callable

from challenges.interface import Challenge, CheckResult, VulboxTarget

from .constants import challenge_dir

# Per-probe cap inside await_ready: one functionality_test call can't burn more
# than this, so a probe that blocks against a still-booting service is retried
# rather than failing the whole readiness window.
PROBE_ATTEMPT_TIMEOUT = 20.0


def resolve_and_build_image(snake: str, ch: Challenge, client) -> str:
    """Return a built/available image tag. If the challenge ships build.py (the
    convention the registry deploys by), that path is AUTHORITATIVE: call its
    build_image() and let any failure propagate so it FAILs the image-build
    check — do NOT silently fall back to a build_context build or a stale local
    image and mask a broken builder. Only when there is no build.py do we fall
    back to a raw build_context build, then an already-present image."""
    import importlib

    cdir = challenge_dir(snake)
    if cdir is not None and (cdir / "build.py").exists():
        bm = importlib.import_module(f"challenges.{snake}.build")
        return bm.build_image()
    ref = ch.vulbox.reference
    if ch.vulbox.build_context:
        client.images.build(path=ch.vulbox.build_context, tag=ref,
                            platform="linux/amd64", rm=True)
        return ref
    client.images.get(ref)  # raises if absent
    return ref


def call_with_timeout(fn: Callable[[], Any], timeout: float) -> tuple[str, Any]:
    """Run fn() in a daemon thread bounded by `timeout`.

    Returns ("ok", value) | ("error", exc) | ("timeout", None). On timeout the
    worker thread is abandoned (daemon — it dies with the process); this is the
    only way to bound a blocking docker-exec the SDK can't interrupt.
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 — relayed to the caller
            box["e"] = e

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(max(0.0, timeout))
    if th.is_alive():
        return "timeout", None
    if "e" in box:
        return "error", box["e"]
    return "ok", box.get("v")


def await_ready(ch: Challenge, target: VulboxTarget, timeout: float) -> tuple[bool, str]:
    """Poll functionality_test until it passes or `timeout` elapses.

    Each probe call is bounded by the remaining budget, so a probe that blocks
    (e.g. a contributor TCP connect with no socket timeout) can't hang the run
    past `timeout`. Returns (ready, detail) — `detail` describes the last
    outcome (a raised exception, a wrong return type, or the failing leaves) so
    a not-ready verdict is diagnosable instead of a bare timeout.
    """
    import time

    deadline = time.monotonic() + timeout
    last_detail = "functionality_test never produced a result"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last_detail
        # Bound each probe by a fixed attempt cap, not the whole remaining
        # budget: a slow-booting service often blocks the FIRST probe (e.g. a
        # checker opening a socket with no connect timeout against a port that
        # isn't listening yet), and one hung call must not consume the entire
        # window. Cap it so we keep retrying until the service answers or the
        # deadline passes — the same tolerance the game's repeated polling has.
        attempt = min(PROBE_ATTEMPT_TIMEOUT, remaining)
        status, val = call_with_timeout(lambda: ch.functionality_test.run(target), attempt)
        if status == "timeout":
            last_detail = (f"functionality_test did not return within {attempt:.0f}s "
                           f"(still booting?); retried until {int(timeout)}s")
        elif status == "error":
            last_detail = f"functionality_test.run raised {val!r}"
        elif not isinstance(val, CheckResult):
            last_detail = f"functionality_test.run returned {type(val).__name__}, expected CheckResult"
        elif val.passed:
            return True, leaf_summary(val)
        else:
            last_detail = leaf_summary(val)
        # Clamp the backoff so we never sleep past the deadline.
        time.sleep(min(3.0, max(0.0, deadline - time.monotonic())))


def leaf_summary(r: CheckResult | None) -> str:
    if r is None:
        return "no result"
    if not r.children:
        return f"passed={r.passed} {r.detail[:80]}"
    return " ".join(f"{c.name}={'ok' if c.passed else 'FAIL'}" for c in r.children)


def free_ports(n: int) -> list[int]:
    """`n` DISTINCT free ports. Binds all `n` sockets at once before reading any
    port, so the OS can't hand the same ephemeral port back twice — binding one
    at a time and closing each before the next can return duplicates, which
    would make two facility containers publish the same host port. Closes them
    all before returning; a tiny bind race with another process remains, but the
    per-run namespacing covers the rest."""
    socks = []
    try:
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("", 0))
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()
