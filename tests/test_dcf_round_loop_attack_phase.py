"""DEFCON: attack kickoff at ATTACK phase entry.

One agent per team per round (not per-team-per-service) because the
M5.6 attack prompt walks every opposing prod from a single install.

Post-bus-refactor: tests drive `game.enter_attack_phase(round_id)`
directly. The "doesn't fire outside ATTACK" guard is structurally
ensured (only enter_attack_phase records ATTACK_KICKOFF), so the
explicit test was dropped.
"""

from __future__ import annotations

import asyncio

import pytest

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType
from gamemgr.game_mgr import GameMgr
from gamemgr.round import Phase
from models.agent import AgentConfig
from models.topology import Team, TopologyGraph


class _StubOrchestrator:
    network = None


def _make_game(
    *,
    challenges: list[ChallengeConfig] | None = None,
    teams: list[Team] | None = None,
    agents: dict[str, AgentConfig] | None = None,
) -> GameMgr:
    if teams is None:
        teams = [
            Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-team1"),
            Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-team2"),
        ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=challenges
        or [ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1", "team2"])],
        rounds=RoundConfig(),
        agents=agents or {},
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def _attack_kickoffs(game: GameMgr) -> list:
    return [e for e in game.timeline.history() if e.type == EventType.ATTACK_KICKOFF.value]


# ── ATTACK_KICKOFF emission ───────────────────────────────────────────


def test_attack_kickoff_records_one_event_per_team():
    """One per team in topology — independent of how many challenges are deployed."""
    game = _make_game()
    game.enter_attack_phase(round_id=1)
    kickoffs = _attack_kickoffs(game)
    assert len(kickoffs) == 2
    assert sorted(e.team_id for e in kickoffs) == ["team1", "team2"]
    for e in kickoffs:
        assert e.round_id == 1
        assert e.phase == Phase.ATTACK.value


def test_attack_kickoff_is_idempotent_within_a_round():
    """Re-entering ATTACK in the same round MUST NOT double-spawn."""
    game = _make_game()
    game.enter_attack_phase(round_id=1)
    game.enter_attack_phase(round_id=1)
    game.enter_attack_phase(round_id=2)
    kickoffs = _attack_kickoffs(game)
    # 2 teams × 2 distinct rounds = 4 (round 1 dedup'd).
    assert len(kickoffs) == 4
    assert sorted((e.round_id, e.team_id) for e in kickoffs) == [
        (1, "team1"), (1, "team2"), (2, "team1"), (2, "team2"),
    ]


# ── Handler dispatch ──────────────────────────────────────────────────


def test_registered_attack_kickoff_handler_receives_attack_prompt():
    """Handler called once per team with an attack-mode AgentConfig.
    system_prompt fingerprints the attack recipe."""
    agents = {
        tid: AgentConfig(
            cli="claude --print", model="claude-sonnet-4-5",
            api_key="test-key", base_url="https://api.anthropic.com",
        )
        for tid in ("team1", "team2")
    }
    game = _make_game(agents=agents)
    calls: list[tuple[str, AgentConfig, int, str, bool]] = []

    async def handler(team_id, cfg, round_id, phase, is_first):
        calls.append((team_id, cfg, round_id, phase, is_first))

    game.register_attack_kickoff_handler(handler)

    async def run():
        tasks = game.enter_attack_phase(round_id=1)
        await asyncio.gather(*tasks)

    asyncio.run(run())

    assert len(calls) == 2
    by_team = {c[0]: c[1] for c in calls}
    assert set(by_team) == {"team1", "team2"}
    assert all(c[4] is True for c in calls), f"first kickoff per team must be is_first=True; got {calls}"
    assert all(c[2] == 1 and c[3] == "attack" for c in calls), calls
    for tid, cfg in by_team.items():
        # async-only: sync kickoff now delegates to the phase-agnostic L1+L2
        # briefing (no separate "attack agent" prompt); just fingerprint L1.
        assert "autonomous agent" in cfg.system_prompt.lower()
        assert "edit the source" not in cfg.system_prompt.lower()
        assert cfg.opposing_prod_hosts != tuple()


def test_handler_exception_does_not_break_subsequent_kickoffs():
    """A handler raising for team1 must not stop the team2 kickoff."""
    agents = {
        tid: AgentConfig(
            cli="claude --print", model="claude-sonnet-4-5",
            api_key="test-key", base_url="https://api.anthropic.com",
        )
        for tid in ("team1", "team2")
    }
    game = _make_game(agents=agents)
    invoked: list[str] = []

    async def handler(team_id, cfg, round_id, phase, is_first):
        invoked.append(team_id)
        if team_id == "team1":
            raise RuntimeError("simulated handler crash")

    game.register_attack_kickoff_handler(handler)

    async def run():
        tasks = game.enter_attack_phase(round_id=1)
        # Handlers swallow exceptions internally, so gather doesn't need
        # return_exceptions=True; but use it defensively.
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())
    assert sorted(invoked) == ["team1", "team2"]


def test_no_handler_registered_still_records_event():
    """Signal-only mode: observers see ATTACK_KICKOFF even without an agent wired."""
    game = _make_game()  # no handler registered
    game.enter_attack_phase(round_id=1)
    assert len(_attack_kickoffs(game)) == 2
