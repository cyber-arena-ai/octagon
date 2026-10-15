"""trigger_health_check + get_health_checks — the two agent-facing health
MCP tools at the GameMgr level (the MCP/backend layers are thin forwarders).

trigger_health_check kicks a one-shot functionality test on the team's own
defendable service(s) via the health poller (fire-and-forget); get_health_checks
reads the resulting SERVICE_HEALTH_CHECK leaf events back, regrouped by probe_id
into one entry per run, newest first.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from challenges.registry import build_challenge
from gamemgr.config import ChallengeConfig, GameConfig, HealthPollerConfig, RoundConfig
from gamemgr.events import EventType
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


class _StubOrchestrator:
    network = None


class _FakePoller:
    """Captures trigger_now calls (incl. the trigger tag) without probing."""

    name = "health"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def trigger_now(self, team_id, service, *, trigger="restart"):
        self.calls.append((team_id, service, trigger))


def _make_game(*, recent_default: int = 10) -> GameMgr:
    """team1 owns a defendable challenge (nautilus-ico) + an attack-only one
    (notes); team2 owns only the defendable one. Mirrors what
    deploy_challenges registers at setup (tests don't run setup)."""
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-team1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-team2"),
    ]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[
            ChallengeConfig(name="nautilus-ico", type="nautilus-ico", port=4265,
                            deploy_on=["team1", "team2"]),
            ChallengeConfig(name="notes", type="notes", port=5000,
                            deploy_on=["team1"]),
        ],
        rounds=RoundConfig(),
        health_poller=HealthPollerConfig(recent_results_default=recent_default),
    )
    game = GameMgr(config=cfg, orchestrator=_StubOrchestrator())
    for chcfg in cfg.challenges:
        ch = build_challenge(chcfg.name)
        for tid in chcfg.deploy_on:
            game._challenge_by_owner[(tid, chcfg.name)] = ch
    return game


_FOUR_LEVEL = [("process", True), ("tcp", True), ("protocol", True), ("checker", True)]


def _record_run(game, *, team_id, service="nautilus-ico", levels=_FOUR_LEVEL,
                trigger="periodic", round_id=1) -> str:
    """Mimic HealthChecker.run: one probe_id shared by every leaf event."""
    pid = uuid.uuid4().hex[:12]
    for name, passed in levels:
        game.timeline.record(
            EventType.SERVICE_HEALTH_CHECK,
            team_id=team_id,
            round_id=round_id,
            phase="ATTACK",
            payload={"team_id": team_id, "service": service, "probe_id": pid,
                     "level": name, "passed": passed, "detail": "", "trigger": trigger},
        )
    return pid


# ── get_health_checks ────────────────────────────────────────────────────


def test_get_health_checks_groups_leaves_into_runs_newest_first():
    game = _make_game()
    _record_run(game, team_id="team1", trigger="periodic")
    _record_run(game, team_id="team1", trigger="manual")
    _record_run(game, team_id="team2")  # other team — must be excluded

    res = game.get_health_checks("team1")
    assert res["team_id"] == "team1"
    assert res["count"] == 2
    # Newest first: the manual run was recorded last.
    assert res["checks"][0]["trigger"] == "manual"
    assert res["checks"][1]["trigger"] == "periodic"
    # Each run rolls its 4 leaves back up, all passing.
    first = res["checks"][0]
    assert [lv["level"] for lv in first["levels"]] == ["process", "tcp", "protocol", "checker"]
    assert first["passed"] is True


def test_get_health_checks_overall_passed_is_and_of_levels():
    game = _make_game()
    _record_run(game, team_id="team1",
                levels=[("process", True), ("tcp", True),
                        ("protocol", True), ("checker", False)])
    res = game.get_health_checks("team1")
    run = res["checks"][0]
    assert run["passed"] is False  # one failing level fails the whole run
    assert {lv["level"]: lv["passed"] for lv in run["levels"]}["checker"] is False


def test_get_health_checks_default_and_explicit_limit():
    game = _make_game(recent_default=2)
    for _ in range(5):
        _record_run(game, team_id="team1")

    assert game.get_health_checks("team1")["count"] == 2          # yaml default
    assert game.get_health_checks("team1", limit=4)["count"] == 4  # explicit
    # A huge limit is clamped to the server cap but never exceeds what exists.
    assert game.get_health_checks("team1", limit=10_000)["count"] == 5


def test_get_health_checks_empty_when_none_recorded():
    game = _make_game()
    res = game.get_health_checks("team1")
    assert res == {"team_id": "team1", "count": 0, "checks": []}


