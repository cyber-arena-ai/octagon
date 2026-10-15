"""Pre-R1 dry-run readiness probe.

GameMgr.dry_run_all_agents runs one short claude invocation per team
asking it to call `mcp__cyberarena__report_ready`. Success is observed
on the bus via AGENT_READY → DRY_RUN_OK; timeout / handler crash →
DRY_RUN_FAILED.

These tests stub the handler with in-memory shapes (no docker, no
real claude); they verify the **coordination** logic, not the agent
invocation itself (that's covered by test_dcf_phase_invocation.py).
"""

from __future__ import annotations

import asyncio

import pytest

from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType
from gamemgr.game_mgr import DryRunAttemptOutcome, GameMgr
from models.agent import AgentConfig
from models.topology import Team, TopologyGraph


class _StubOrchestrator:
    network = None


def _make_game(agents: dict[str, AgentConfig] | None = None) -> GameMgr:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1"])],
        rounds=RoundConfig(),
        agents=agents or {},
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def _event_types_for_team(game: GameMgr, team_id: str) -> list[str]:
    return [e.type for e in game.timeline.history() if e.team_id == team_id]


# ── no handler registered ────────────────────────────────────────────


def test_dry_run_all_agents_noop_when_no_handler():
    """Without `register_dry_run_handler`, the method returns immediately
    and emits nothing — keeps test-mode `start()` runs cheap."""
    game = _make_game()
    asyncio.run(game.dry_run_all_agents())
    assert game.timeline.history() == []


# ── happy path: AGENT_READY arrives → DRY_RUN_OK ─────────────────────


def test_dry_run_emits_ok_when_handler_triggers_agent_ready():
    """Handler simulates the real agent by calling
    `game.record_agent_ready(team_id)` — that fires AGENT_READY on the
    bus, which the dry-run coordinator observes per team."""
    game = _make_game()

    async def handler(team_id: str) -> None:
        await game.record_agent_ready(team_id)

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    for team_id in ("team1", "team2"):
        types = _event_types_for_team(game, team_id)
        assert types[0] == EventType.DRY_RUN_START.value, types
        assert EventType.AGENT_READY.value in types
        assert types[-1] == EventType.DRY_RUN_OK.value, types
        assert EventType.DRY_RUN_FAILED.value not in types
        ok = next(
            e for e in game.timeline.history()
            if e.team_id == team_id and e.type == EventType.DRY_RUN_OK.value
        )
        assert ok.payload["attempts"] == 1
        assert ok.payload["duration_secs"] >= 0


# ── handler runs but never triggers AGENT_READY → DRY_RUN_FAILED ─────


def test_dry_run_emits_failed_when_no_agent_ready():
    """Handler returns cleanly but never causes AGENT_READY (e.g. agent
    crashed silently). Dry-run coordinator catches the missing signal
    and emits DRY_RUN_FAILED with a reason."""
    game = _make_game()

    invoked: list[str] = []
    async def handler(team_id: str) -> None:
        invoked.append(team_id)
        # No call to record_agent_ready — simulates broken setup.

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    assert sorted(invoked) == ["team1", "team2"]
    for team_id in ("team1", "team2"):
        failed = [
            e for e in game.timeline.history()
            if e.team_id == team_id and e.type == EventType.DRY_RUN_FAILED.value
        ]
        assert len(failed) == 1
        assert "AGENT_READY" in failed[0].payload.get("error", "")
        assert failed[0].payload["failure_class"] == "agent_ready_missing"


def test_dry_run_correlates_missing_ready_with_configured_proxy_403():
    game = _make_game()
    game.orchestrator.dev_setup_results = {
        "team1_dev": {
            "status": "configured_proxy_http_403",
            "proxy_configured": True,
            "duration_secs": 0.25,
            "rc": 100,
        },
        "team2_dev": {
            "status": "ok",
            "proxy_configured": True,
            "duration_secs": 0.2,
            "rc": 0,
        },
    }

    async def handler(_team_id: str) -> None:
        return None

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))
    failed = {
        e.team_id: e.payload
        for e in game.timeline.history()
        if e.type == EventType.DRY_RUN_FAILED.value
    }
    assert failed["team1"]["failure_class"] == "configured_proxy_http_403"
    assert failed["team2"]["failure_class"] == "agent_ready_missing"


