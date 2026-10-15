"""Async-mode game driver.

Parallel to `RoundLoop`, selected when `round_config.mode == "async"`.

Where `RoundLoop` runs a strict DEFENSE↔ATTACK state machine (toggling the
cross-team network each phase and spawning a fresh agent per phase), `AsyncLoop`
runs ONE continuous game at `Phase.LIVE`:

  - cross-team networks are connected ONCE at start and stay up the whole game
    (attack + defense happen simultaneously — no protected patch window),
  - one long-lived supervised agent per team is started once (the supervisor
    lives in main.py; AsyncLoop just kicks it via `game.start_async_agents()`),
  - health polling runs continuously (`on_attack_enter` once, never exited until
    game end),
  - a "round" tick rotates objective evidence every `round_secs` and briefly
    uses SETUP as a non-accepting transition; scoring is otherwise live
    continuously.

AsyncLoop deliberately does NOT call `enter_defense_phase` / `enter_attack_phase`
— those carry the sync per-phase kickoff + network toggle semantics.

It mirrors the small surface `GameMgr.status()` / `stop()` read off the active
driver (`.phase`, `.round`, `.stop()`, `.end_phase_early()`), so GameMgr can hold
either loop in `self.round_loop` ("the active driver") without branching.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import RoundConfig
from .round import Phase

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .game_mgr import GameMgr

log = logging.getLogger(__name__)


class AsyncLoop:
    def __init__(self, game: "GameMgr", network, config: RoundConfig) -> None:
        self.game = game
        self.network = network        # orchestrator.infra.network.NetworkManager
        self.config = config
        self.phase: Phase = Phase.INIT
        self.round: int = 0
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        # Pre-loop fail-loud (rules B/D): the match was declared INVALID before
        # the round loop could start — the flag-service planted fewer flags than
        # expected, or a team failed the pre-R1 dry-run. Record a failed GAME_END
        # and stop WITHOUT connecting the attack network or starting agents:
        # there is no game to play, and we must not burn a real match (or LLM
        # spend) on unwinnable/uncontested noise.
        if self.game._fatal_failure is not None:
            log.error(
                "async loop: match already failed pre-loop (%s); recording a "
                "failed end without starting agents", self.game._fatal_failure,
            )
            self.phase = Phase.STOPPED
            self.game._game_over.set()
            try:
                self.game.on_game_end(
                    0, Phase.STOPPED.value,
                    {"reason": "failed", "fatal": self.game._fatal_failure,
                     "final_round": 0, "exited_from_phase": Phase.INIT.value,
                     "total_rounds": self.config.total_rounds,
                     "mode": self.config.mode},
                )
            except Exception:
                log.exception("on_game_end failed on the pre-loop fail path")
            return

        log.info(
            "async loop start: total_rounds=%d round_secs=%ds (networks always-on)",
            self.config.total_rounds, self.config.round_secs,
        )
        reason = "completed"
        try:
            # Networks ON for the whole game — attack and defense happen
            # simultaneously. Toggled once here, never flipped per round. Solo
            # runs are a normal 2-team async game (they declare `attack_net`), so
            # this connects the two teams' prods exactly as async does.
            self.network.set_cross_team_network(True)

            # Round tick = evidence-rotation interval. Scoring/health run the
            # whole time; the only per-round side effect is rotate + a
            # periodic service-log snapshot.
            while not self._stop_event.is_set() and self.round < self.config.total_rounds:
                next_round = self.round + 1
                # Rotation commits each victim independently, so the whole
                # interval is NON-ACCEPTING: SETUP is not in the capture path's
                # {ATTACK, LIVE} gate, so no evidence can be credited to a round
                # that is only half published.
                self.phase = Phase.SETUP
                # rotate_flags is a BLOCKING HTTP POST to the flag-service, which
                # plants over SSH and then calls back into GameMgr's HTTP server
                # (same event loop) to record FLAG_PLANTED. Running it inline would
                # block the loop, so the callbacks can't be served until rotate
                # returns — they stall to their 10s timeout (×N teams) and only
                # then land. Run it off-loop (as the round-0 plant already does in
                # main.py) so GameMgr stays responsive and FLAG_PLANTED is recorded
                # the instant each flag lands.
                await self._run_blocking(self.game.rotate_flags, next_round)

                # A FLAG_PLANT_FAILED callback may stop the match while the
                # blocking rotate is still returning. Do not publish a round or
                # start agents after that failure.
                if self._stop_event.is_set() or self.game._fatal_failure is not None:
                    reason = "stopped"
                    break

                self.round = next_round
                self.game._async_round_started_ts = time.time()
                self.phase = Phase.LIVE
                self.game.on_round_start(self.round, Phase.LIVE.value)

                if self.round == 1:
                    # Continuous health polling + the long-lived supervised
                    # agents start only once round 1's context is fully rotated
                    # and published. Both stay on for the rest of the match —
                    # never on_attack_exit until teardown, which is what makes
                    # polling continuous.
                    if self.game.health_poller is not None:
                        self.game.health_poller.on_attack_enter(0)
                    self.game.start_async_agents()

                await self._wait(self.config.round_secs)
                if self.game.artifacts is not None:
                    await self.game._persist_service_logs(self.round)
                # Audit this round's flags (read-only) before the next tick
                # rotates them away. Off-loop (blocking SSH reads), like rotate.
                await self._run_blocking(self.game.audit_flags, self.round)
            else:
                if self._stop_event.is_set():
                    reason = "stopped"
        except Exception:
            reason = "error"
            raise
        finally:
            final_round = self.round
            exited_from_phase = self.phase.value
            self.phase = Phase.STOPPED
            # Release any supervisor blocked on is_game_over() so it stops
            # respawning the agent.
            self.game._game_over.set()
            # Fail-loud (rules C/E): a mid-loop fail_match() halts the loop via
            # _stop_event — the same path as an operator stop() — but the verdict
            # is FAILED, not a clean stop. Reflect that in GAME_END so the
            # artifact isn't a fake `stopped`/`completed`.
            payload = {
                "reason": reason,
                "final_round": final_round,
                "exited_from_phase": exited_from_phase,
                "total_rounds": self.config.total_rounds,
                "mode": self.config.mode,
            }
            if self.game._fatal_failure is not None:
                payload["reason"] = "failed"
                payload["fatal"] = self.game._fatal_failure
            log.info(
                "async loop stopped at round=%d reason=%s",
                final_round, payload["reason"],
            )
            try:
                self.game.on_game_end(
                    final_round,
                    Phase.STOPPED.value,
                    payload,
                )
            except Exception:
                log.exception("on_game_end failed; loop already exiting")

    async def _wait(self, secs: int) -> None:
        """Wait up to `secs`, waking early if `_stop_event` fires (shutdown).

        Mirrors RoundLoop._wait minus the phase-early-end signal (async has
        no per-phase early end — rounds are fixed flag-rotation intervals).
        """
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait({stop_task}, timeout=secs, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not stop_task.done():
                stop_task.cancel()

    @staticmethod
    async def _run_blocking(fn, *args) -> None:
        """Run one blocking facility call off-loop; split out so loop tests can
        stub it and stay offline."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, fn, *args)

    def end_phase_early(self) -> None:
        """No-op: async has no per-phase early end. Present so GameMgr's
        late-DEPLOY_COMPLETE path (guarded to DEFENSE, never reached at LIVE)
        can call it on the active driver without an AttributeError."""

    def stop(self) -> None:
        self._stop_event.set()
        # Mirror RoundLoop.stop()'s effect on the rest of the system: trip
        # the game-over gate so the agent supervisors stop respawning even
        # before the loop's finally block runs.
        self.game._game_over.set()
