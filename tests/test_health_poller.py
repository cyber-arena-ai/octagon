"""HealthPoller unit tests.

Verify:
  - on_attack_enter fires one immediate probe per target
  - periodic loop runs at the configured interval
  - on_attack_exit cancels all periodic tasks
  - trigger_now runs one-shot for a specific (team, service)
  - non-defense-capable challenges have no target (trigger_now no-ops)

Stage 4 (challenge interface v2): PollTarget now carries a `Challenge`
+ a pre-built `VulboxTarget`; HealthChecker delegates to
`challenge.functionality_test.run(target)`.
"""

from __future__ import annotations

import asyncio

from challenges.interface import (
    Challenge,
    ChallengeDocs,
    CheckResult,
    FlagHandler,
    FlagObservation,
    FunctionalityTest,
    ObservationStatus,
    RestartHandler,
    VulboxImage,
    VulboxTarget,
)
from facility import FacilityContext, LocalEventSink, LocalStatusPort
from gamemgr.events import EventType
from gamemgr.timeline import Timeline
from health.checker import HealthChecker
from health.poller import DEFAULT_INTERVAL_SECS, HealthPoller, PollTarget, build_targets


# ── stub Challenge with a 4-leaf functionality test ────────────────────


class _StubFT(FunctionalityTest):
    @property
    def name(self) -> str:
        return "stub-ft"

    def run(self, target):
        return CheckResult(name="root", passed=True, children=[
            CheckResult(name="process", passed=True),
            CheckResult(name="tcp", passed=True),
            CheckResult(name="protocol", passed=True),
            CheckResult(name="checker", passed=True),
        ])


class _StubFH(FlagHandler):
    @property
    def name(self) -> str:
        return "stub-fh"

    def plant(self, target, flag):  # pragma: no cover — unused
        raise NotImplementedError

    def retrieve(self, target, handle, expected=None):
        return FlagObservation(ObservationStatus.NOT_FOUND)


class _StubRH(RestartHandler):
    @property
    def name(self) -> str:
        return "stub-rh"

    def run(self, target):  # pragma: no cover — unused
        raise NotImplementedError


class _DefenseChallenge(Challenge):
    """Defense-capable: has a restart_handler."""
    @property
    def name(self) -> str:
        return "nautilus-ico"

    @property
    def vulbox(self) -> VulboxImage:
        return VulboxImage(reference="x", exposed_ports={"service": 4265})

    @property
    def functionality_test(self) -> FunctionalityTest:
        return _StubFT()

    @property
    def flag_handler(self) -> FlagHandler:
        return _StubFH()

    @property
    def restart_handler(self) -> RestartHandler | None:
        return _StubRH()

    @property
    def docs(self) -> ChallengeDocs:
        return ChallengeDocs(intro="", agent_prompts={}, func_test_spec="")


class _AttackOnlyChallenge(Challenge):
    """Attack-only: restart_handler stays default (None)."""
    @property
    def name(self) -> str:
        return "notes"

    @property
    def vulbox(self) -> VulboxImage:
        return VulboxImage(reference="x", exposed_ports={"service": 5000})

    @property
    def functionality_test(self) -> FunctionalityTest:
        return _StubFT()

    @property
    def flag_handler(self) -> FlagHandler:
        return _StubFH()

    @property
    def docs(self) -> ChallengeDocs:
        return ChallengeDocs(intro="", agent_prompts={}, func_test_spec="")


def _checker(timeline: Timeline) -> HealthChecker:
    return HealthChecker(
        FacilityContext(
            status=LocalStatusPort(lambda: {"round": 1, "phase": "ATTACK"}),
            events=LocalEventSink(timeline),
        )
    )


def _checks(timeline: Timeline) -> list:
    return [e for e in timeline.history() if e.type == EventType.SERVICE_HEALTH_CHECK.value]


def _target(team: str, *, interval: float, challenge: Challenge | None = None) -> PollTarget:
    ch = challenge or _DefenseChallenge()
    vt = VulboxTarget(host=f"{team}_prod", ports={"service": 4265}, meta={"team_id": team})
    return PollTarget(
        team_id=team,
        service=ch.name,
        challenge=ch,
        target=vt,
        interval_secs=interval,
    )


# ── on_attack_enter ────────────────────────────────────────────────────


def test_on_attack_enter_fires_immediate_probe_per_target():
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [_target("team1", interval=100), _target("team2", interval=100)])

    async def run():
        immediate = poller.on_attack_enter(round_id=1)
        await asyncio.gather(*immediate)
        poller.on_attack_exit()

    asyncio.run(run())

    by_team = {}
    for e in _checks(tl):
        by_team.setdefault(e.team_id, []).append(e.payload["level"])
    assert sorted(by_team.keys()) == ["team1", "team2"]
    assert len(by_team["team1"]) == 4
    assert len(by_team["team2"]) == 4
    # The on-entry one-shot is tagged so it's distinguishable from the
    # periodic cadence on the same lane.
    assert all(e.payload.get("trigger") == "attack_enter" for e in _checks(tl))