def test_dsh_missing_ready_gets_bounded_retries_then_succeeds(monkeypatch):
    game = _make_game(agents={
        team: AgentConfig(cli="dsh", provider="deepseek")
        for team in ("team1", "team2")
    })
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> None:
        calls[team_id] += 1
        if calls[team_id] == 3:
            await game.record_agent_ready(team_id)

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))
    assert calls == {"team1": 3, "team2": 3}
    assert all(EventType.DRY_RUN_FAILED.value not in _event_types_for_team(game, t)
               for t in calls)


def test_dsh_nonzero_exit_is_retried_within_bound_then_classified(monkeypatch):
    game = _make_game(agents={
        team: AgentConfig(cli="dsh", provider="deepseek")
        for team in ("team1", "team2")
    })
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        calls[team_id] += 1
        return DryRunAttemptOutcome(
            exit_code=1,
            failure_class="agent_process_exit",
            retryable=True,
            startup_category=(
                "profile_patch_parsing"
                if calls[team_id] == 1
                else "filesystem_state"
            ),
            boot_stage=(
                "profile_overlay_resolution"
                if calls[team_id] == 1
                else "unknown"
            ),
            warmup_exit_class="nonzero_exit",
            error_token=(
                "profile_patch_parse_error"
                if calls[team_id] == 1
                else "path_not_found"
            ),
            stderr_size_bucket="1-127",
            stderr_lines_bucket="1",
        )

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    assert calls == {"team1": 3, "team2": 3}
    failed = [
        e for e in game.timeline.history()
        if e.type == EventType.DRY_RUN_FAILED.value
    ]
    assert len(failed) == 2
    assert all(e.payload["error"] == "agent command exited 1" for e in failed)
    assert all(e.payload["failure_class"] == "agent_process_exit" for e in failed)
    assert all(e.payload["exit_code"] == 1 for e in failed)
    assert all(e.payload["timed_out"] is False for e in failed)
    assert all(e.payload["attempts"] == 3 for e in failed)
    assert all(
        [d["attempt"] for d in e.payload["attempt_diagnostics"]] == [1, 2]
        for e in failed
    )
    assert all(
        [d["category"] for d in e.payload["attempt_diagnostics"]]
        == ["profile_patch_parsing", "filesystem_state"]
        for e in failed
    )
    assert all(
        [d["error_token"] for d in e.payload["attempt_diagnostics"]]
        == ["profile_patch_parse_error", "path_not_found"]
        for e in failed
    )
    assert all(
        [d["phase"] for d in e.payload["attempt_diagnostics"]]
        == ["dry_run", "dry_run"]
        for e in failed
    )
    assert all(
        [d["boot_stage"] for d in e.payload["attempt_diagnostics"]]
        == ["profile_overlay_resolution", "unknown"]
        for e in failed
    )
    assert all(
        [d["warmup_exit_class"] for d in e.payload["attempt_diagnostics"]]
        == ["nonzero_exit", "nonzero_exit"]
        for e in failed
    )
    assert all(
        all(len(d["diagnostic_digest"]) == 64 for d in e.payload["attempt_diagnostics"])
        for e in failed
    )


def test_dsh_late_ready_is_drained_before_false_failure():
    """An in-flight MCP report_ready may complete just after dsh exits.

    The bounded drain must observe that event and accept the attempt instead
    of starting retries / recording DRY_RUN_FAILED.
    """
    game = _make_game(agents={
        team: AgentConfig(cli="dsh", provider="deepseek")
        for team in ("team1", "team2")
    })

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        async def late_ready() -> None:
            await asyncio.sleep(0.01)
            await game.record_agent_ready(team_id)

        asyncio.create_task(late_ready())
        return DryRunAttemptOutcome(
            exit_code=1,
            failure_class="agent_process_exit",
            retryable=True,
        )

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    ok = [
        e for e in game.timeline.history()
        if e.type == EventType.DRY_RUN_OK.value
    ]
    assert len(ok) == 2
    assert all(e.payload["attempts"] == 1 for e in ok)
    assert not [
        e for e in game.timeline.history()
        if e.type == EventType.DRY_RUN_FAILED.value
    ]


def test_dsh_success_retains_bounded_ordered_attempt_diagnostics(monkeypatch):
    game = _make_game(agents={
        team: AgentConfig(cli="dsh", provider="deepseek")
        for team in ("team1", "team2")
    })
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        calls[team_id] += 1
        if calls[team_id] == 1:
            return DryRunAttemptOutcome(
                exit_code=1,
                failure_class="agent_process_exit",
                startup_category="binary_plugin_startup",
                error_token="plugin_load_error",
            )
        await game.record_agent_ready(team_id)
        return DryRunAttemptOutcome(
            exit_code=0,
            startup_category="clean_exit",
            error_token="clean_exit",
        )

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    ok = [
        e for e in game.timeline.history()
        if e.type == EventType.DRY_RUN_OK.value
    ]
    assert len(ok) == 2
    assert all(e.payload["attempts"] == 2 for e in ok)
    assert all(
        [d["category"] for d in e.payload["attempt_diagnostics"]]
        == ["binary_plugin_startup", "clean_exit"]
        for e in ok
    )
    assert all(len(e.payload["attempt_diagnostics"]) == 2 for e in ok)


