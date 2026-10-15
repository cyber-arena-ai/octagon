"""HealthChecker — run a challenge's FunctionalityTest, emit one
SERVICE_HEALTH_CHECK event per CheckResult leaf.

The challenge's `FunctionalityTest.run(target)` decides the shape of
the result tree; this module just walks it and records leaves. The
bundled challenges follow the HealthLevel convention
(process/tcp/protocol/checker) so the panel renders the familiar
4-chip strip; non-conventional tree shapes work too, the panel just
shows the raw leaf names.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from challenges.interface import Challenge, CheckResult, VulboxTarget
from facility import FacilityContext
from gamemgr.events import EventType

log = logging.getLogger(__name__)


class HealthChecker:
    """Stateless probe runner. Uses `ctx.events` to record results and
    `ctx.status` to stamp round/phase; never touches `ctx.exec` directly
    (the exec callable rides inside the `VulboxTarget` it's handed)."""

    def __init__(self, ctx: FacilityContext) -> None:
        self._events = ctx.events
        self._status = ctx.status

    async def run(
        self,
        *,
        team_id: str,
        service: str,
        target: VulboxTarget,
        challenge: Challenge,
        trigger: str = "periodic",
    ) -> CheckResult:
        """Run the functionality test and emit one SERVICE_HEALTH_CHECK
        per leaf. Returns the root CheckResult.

        `trigger` records WHY this probe ran — "periodic" (the poll loop),
        "attack_enter" (the one-shot at ATTACK entry), or "restart" (the
        post-restart probe). The periodic cadence and these one-shots
        interleave on the same lane, so the tag is what lets a reader tell
        why two checks are closer together than the poll interval.
        """
        st = self._status.status()
        loop = asyncio.get_running_loop()
        # functionality_test.run is blocking (docker exec under the hood),
        # so it goes through the default executor.
        result = await loop.run_in_executor(
            None, challenge.functionality_test.run, target,
        )

        # Auto-detected CHECKER forms (checker.py / checker.sh), run OUTSIDE
        # prod's trust boundary. Both run independently (no short-circuit) and
        # merge as extra leaves under a new root, AND-ing into the overall pass.
        # See health/checkers.py.
        #
        # checker / checker_sh are attached by service_app wiring (the poll shim).
        # Passing a raw Challenge object will NOT enable deep checks — getattr
        # falls back to None and the CHECKER is silently skipped. Not every
        # challenge has a checker, so absence is legitimate (no exception); but a
        # shim that carries NEITHER attribute is likely a mis-wire, so debug-log
        # it to make that case visible without failing the probe.
        if not hasattr(challenge, "checker") and not hasattr(challenge, "checker_sh"):
            log.debug("no checker metadata attached (raw Challenge?); deep checker skipped")
        checker_py = getattr(challenge, "checker", None)
        checker_sh = getattr(challenge, "checker_sh", None)
        if checker_py is not None or checker_sh is not None:
            from health.checkers import run_checker_leaves
            extra = await loop.run_in_executor(
                None, run_checker_leaves, target, checker_py, checker_sh,
            )
            if extra:
                result = CheckResult(
                    name=result.name,
                    passed=result.passed and all(leaf.passed for leaf in extra),
                    children=[result, *extra],
                )

        # One id per run, shared by every leaf event, so consumers (e.g. the
        # get_health_checks MCP tool) can regroup the per-level events back
        # into the single probe they came from.
        probe_id = uuid.uuid4().hex[:12]
        for leaf in leaves(result):
            self._events.record(
                EventType.SERVICE_HEALTH_CHECK,
                team_id=team_id,
                round_id=st.get("round", 0),
                phase=st.get("phase", ""),
                payload={
                    "team_id": team_id,
                    "service": service,
                    "probe_id": probe_id,
                    "level": leaf.name,
                    "passed": leaf.passed,
                    "detail": leaf.detail[-200:],
                    "trigger": trigger,
                },
            )
        return result


def leaves(node: CheckResult) -> list[CheckResult]:
    """Pre-order walk yielding only nodes with no children. Internal
    nodes (the root rollup over a 4-level chain) are skipped — we
    emit one event per probed dimension, not for the aggregate."""
    if not node.children:
        return [node]
    out: list[CheckResult] = []
    for child in node.children:
        out.extend(leaves(child))
    return out