def test_periodic_loop_runs_at_interval():
    """With interval=0.05s, ~3 ticks land in 0.2s. Allow slack — we just
    need to see more than one round of probes per target."""
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [_target("team1", interval=0.05)])

    async def run():
        immediate = poller.on_attack_enter(round_id=1)
        await asyncio.gather(*immediate)
        await asyncio.sleep(0.2)
        poller.on_attack_exit()

    asyncio.run(run())
    # immediate (4) + ≥2 periodic ticks × 4 leaves = ≥8 events.
    assert len(_checks(tl)) >= 8, f"expected ≥8 checks, got {len(_checks(tl))}"


def test_on_attack_exit_cancels_periodic_tasks():
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [_target("team1", interval=0.02)])

    async def run():
        immediate = poller.on_attack_enter(round_id=1)
        await asyncio.gather(*immediate)
        poller.on_attack_exit()
        baseline = len(_checks(tl))
        await asyncio.sleep(0.1)
        assert len(_checks(tl)) == baseline

    asyncio.run(run())


# ── trigger_now ────────────────────────────────────────────────────────


def test_trigger_now_runs_one_chain_for_known_target():
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [_target("team1", interval=100)])

    async def run():
        await poller.trigger_now("team1", "nautilus-ico")

    asyncio.run(run())
    assert len(_checks(tl)) == 4
    # Post-restart one-shot is tagged "restart".
    assert all(e.payload.get("trigger") == "restart" for e in _checks(tl))


def test_trigger_now_is_noop_for_unknown_target():
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [_target("team1", interval=100)])

    async def run():
        await poller.trigger_now("team-bogus", "nautilus-ico")
        await poller.trigger_now("team1", "not-a-service")

    asyncio.run(run())
    assert _checks(tl) == []


# ── build_targets cross-product + filter ──────────────────────────────


def _target_factory(team_id, ch):
    return VulboxTarget(host=f"{team_id}_prod", ports=dict(ch.vulbox.exposed_ports), meta={})


class _DefenseWithInterval(_DefenseChallenge):
    """Defense-capable with an intrinsic poll interval."""
    health_interval_secs = 30.0


def test_build_targets_filters_attack_only_challenges():
    """Challenges with restart_handler=None aren't pollable — periodic
    CHECKER probes would be wasted load on attack-only services."""
    from gamemgr.config import ChallengeConfig
    from models.topology import Team

    challenges = [
        ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1", "team2"]),
        ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico", port=4265,
            deploy_on=["team1", "team2"], health_interval_secs=5.0,
        ),
    ]
    teams_by_id = {
        "team1": Team(id="team1", dev="team1_dev", prod="team1_prod", token="t1"),
        "team2": Team(id="team2", dev="team2_dev", prod="team2_prod", token="t2"),
    }
    challenge_by_name = {
        "notes": _AttackOnlyChallenge(),
        "nautilus-ico": _DefenseChallenge(),
    }

    targets = build_targets(
        challenges=challenges,
        teams_by_id=teams_by_id,
        challenge_by_name=challenge_by_name.__getitem__,
        target_factory=_target_factory,
    )
    # notes filtered out; nautilus-ico × 2 teams = 2 targets.
    assert len(targets) == 2
    assert all(t.service == "nautilus-ico" for t in targets)
    assert sorted(t.team_id for t in targets) == ["team1", "team2"]
    assert all(t.interval_secs == 5.0 for t in targets)  # game.yaml override


def _one_target(challenge, *, interval=None):
    from gamemgr.config import ChallengeConfig
    from models.topology import Team

    challenges = [
        ChallengeConfig(
            name="nautilus-ico", type="nautilus-ico", port=4265,
            deploy_on=["team1"], health_interval_secs=interval,
        ),
    ]
    teams_by_id = {"team1": Team(id="team1", dev="d", prod="p", token="t")}
    targets = build_targets(
        challenges=challenges,
        teams_by_id=teams_by_id,
        challenge_by_name={"nautilus-ico": challenge}.__getitem__,
        target_factory=_target_factory,
    )
    assert len(targets) == 1
    return targets[0]


def test_build_targets_falls_back_to_default_interval():
    """No game.yaml override and no challenge default -> framework default."""
    t = _one_target(_DefenseChallenge())
    assert t.interval_secs == float(DEFAULT_INTERVAL_SECS)


def test_build_targets_uses_challenge_default_when_no_override():
    """No game.yaml override -> the challenge's intrinsic interval."""
    t = _one_target(_DefenseWithInterval())
    assert t.interval_secs == 30.0


def test_build_targets_yaml_override_beats_challenge_default():
    """game.yaml override wins over the challenge's intrinsic interval."""
    t = _one_target(_DefenseWithInterval(), interval=5.0)
    assert t.interval_secs == 5.0


def test_empty_targets_list_makes_poller_noop():
    """notes-only game (no defense-capable services) — poller doesn't crash."""
    tl = Timeline()
    poller = HealthPoller(_checker(tl), [])

    async def run():
        immediate = poller.on_attack_enter(round_id=1)
        assert immediate == []
        poller.on_attack_exit()
        await poller.stop()

    asyncio.run(run())
    assert _checks(tl) == []