def test_dsh_final_clean_exit_does_not_retain_prior_nonzero(monkeypatch):
    game = _make_game(agents={
        team: AgentConfig(cli="dsh", provider="deepseek")
        for team in ("team1", "team2")
    })
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        calls[team_id] += 1
        if calls[team_id] == 1:
            return DryRunAttemptOutcome(
                exit_code=7,
                failure_class="agent_process_exit",
            )
        return DryRunAttemptOutcome(exit_code=0)

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    failed = [
        e for e in game.timeline.history()
        if e.type == EventType.DRY_RUN_FAILED.value
    ]
    assert len(failed) == 2
    assert all(e.payload["failure_class"] == "agent_ready_missing" for e in failed)
    assert all(e.payload["error"] == "AGENT_READY not received" for e in failed)
    assert all(e.payload["exit_code"] == 0 for e in failed)


def test_dsh_typed_auth_failure_is_not_retried():
    game = _make_game(agents={
        team: AgentConfig(cli="dsh", provider="deepseek")
        for team in ("team1", "team2")
    })
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        calls[team_id] += 1
        return DryRunAttemptOutcome(
            exit_code=1,
            failure_class="auth_error",
            retryable=False,
            startup_category="auth",
            error_token="auth_error",
        )

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    assert calls == {"team1": 1, "team2": 1}
    failed = [
        e for e in game.timeline.history()
        if e.type == EventType.DRY_RUN_FAILED.value
    ]
    assert all(e.payload["failure_class"] == "auth_error" for e in failed)
    assert all(e.payload["attempts"] == 1 for e in failed)


def test_dsh_genuine_handler_error_is_not_retried():
    game = _make_game(agents={
        team: AgentConfig(cli="dsh", provider="deepseek")
        for team in ("team1", "team2")
    })
    # Even if setup also observed a configured-proxy 403, a genuine handler
    # authentication exception takes precedence and is never masked/retried.
    game.orchestrator.dev_setup_results = {
        f"{team}_dev": {"status": "configured_proxy_http_403"}
        for team in ("team1", "team2")
    }
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> None:
        calls[team_id] += 1
        raise PermissionError("auth rejected")

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))
    assert calls == {"team1": 1, "team2": 1}
    assert all(EventType.DRY_RUN_FAILED.value in _event_types_for_team(game, t)
               for t in calls)
    failed = [
        e for e in game.timeline.history()
        if e.type == EventType.DRY_RUN_FAILED.value
    ]
    assert all(e.payload["failure_class"] == "auth_error" for e in failed)
    assert all(e.payload["attempts"] == 1 for e in failed)


# ── scripted agent: probe skipped, DRY_RUN_OK without AGENT_READY ────


def test_dry_run_skips_probe_for_script_provider():
    """A `provider: script` agent has no LLM/MCP channel and never calls
    report_ready. The dry-run coordinator must short-circuit such teams to
    DRY_RUN_OK instead of letting the missing AGENT_READY surface as a
    misleading DRY_RUN_FAILED. team1 is scripted; team2 is a normal LLM
    team that still goes through the probe."""
    game = _make_game(agents={
        "team1": AgentConfig(cli="bash exploit.sh", provider="script"),
        "team2": AgentConfig(cli="claude", provider="claude"),
    })

    invoked: list[str] = []
    async def handler(team_id: str) -> None:
        invoked.append(team_id)
        await game.record_agent_ready(team_id)

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    # team1 (script): handler never invoked, DRY_RUN_OK, no AGENT_READY.
    assert "team1" not in invoked
    t1 = _event_types_for_team(game, "team1")
    assert t1 == [EventType.DRY_RUN_START.value, EventType.DRY_RUN_OK.value], t1
    assert EventType.AGENT_READY.value not in t1
    assert EventType.DRY_RUN_FAILED.value not in t1

    # team2 (claude): normal probe path still runs and succeeds.
    assert "team2" in invoked
    t2 = _event_types_for_team(game, "team2")
    assert EventType.AGENT_READY.value in t2
    assert t2[-1] == EventType.DRY_RUN_OK.value, t2