def test_get_health_checks_legacy_events_without_probe_id():
    """Events recorded before probe_id existed each become their own run."""
    game = _make_game()
    for level in ("process", "tcp"):
        game.timeline.record(
            EventType.SERVICE_HEALTH_CHECK, team_id="team1", round_id=1, phase="ATTACK",
            payload={"team_id": "team1", "service": "nautilus-ico",
                     "level": level, "passed": True, "detail": "", "trigger": "periodic"},
        )
    res = game.get_health_checks("team1")
    assert res["count"] == 2  # no probe_id → one run per leaf


# ── trigger_health_check ───────────────────────────────────────────────────


def test_trigger_all_defendable_services_tagged_manual():
    game = _make_game()
    poller = _FakePoller()
    game.health_poller = poller

    async def scenario():
        res = await game.trigger_health_check("team1")
        await game._drain_kickoffs()   # fire-and-forget: probe runs off the request path
        return res
    res = asyncio.run(scenario())
    # notes is attack-only (no restart_handler) → excluded.
    assert res == {"triggered": ["nautilus-ico"]}
    assert poller.calls == [("team1", "nautilus-ico", "manual")]


def test_trigger_specific_service_ok():
    game = _make_game()
    poller = _FakePoller()
    game.health_poller = poller

    async def scenario():
        res = await game.trigger_health_check("team1", "nautilus-ico")
        await game._drain_kickoffs()
        return res
    res = asyncio.run(scenario())
    assert res == {"triggered": ["nautilus-ico"]}
    assert poller.calls == [("team1", "nautilus-ico", "manual")]


def test_trigger_is_fire_and_forget_and_tracked():
    """The agent's call returns immediately — the probe runs OFF the request path
    (registered in the kickoff set so stop() drains it), not awaited inline."""
    game = _make_game()

    class _BlockingPoller:
        name = "health"
        def __init__(self):
            self.calls: list = []
            self.release: asyncio.Event | None = None
        async def trigger_now(self, team_id, service, *, trigger="restart"):
            self.calls.append((team_id, service, trigger))
            await self.release.wait()   # simulate a slow probe (SSH+http)

    poller = _BlockingPoller()
    game.health_poller = poller

    async def scenario():
        poller.release = asyncio.Event()
        res = await game.trigger_health_check("team1")   # must NOT block on the probe
        assert res == {"triggered": ["nautilus-ico"]}
        # returned while the probe is still in flight, tracked in the kickoff set
        assert len([t for t in game._kickoff_tasks if not t.done()]) == 1
        poller.release.set()
        await game._drain_kickoffs()
        assert poller.calls == [("team1", "nautilus-ico", "manual")]
    asyncio.run(scenario())


def test_trigger_probe_failure_is_logged_not_swallowed(caplog):
    """Fail loud: a probe/POST failure in the detached task is logged, never
    silently dropped — and it doesn't take down the agent's tool call."""
    game = _make_game()

    class _BoomPoller:
        name = "health"
        async def trigger_now(self, team_id, service, *, trigger="restart"):
            raise RuntimeError("health-poller unreachable")

    game.health_poller = _BoomPoller()

    async def scenario():
        res = await game.trigger_health_check("team1")   # must not raise
        await game._drain_kickoffs()
        return res
    with caplog.at_level("ERROR"):
        res = asyncio.run(scenario())
    assert res == {"triggered": ["nautilus-ico"]}
    assert "manual health-check probe failed" in caplog.text


def test_trigger_attack_only_service_raises():
    game = _make_game()
    game.health_poller = _FakePoller()
    with pytest.raises(ValueError, match="non-defendable"):
        asyncio.run(game.trigger_health_check("team1", "notes"))


def test_trigger_unknown_service_raises():
    game = _make_game()
    game.health_poller = _FakePoller()
    with pytest.raises(ValueError, match="non-defendable"):
        asyncio.run(game.trigger_health_check("team1", "ghost"))


def test_trigger_team_with_no_defendable_service_raises():
    game = _make_game()
    game.health_poller = _FakePoller()
    # team1 keeps only its attack-only challenge.
    game._challenge_by_owner = {
        k: v for k, v in game._challenge_by_owner.items()
        if not (k[0] == "team1" and k[1] == "nautilus-ico")
    }
    with pytest.raises(ValueError, match="no defendable service"):
        asyncio.run(game.trigger_health_check("team1"))
