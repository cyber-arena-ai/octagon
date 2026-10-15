"""DEFCON: DEFENSE phase ends early once every defense-capable team has
reported DEPLOY_COMPLETE.

The legacy `_on_defense_end_health_check` subscriber was removed when
HealthPoller took over the end-of-defense probe; tests for that path
live in test_health_poller.py.
"""

from __future__ import annotations

import asyncio

import pytest

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.game_mgr import GameMgr
from gamemgr.round import Phase, RoundLoop
from models.topology import Team, TopologyGraph


class _StubNetwork:
    def set_cross_team_network(self, allow: bool) -> None: ...


class _StubOrchestrator:
    network = _StubNetwork()


def _make_game(*, defense_secs: int = 30, attack_secs: int = 5) -> GameMgr:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="t1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="t2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=[ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico", port=4265,
            deploy_on=["team1", "team2"],
        )],
        rounds=RoundConfig(defense_secs=defense_secs, attack_secs=attack_secs, total_rounds=1),
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


# ── record_deploy_complete drains pending; ends phase when empty ──────


def test_deploy_complete_drains_pending_and_ends_phase_when_empty():
    game = _make_game()
    # round_loop must exist so end_phase_early has somewhere to signal.
    game.round_loop = RoundLoop(game, game.orchestrator.network, game.config.rounds)
    game.round_loop.round = 1
    # status() reads phase from round_loop.phase; the drain+early-end
    # path is gated on phase=DEFENSE, so set it the way RoundLoop's
    # _enter_defense would.
    game.round_loop.phase = Phase.DEFENSE

    async def run():
        # Simulate DEFENSE kickoff for both teams in round 1.
        game.enter_defense_phase(round_id=1)
        assert game._defense_pending.get(1) == {("team1", "nautilus-ico"), ("team2", "nautilus-ico")}

        # team1 reports — still one pending.
        await game.record_deploy_complete("team1", "nautilus-ico")
        assert game._defense_pending[1] == {("team2", "nautilus-ico")}
        assert not game.round_loop._phase_early_end_event.is_set()

        # team2 reports — now empty, round loop signalled.
        await game.record_deploy_complete("team2", "nautilus-ico")
        assert game._defense_pending[1] == set()
        assert game.round_loop._phase_early_end_event.is_set()

    asyncio.run(run())


def test_one_team_complete_does_not_end_phase():
    """Only one of two teams reports — phase should NOT end early."""
    game = _make_game()
    game.round_loop = RoundLoop(game, game.orchestrator.network, game.config.rounds)
    game.round_loop.round = 1
    game.round_loop.phase = Phase.DEFENSE

    async def run():
        game.enter_defense_phase(round_id=1)
        await game.record_deploy_complete("team1", "nautilus-ico")
        assert not game.round_loop._phase_early_end_event.is_set()

    asyncio.run(run())


def test_deploy_complete_during_attack_does_not_signal_early_end():
    """REGRESSION: a late DEPLOY_COMPLETE that lands after ATTACK has
    started must NOT touch _defense_pending or signal end_phase_early —
    that would cut ATTACK short. Slow defense agents (whose claude
    flushed report_deploy_complete past the DEFENSE→ATTACK boundary)
    were observed doing exactly this in DEFCON live runs."""
    game = _make_game()
    game.round_loop = RoundLoop(game, game.orchestrator.network, game.config.rounds)
    game.round_loop.round = 1

    async def run():
        game.round_loop.phase = Phase.DEFENSE
        game.enter_defense_phase(round_id=1)
        baseline = dict(game._defense_pending)

        # Phase boundary — RoundLoop transitions to ATTACK.
        game.round_loop.phase = Phase.ATTACK

        # Late DEPLOY_COMPLETE for both teams. The events still record
        # on the timeline (for the operator/panel), but:
        #   - pending must NOT be drained
        #   - end_phase_early must NOT fire
        await game.record_deploy_complete("team1", "nautilus-ico")
        await game.record_deploy_complete("team2", "nautilus-ico")
        assert game._defense_pending == baseline, (
            "pending must be untouched when DEPLOY_COMPLETE arrives outside DEFENSE"
        )
        assert not game.round_loop._phase_early_end_event.is_set()

    asyncio.run(run())


def test_deploy_complete_for_non_defense_capable_service_is_silent():
    """DEPLOY_COMPLETE for a service that wasn't in _defense_pending (e.g.
    notes — no restart_cmd, so no defense kickoff fired) must not crash
    or spuriously end the phase."""
    teams = [Team(id="team1", dev="d1", prod="p1", token="t1"),
             Team(id="team2", dev="d2", prod="p2", token="t2")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1", "team2"])],
        rounds=RoundConfig(),
    )
    game = GameMgr(config=cfg, orchestrator=_StubOrchestrator())
    game.round_loop = RoundLoop(game, game.orchestrator.network, game.config.rounds)
    game.round_loop.round = 1
    game.round_loop.phase = Phase.DEFENSE

    async def run():
        game.enter_defense_phase(round_id=1)
        # notes isn't defense-capable, so _defense_pending should be empty.
        assert game._defense_pending.get(1, set()) == set()
        await game.record_deploy_complete("team1", "notes")
        # No early-end signal because nothing was ever pending.
        assert not game.round_loop._phase_early_end_event.is_set()

    asyncio.run(run())


# ── RoundLoop._wait wakes on the early-end event ──────────────────────


def test_round_loop_wait_wakes_on_end_phase_early():
    """_wait should return well before the timeout when end_phase_early()
    is called."""
    game = _make_game()
    loop = RoundLoop(game, _StubNetwork(), RoundConfig(defense_secs=100, attack_secs=100))

    async def run():
        import time as _t
        async def signal():
            await asyncio.sleep(0.1)
            loop.end_phase_early()
        task = asyncio.create_task(signal())
        t0 = _t.monotonic()
        await loop._wait(100)
        elapsed = _t.monotonic() - t0
        await task
        assert elapsed < 1.0, f"_wait took {elapsed:.2f}s; early-end signal missed"

    asyncio.run(run())


def test_enter_phase_clears_prior_early_end_signal():
    """An early-end signal in one phase must not collapse the next phase."""
    game = _make_game()
    loop = RoundLoop(game, _StubNetwork(), RoundConfig(defense_secs=0, attack_secs=0))
    loop.round = 1
    loop.end_phase_early()
    assert loop._phase_early_end_event.is_set()

    async def run():
        # _enter_defense / _enter_attack both clear the event at the top.
        await loop._enter_defense(duration=0)
        assert not loop._phase_early_end_event.is_set()
        loop.end_phase_early()
        await loop._enter_attack(duration=0)
        assert not loop._phase_early_end_event.is_set()

    asyncio.run(run())