def test_dry_run_skips_probe_for_idle_provider():
    """A `provider: idle` agent — the solo-attack STATIC TARGET — also has no
    LLM/MCP channel and never calls report_ready, so it must be short-circuited
    to DRY_RUN_OK just like a scripted agent. Without this, every attack-solo
    match (whose target team is always idle) aborts at the pre-R1 dry-run."""
    game = _make_game(agents={
        "team1": AgentConfig(cli="claude", provider="claude"),
        "team2": AgentConfig(cli="idle", provider="idle"),   # the idle target
    })

    invoked: list[str] = []
    async def handler(team_id: str) -> None:
        invoked.append(team_id)
        await game.record_agent_ready(team_id)

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    # team2 (idle): handler never invoked, DRY_RUN_OK, no AGENT_READY, no FAIL.
    assert "team2" not in invoked
    t2 = _event_types_for_team(game, "team2")
    assert t2 == [EventType.DRY_RUN_START.value, EventType.DRY_RUN_OK.value], t2
    assert EventType.DRY_RUN_FAILED.value not in t2
    # team1 (claude): normal probe path still runs.
    assert "team1" in invoked
    assert _event_types_for_team(game, "team1")[-1] == EventType.DRY_RUN_OK.value


# ── handler raises → DRY_RUN_FAILED with safe exception type ─────────


def test_dry_run_emits_failed_when_handler_raises():
    game = _make_game()

    async def handler(team_id: str) -> None:
        raise RuntimeError(f"simulated crash for {team_id}")

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    for team_id in ("team1", "team2"):
        failed = [
            e for e in game.timeline.history()
            if e.team_id == team_id and e.type == EventType.DRY_RUN_FAILED.value
        ]
        assert len(failed) == 1
        err = failed[0].payload.get("error", "")
        assert err == "RuntimeError"
        assert failed[0].payload["failure_class"] == "handler_exception"


def test_dry_run_handler_exception_does_not_persist_secret(caplog):
    game = _make_game()
    secret = "credential-super-secret-value"

    async def handler(_team_id: str) -> None:
        raise RuntimeError(secret)

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    failed = [
        e
        for e in game.timeline.history()
        if e.type == EventType.DRY_RUN_FAILED.value
    ]
    assert len(failed) == 2
    assert all(e.payload["error"] == "RuntimeError" for e in failed)
    assert all(e.payload["failure_class"] == "handler_exception" for e in failed)
    assert secret not in repr([e.payload for e in failed])
    assert secret not in caplog.text


# ── handler hangs past timeout → DRY_RUN_FAILED ──────────────────────


def test_dry_run_emits_failed_when_handler_hangs():
    """If the agent never returns from the per-team handler within
    timeout_secs, dry-run coordinator gives up and emits FAILED. The
    underlying claude process should be killed by the runner's
    transport-level timeout (not exercised here — that's the runner's
    test surface)."""
    game = _make_game()

    async def handler(team_id: str) -> None:
        await asyncio.sleep(60)  # way past our timeout

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=0.05))

    for team_id in ("team1", "team2"):
        failed = [
            e for e in game.timeline.history()
            if e.team_id == team_id and e.type == EventType.DRY_RUN_FAILED.value
        ]
        assert len(failed) == 1
        assert "exceeded" in failed[0].payload.get("error", "").lower()
        assert failed[0].payload["failure_class"] == "timeout"


# ── isolation: one team failing doesn't break the other ─────────────


def test_dry_run_one_team_failing_does_not_block_other():
    game = _make_game()

    async def handler(team_id: str) -> None:
        if team_id == "team1":
            raise RuntimeError("simulated team1 crash")
        # team2: succeed normally.
        await game.record_agent_ready(team_id)

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))

    # team1 → FAILED
    assert any(
        e.type == EventType.DRY_RUN_FAILED.value and e.team_id == "team1"
        for e in game.timeline.history()
    )
    # team2 → OK
    assert any(
        e.type == EventType.DRY_RUN_OK.value and e.team_id == "team2"
        for e in game.timeline.history()
    )


# ── opencode: a timed-out probe (reasoning spiral) is retried, bounded ──
#
# hy4-preview (2026-09-19): the model sometimes deliberates the one-line probe
# for minutes and never reaches the tool call, so the attempt times out on a
# healthy agent. The coordinator retries a TIMED-OUT opencode attempt (up to 3),
# each with the full budget; refusals/auth stay non-retryable, and a harness
# without a retry policy (claude) still fails on the first timeout.


