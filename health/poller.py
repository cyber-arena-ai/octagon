"""HealthPoller — periodic + on-demand health-check scheduler.

GameMgr drives it at phase boundaries (the poller doesn't subscribe
to anything):

  enter_attack_phase  → on_attack_enter(round_id) — one immediate
                        probe + a per-(team, service) periodic task
  enter_defense_phase / GAME_END / stop → on_attack_exit() — cancel
  _run_restart_job → trigger_now(team, service) — one-shot probe so
                     the agent sees a fresh SERVICE_HEALTH_CHECK
                     right after a defense restart
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from challenges.interface import Challenge, VulboxTarget
from facility import Facility

from .checker import HealthChecker

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECS = 10
"""Fallback when a challenge doesn't override its poll interval."""


@dataclass(frozen=True)
class PollTarget:
    team_id: str
    service: str
    challenge: Challenge
    target: VulboxTarget
    interval_secs: float


class HealthPoller(Facility):
    def __init__(self, checker: HealthChecker, targets: list[PollTarget]) -> None:
        self.checker = checker
        self.targets = targets
        # Per-(team, service) periodic task; populated on on_attack_enter,
        # cancelled on on_attack_exit / stop.
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._running = False

    @property
    def name(self) -> str:
        return "health"

    def on_attack_enter(self, round_id: int) -> list[asyncio.Task]:
        """Fire one immediate probe per target (fire-and-forget), then
        start a periodic task per target. Returns the immediate-probe
        tasks for tests to await; production callers ignore them."""
        if self._running:
            log.warning("poller.on_attack_enter while already running; restarting")
            self.on_attack_exit()
        self._running = True

        immediate: list[asyncio.Task] = []
        for t in self.targets:
            immediate.append(asyncio.create_task(self._safe_run(t, trigger="attack_enter")))
            self._tasks[(t.team_id, t.service)] = asyncio.create_task(
                self._periodic_loop(t)
            )
        return immediate

    def on_attack_exit(self) -> None:
        """Cancel all periodic tasks. Idempotent."""
        self._running = False
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        self._tasks.clear()

    async def trigger_now(
        self, team_id: str, service: str, *, trigger: str = "restart"
    ) -> None:
        """One-shot probe. No-op if the service isn't defense-capable (no
        PollTarget was registered). `trigger` tags the emitted
        SERVICE_HEALTH_CHECK events — "restart" for the post-restart probe
        (default), "manual" for an agent-requested trigger_health_check."""
        target = next(
            (t for t in self.targets if t.team_id == team_id and t.service == service),
            None,
        )
        if target is None:
            log.debug(
                "trigger_now: no poll target for (%s, %s); skipping",
                team_id, service,
            )
            return
        await self._safe_run(target, trigger=trigger)

    async def stop(self) -> None:
        """Cancel everything. Safe to call multiple times."""
        self.on_attack_exit()

    async def _periodic_loop(self, target: PollTarget) -> None:
        """Fixed-rate poll: probes land on a stable `start + n·interval`
        grid regardless of how long each probe takes, so the cadence
        doesn't drift by the probe duration. A probe that overruns a whole
        interval skips the missed tick(s) rather than bursting to catch up.
        """
        loop = asyncio.get_running_loop()
        next_at = loop.time() + target.interval_secs
        try:
            while True:
                delay = next_at - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._safe_run(target, trigger="periodic")
                next_at += target.interval_secs
                now = loop.time()
                while next_at <= now:  # skip missed ticks if a probe overran
                    next_at += target.interval_secs
        except asyncio.CancelledError:
            return

    async def _safe_run(self, target: PollTarget, *, trigger: str) -> None:
        """Log exceptions instead of propagating so one bad probe
        doesn't kill the periodic task."""
        try:
            await self.checker.run(
                team_id=target.team_id,
                service=target.service,
                target=target.target,
                challenge=target.challenge,
                trigger=trigger,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "health probe raised for (%s, %s); continuing",
                target.team_id, target.service,
            )


def build_targets(
    *,
    challenges,
    teams_by_id,
    challenge_by_name,
    target_factory,
) -> list[PollTarget]:
    """Cross-product (challenges × teams) filtered to defense-capable
    challenges. Attack-only ones have no restart_handler and nothing
    the poller would usefully repeat beyond the kickoff probe.

    challenge_by_name(chcfg.name) -> Challenge — typically registry.build_challenge.
    target_factory(team_id, challenge) -> VulboxTarget — typically GameMgr.target_for.

    Poll interval is layered: the game.yaml override (`chcfg.health_interval_secs`)
    wins, else the challenge's intrinsic `health_interval_secs`, else
    `DEFAULT_INTERVAL_SECS`.
    """
    targets: list[PollTarget] = []
    for chcfg in challenges:
        challenge = challenge_by_name(chcfg.name)
        if challenge.restart_handler is None:
            continue
        interval = chcfg.health_interval_secs
        if interval is None:
            interval = challenge.health_interval_secs
        if interval is None:
            interval = DEFAULT_INTERVAL_SECS
        interval = float(interval)
        for team_id in chcfg.deploy_on:
            team = teams_by_id.get(team_id)
            if team is None or not team.prod:
                continue
            target = target_factory(team_id, challenge)
            targets.append(PollTarget(
                team_id=team_id,
                service=chcfg.name,
                challenge=challenge,
                target=target,
                interval_secs=interval,
            ))
    return targets
