"""Round state machine.

INIT -> PREFLIGHT -> SETUP -> DEFENSE <-> ATTACK -> STOPPED
                                  ↑          ↓
                                  └── round+=1

RoundLoop drives phase transitions but no longer publishes to an event
bus. Instead it calls back into GameMgr via direct methods
(`on_round_start`, `enter_defense_phase`, `enter_attack_phase`,
`on_round_stop`, `on_game_end`) which decide what to record on the
timeline and which side-effects to spawn.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import TYPE_CHECKING

from .config import RoundConfig

if TYPE_CHECKING:
    from .game_mgr import GameMgr

log = logging.getLogger(__name__)


class Phase(str, Enum):
    INIT = "INIT"
    PREFLIGHT = "PREFLIGHT"
    SETUP = "SETUP"
    DEFENSE = "DEFENSE"
    ATTACK = "ATTACK"
    # Async mode: attack + defense happen simultaneously (no DEFENSE/ATTACK
    # split). The game sits at LIVE for its whole duration; AsyncLoop only
    # ticks flag rotations underneath. STOPPED at end, same as sync.
    LIVE = "LIVE"
    STOPPED = "STOPPED"


class RoundLoop:
    def __init__(self, game: "GameMgr", network, config: RoundConfig) -> None:
        self.game = game
        self.network = network        # orchestrator.infra.network.NetworkManager
        self.config = config
        self.phase: Phase = Phase.INIT
        self.round: int = 0
        self._stop_event = asyncio.Event()
        # Phase-scoped early-end signal — set by GameMgr when all teams
        # have submitted DEPLOY_COMPLETE for the current DEFENSE phase
        # (later: same for ATTACK if symmetric early-end is wanted).
        # Reset at the start of every phase so the signal applies to one
        # phase only.
        self._phase_early_end_event = asyncio.Event()

    async def run(self) -> None:
        # Pre-loop fail-loud (rules B/D): the match was declared INVALID before
        # the round loop could start (short flag-plant / a team failed the
        # pre-R1 dry-run). fail_match() ran while round_loop was still None, so
        # it couldn't stop us via _stop_event — check the latch here and bail,
        # recording a failed GAME_END WITHOUT spawning any phase agents. Same
        # contract as AsyncLoop.run(); mode-independent so sync runs abort too.
        if self.game._fatal_failure is not None:
            log.error(
                "round loop: match already failed pre-loop (%s); recording a "
                "failed end without starting agents", self.game._fatal_failure,
            )
            self.phase = Phase.STOPPED
            self.game._game_over.set()
            try:
                self.game.on_game_end(
                    0, Phase.STOPPED.value,
                    {"reason": "failed", "fatal": self.game._fatal_failure,
                     "final_round": 0, "exited_from_phase": Phase.INIT.value,
                     "total_rounds": self.config.total_rounds},
                )
            except Exception:
                log.exception("on_game_end failed on the pre-loop fail path")
            return

        log.info(
            "round loop start: total_rounds=%d defense=%ds attack=%ds",
            self.config.total_rounds, self.config.defense_secs, self.config.attack_secs,
        )
        # Reason tracking for GAME_END payload. "completed" = ran the full
        # total_rounds; "stopped" = stop() was called mid-loop; "error" =
        # an unhandled exception bubbled out of the body.
        reason = "completed"
        try:
            while not self._stop_event.is_set() and self.round < self.config.total_rounds:
                self.round += 1
                self.game.on_round_start(self.round, self.phase.value)

                await self._enter_defense(self.config.defense_secs)
                if self._stop_event.is_set():
                    reason = "stopped"
                    break

                await self._enter_attack(self.config.attack_secs)

                await self.game.on_round_stop(self.round, self.phase.value)
            else:
                # while-loop fell through because total_rounds reached or
                # stop_event was set at the top of an iteration.
                if self._stop_event.is_set():
                    reason = "stopped"
        except Exception:
            reason = "error"
            raise
        finally:
            final_round = self.round
            final_phase = self.phase.value
            self.phase = Phase.STOPPED
            payload = {
                "reason": reason,
                "final_round": final_round,
                "exited_from_phase": final_phase,
                "total_rounds": self.config.total_rounds,
            }
            # Fail-loud (rules C/E): a mid-loop fail_match() halts via _stop_event
            # (same path as an operator stop) but the verdict is FAILED, not a
            # clean stop — reflect it in GAME_END so the artifact isn't a fake
            # `completed`/`stopped`.
            if self.game._fatal_failure is not None:
                payload["reason"] = "failed"
                payload["fatal"] = self.game._fatal_failure
            log.info(
                "round loop stopped at round=%d phase=%s reason=%s",
                final_round, final_phase, payload["reason"],
            )
            # GAME_END fires from inside `finally` so it lands even on
            # exception. Best-effort: if the timeline / game is already
            # torn down, swallow it — the game is exiting anyway.
            try:
                self.game.on_game_end(
                    final_round,
                    Phase.STOPPED.value,
                    payload,
                )
            except Exception:
                log.exception("on_game_end failed; loop already exiting")

    async def _enter_defense(self, duration: int) -> None:
        self.phase = Phase.DEFENSE
        self._phase_early_end_event.clear()
        self.network.set_cross_team_network(False)
        # Plant off-loop first. rotate_flags is a BLOCKING POST whose flag-service
        # callback (FLAG_PLANTED) reenters GameMgr's HTTP server on THIS loop; run
        # inline it would stall the loop, so the callback can't be served until
        # rotate returns — it waits out its 10s timeout (×N teams). Doing the real
        # plant in an executor keeps the loop responsive. enter_defense_phase still
        # calls rotate_flags (preserving the rotate→spawn contract), but it's
        # idempotent per round, so that second call is a cheap no-op at the
        # flag-service (no re-plant, no callback) and doesn't block.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.game.rotate_flags, self.round)
        self.game.enter_defense_phase(self.round)
        await self._wait(duration)

    async def _enter_attack(self, duration: int) -> None:
        self.phase = Phase.ATTACK
        self._phase_early_end_event.clear()
        self.network.set_cross_team_network(True)
        self.game.enter_attack_phase(self.round)
        await self._wait(duration)

    async def _wait(self, secs: int) -> None:
        """Wait up to `secs` seconds, but wake early if either:
          - `_stop_event` fires (game shutdown)
          - `_phase_early_end_event` fires (e.g. all teams done with DEFENSE)
        """
        stop_task = asyncio.create_task(self._stop_event.wait())
        early_task = asyncio.create_task(self._phase_early_end_event.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_task, early_task},
                timeout=secs,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, early_task):
                if not t.done():
                    t.cancel()

    def end_phase_early(self) -> None:
        """Cut the current phase's wait short. GameMgr calls this when
        every team has reported DEPLOY_COMPLETE for the current DEFENSE
        round, so the game can move on to ATTACK without burning the
        rest of the timer.
        """
        self._phase_early_end_event.set()

    def stop(self) -> None:
        self._stop_event.set()