def _opencode_agents(model: str = "hy4-preview") -> dict[str, AgentConfig]:
    return {team: AgentConfig(cli="opencode run", provider="opencode", model=model)
            for team in ("team1", "team2")}


def _timed_out_outcome() -> DryRunAttemptOutcome:
    # What main._dry_run_attempt_outcome projects for a transport timeout.
    return DryRunAttemptOutcome(
        exit_code=-1, timed_out=True, failure_class="timeout", retryable=False,
        startup_category="timeout", error_token="process_timeout",
    )


def test_opencode_timed_out_probe_is_retried_then_succeeds(monkeypatch):
    game = _make_game(agents=_opencode_agents())
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        calls[team_id] += 1
        if calls[team_id] < 3:
            return _timed_out_outcome()
        await game.record_agent_ready(team_id)
        return DryRunAttemptOutcome(exit_code=0)

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))
    assert calls == {"team1": 3, "team2": 3}
    types = _event_types_for_team(game, "team1")
    assert EventType.DRY_RUN_FAILED.value not in types
    assert types.count(EventType.DRY_RUN_START.value) == 3
    retries = [e for e in game.timeline.history()
               if e.type == EventType.DRY_RUN_START.value and e.payload.get("retry")]
    assert retries and all(e.payload["reason"].startswith("opencode") for e in retries)
    ok = next(e for e in game.timeline.history()
              if e.type == EventType.DRY_RUN_OK.value and e.team_id == "team1")
    assert ok.payload["attempts"] == 3
    assert len(ok.payload["attempt_diagnostics"]) == 2   # retained, capped like dsh


def test_opencode_attempt_keeps_the_full_budget():
    """Unlike dsh (budget split three ways), each opencode attempt gets the
    whole coordinator budget: the transport-level kill must fire first."""
    game = _make_game(agents=_opencode_agents())
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        calls[team_id] += 1
        await asyncio.sleep(0.3)      # > 0.5/3 but < 0.5
        await game.record_agent_ready(team_id)
        return DryRunAttemptOutcome(exit_code=0)

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=0.5))
    assert calls == {"team1": 1, "team2": 1}
    assert EventType.DRY_RUN_FAILED.value not in _event_types_for_team(game, "team1")


def test_opencode_probe_retries_are_bounded_then_fail(monkeypatch):
    game = _make_game(agents=_opencode_agents())
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        calls[team_id] += 1
        return _timed_out_outcome()

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))
    assert calls == {"team1": 3, "team2": 3}
    failed = [e for e in game.timeline.history()
              if e.type == EventType.DRY_RUN_FAILED.value and e.team_id == "team1"]
    assert len(failed) == 1
    assert failed[0].payload["attempts"] == 3
    assert failed[0].payload["failure_class"] == "timeout"
    assert failed[0].payload["timed_out"] is True
    assert game._fatal_failure is not None


def test_opencode_refusal_is_not_retried(monkeypatch):
    game = _make_game(agents=_opencode_agents())
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        calls[team_id] += 1
        return DryRunAttemptOutcome(
            exit_code=0, failure_class="model_refusal", retryable=False,
            startup_category="refusal", error_token="model_refusal",
        )

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))
    assert calls == {"team1": 1, "team2": 1}


def test_non_hy4_opencode_timed_out_probe_is_not_retried(monkeypatch):
    """The opencode retry is scoped to NO_REASONING_PROBE_MODELS. Another
    opencode model (MiniMax) keeps single-attempt fail-loud — unchanged."""
    game = _make_game(agents=_opencode_agents(model="MiniMax-M3"))
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        calls[team_id] += 1
        return _timed_out_outcome()

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))
    assert calls == {"team1": 1, "team2": 1}
    failed = [e for e in game.timeline.history()
              if e.type == EventType.DRY_RUN_FAILED.value and e.team_id == "team1"]
    assert failed and failed[0].payload["attempts"] == 1


def test_claude_timed_out_probe_is_not_retried(monkeypatch):
    game = _make_game(agents={
        team: AgentConfig(cli="claude -p", provider="claude")
        for team in ("team1", "team2")
    })
    calls = {"team1": 0, "team2": 0}

    async def handler(team_id: str) -> DryRunAttemptOutcome:
        calls[team_id] += 1
        return _timed_out_outcome()

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=5))
    assert calls == {"team1": 1, "team2": 1}
    failed = [e for e in game.timeline.history()
              if e.type == EventType.DRY_RUN_FAILED.value and e.team_id == "team1"]
    assert failed and failed[0].payload["attempts"] == 1
