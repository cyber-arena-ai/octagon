"""Fail-loud rule (A–E): an A/D match is valid only while BOTH agents are alive
AND flags were planted; the moment that stops being true the match is terminated
and the job is FAILED (non-zero orche exit), never a fake `succeeded`.

Offline — no Docker, no real agents. Drives the in-process GameMgr primitive
(`fail_match` / verdict / flag counts), the async supervisor (`_run_async_agent`)
for the give-up (C) and 401-fast-fail (E) paths, `dry_run_all_agents` (D), and
`AsyncLoop.run` for the pre-loop bail and the failed-reason override.
Mirrors the fakes in test_dcf_async_mode.py / test_dcf_dry_run.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import main
from challenges.interface import FlagStoreSpec
from gamemgr.async_loop import AsyncLoop
from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.events import EventType
from gamemgr.game_mgr import GameMgr
from gamemgr.round import Phase
from models.agent import AgentConfig
from models.topology import Team, TopologyGraph


def test_initial_flag_service_failure_becomes_a_match_failure(monkeypatch):
    """A round-0 configure/rotate error must reach the normal failed-match
    teardown path instead of escaping ``_run_start`` after containers are up."""

    class _Game:
        fatal: str | None = None

        def plant_initial_flags(self):
            raise RuntimeError("flag service down")

        def fail_match(self, reason: str):
            self.fatal = reason

    def _run_now(loop, _executor, func, *args):
        future = loop.create_future()
        try:
            future.set_result(func(*args))
        except Exception as exc:  # noqa: BLE001 — mirror the executor future
            future.set_exception(exc)
        return future

    # Exercise our exception conversion without depending on the interpreter's
    # thread-pool shutdown behavior.
    monkeypatch.setattr(asyncio.BaseEventLoop, "run_in_executor", _run_now)
    game = _Game()
    asyncio.run(main._plant_initial_flags_or_fail(game))
    assert game.fatal is not None
    assert "flag-service configure/rotation failed" in game.fatal


def _stub_ch(stores: tuple[str, ...] = ("default",)):
    """Placeholder challenge for _challenge_by_owner — only needs the
    authoritative flag_store_specs() for expected_flag_count()."""
    return SimpleNamespace(
        flag_store_specs=lambda: [FlagStoreSpec(name) for name in stores])


class _StubOrchestrator:
    network = None


class _StubNetwork:
    def __init__(self) -> None:
        self.cross_team_calls: list[bool] = []

    def set_cross_team_network(self, on: bool) -> None:
        self.cross_team_calls.append(on)


def _make_game(*, mode: str = "async", round_secs: int = 0, total_rounds: int = 3) -> GameMgr:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="tok-team1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="tok-team2"),
    ]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=[],
        rounds=RoundConfig(mode=mode, round_secs=round_secs, total_rounds=total_rounds),
        agents={},
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


class _R:
    """Fake CommandResult with controllable stderr/stdout (bytes)."""
    def __init__(self, *, exit_code=1, stderr=b"", stdout=b"", timed_out=False):
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout
        self.timed_out = timed_out
        self.pkilled_at = None


class _ScriptedMgr:
    """run_phase returns a queued result (or the last one forever). Counts calls."""
    def __init__(self, results: list[_R]):
        self._results = results
        self.calls = 0

    async def run_phase(self, backend, handle, prompt, *, timeout, model=None, **_):
        r = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return r


def _cfg() -> AgentConfig:
    return AgentConfig(cli="claude --print", model="m", api_key="k", base_url="u",
                       system_prompt="briefing")


def _events(game, etype) -> list:
    return [e for e in game.timeline.history() if e.type == etype.value]


# ── the primitive ───────────────────────────────────────────────────────────

def test_fail_match_is_idempotent_first_reason_wins():
    game = _make_game()
    assert game.match_failed is False
    game.fail_match("root cause")
    game.fail_match("downstream noise after the halt")
    assert game.match_failed is True
    assert game.fatal_reason == "root cause"            # first reason wins
    assert game._game_over.is_set()                      # supervisors released
    mf = _events(game, EventType.MATCH_FAILED)
    assert len(mf) == 1                                  # recorded exactly once
    assert mf[0].payload["reason"] == "root cause"


def test_expected_and_planted_flag_counts():
    game = _make_game()
    game._challenge_by_owner = {("team1", "svc"): _stub_ch(), ("team2", "svc"): _stub_ch()}
    assert game.expected_flag_count() == 2
    assert game.count_planted_flags() == 0
    game.timeline.record(EventType.FLAG_PLANTED, team_id="team1", round_id=0, payload={"service": "svc"})
    game.timeline.record(EventType.FLAG_PLANTED, team_id="team2", round_id=0, payload={"service": "svc"})
    game.timeline.record(EventType.FLAG_PLANTED, team_id="team1", round_id=1, payload={"service": "svc"})
    assert game.count_planted_flags(round_id=0) == 2
    assert game.count_planted_flags() == 3


def test_short_flag_plant_fails_match():
    """Rule B: expected 2, only 1 planted at round 0 → match invalid."""
    game = _make_game()
    game._challenge_by_owner = {("team1", "svc"): _stub_ch(), ("team2", "svc"): _stub_ch()}
    game.timeline.record(EventType.FLAG_PLANTED, team_id="team1", round_id=0, payload={"service": "svc"})
    if game.count_planted_flags(round_id=0) < game.expected_flag_count():
        game.fail_match("short plant")
    assert game.match_failed is True


def test_mid_game_flag_plant_failure_fails_the_match():
    """Per-rotation gate: a FLAG_PLANT_FAILED at a LATER round (an agent broke its
    own planter, so its flag stops being planted mid-game → the opponent has
    nothing to steal) must fail the match, not silently continue to `succeeded`.
    The flag-service emits the event; `ingest_event` acts on it, both drivers."""
    game = _make_game()
    assert game.match_failed is False
    recorded = game.ingest_event(
        event_id="fp-r3", type_=EventType.FLAG_PLANT_FAILED.value,
        team_id="team2", round_id=3, payload={"service": "rceaas"},
    )
    assert recorded is True
    assert game.match_failed is True


def test_flag_planted_event_does_NOT_fail_the_match():
    """A normal FLAG_PLANTED must never trip the plant gate."""
    game = _make_game()
    game.ingest_event(event_id="ok-r3", type_=EventType.FLAG_PLANTED.value,
                      team_id="team1", round_id=3, payload={"service": "svc"})
    assert game.match_failed is False


# ── C: give-up halts the whole match ─────────────────────────────────────────

def test_giveup_fails_the_match(monkeypatch):
    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    game = _make_game()
    mgr = _ScriptedMgr([_R(exit_code=1)])   # plain fast exit, no auth marker
    asyncio.run(main._run_async_agent(game, backend=None, mgr=mgr,
                                      handle=None, team_id="team1", cfg=_cfg()))
    # unchanged give-up behavior: 5 fast exits then stop
    assert mgr.calls == main.ASYNC_MAX_FAST_EXITS
    # NEW: the give-up now fails the whole match
    assert game.match_failed is True
    assert _events(game, EventType.MATCH_FAILED)
    assert "gave up" in game.fatal_reason


# ── E: non-retryable auth failure fails fast (first exit), stderr-only ────────

def test_401_on_stderr_fails_on_first_exit(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", lambda _s: asyncio.sleep(0))
    game = _make_game()
    mgr = _ScriptedMgr([_R(exit_code=1, stderr=b"HTTP 401 Unauthorized: invalid x-api-key")])
    asyncio.run(main._run_async_agent(game, backend=None, mgr=mgr,
                                      handle=None, team_id="team1", cfg=_cfg()))
    assert mgr.calls == 1                    # fatal on the FIRST fast exit, no 5× burn
    assert game.match_failed is True
    assert "auth" in game.fatal_reason


def test_401_on_stdout_does_NOT_fast_fail(monkeypatch):
    """The agent's stdout transcript can legitimately contain '401' from probing
    the opponent. That must NOT trip the auth fast-fail — only stderr does."""
    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    game = _make_game()
    mgr = _ScriptedMgr([_R(exit_code=1, stdout=b"got 401 from team2_prod:6789", stderr=b"")])
    asyncio.run(main._run_async_agent(game, backend=None, mgr=mgr,
                                      handle=None, team_id="team1", cfg=_cfg()))
    # falls through to the ordinary 5-fast-exit give-up (still fails, via rule C)
    assert mgr.calls == main.ASYNC_MAX_FAST_EXITS
    assert game.match_failed is True
    assert "gave up" in game.fatal_reason    # not the auth path


def test_healthy_clean_exit_is_not_a_failure(monkeypatch):
    """Idle/摆烂 guard: an agent that runs a full healthy chunk then exits 0 is a
    legitimate game state — it must never be failed. We stop the loop via
    game-over (as end-of-game does) and assert NO fail_match happened."""
    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    game = _make_game()

    # A mgr whose run is 'healthy' (>= ASYNC_MIN_HEALTHY_SECS) and clean (exit 0);
    # after the first chunk we flip game-over so the supervisor loop exits.
    class _HealthyThenOver:
        def __init__(self):
            self.calls = 0
        async def run_phase(self, backend, handle, prompt, *, timeout, model=None, **_):
            self.calls += 1
            game._game_over.set()            # end-of-game arrives during the chunk
            return _R(exit_code=0)
    mgr = _HealthyThenOver()
    # monkeypatch time so the chunk reads as a long, healthy run
    ts = iter([1000.0, 1000.0 + main.ASYNC_MIN_HEALTHY_SECS + 5])
    monkeypatch.setattr(main.time, "time", lambda: next(ts, 2000.0))
    asyncio.run(main._run_async_agent(game, backend=None, mgr=mgr,
                                      handle=None, team_id="team1", cfg=_cfg()))
    assert mgr.calls == 1
    assert game.match_failed is False        # a clean healthy exit is NOT a failure


# ── C': a fast BUT productive exit is not a crash-loop (Finding 3) ───────────

def test_chunk_did_work_stdout_fallback_when_unparseable():
    """No parseable stream (harnesses we don't parse, or nothing written): credit
    only a CLEAN exit that streamed substantial stdout. A NON-zero exit that
    dumped a big banner/trace is a broken config, not work — crediting it would
    defeat the give-up. result None (run_phase raised) is a crash, never work."""
    B = main.ASYNC_WORK_STDOUT_BYTES
    assert main._chunk_did_work(_R(exit_code=0, stdout=b"x" * B), None) is True
    assert main._chunk_did_work(_R(exit_code=1, stdout=b"x" * B), None) is False   # non-zero → broken
    assert main._chunk_did_work(_R(exit_code=0, stdout=b"boom"), None) is False    # too little
    assert main._chunk_did_work(None, None) is False                              # crash, never work


def test_chunk_did_work_credits_real_events_not_init_only(tmp_path):
    """Parseable stream: a real model turn counts as work; an init-banner-only
    stream (e.g. MCP unreachable after init) does NOT — so a broken config still
    trips the guard even though it wrote a stream line. The parse path wins over
    the stdout fallback (stdout here is deliberately over the byte threshold)."""
    init_only = tmp_path / "stream-init.jsonl"
    init_only.write_text('{"type":"system","subtype":"init","model":"m","tools":[]}\n')
    assert main._chunk_did_work(
        _R(stdout=b"x" * (main.ASYNC_WORK_STDOUT_BYTES * 2)), init_only) is False

    did_work = tmp_path / "stream-work.jsonl"
    did_work.write_text(
        '{"type":"system","subtype":"init","model":"m","tools":[]}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
    )
    assert main._chunk_did_work(_R(stdout=b""), did_work) is True
    # result None (run_phase raised) is a crash, never work — even if the tee
    # already wrote a real event (Finding 6).
    assert main._chunk_did_work(None, did_work) is False


def test_fast_but_productive_exit_is_not_a_giveup(monkeypatch):
    """Finding 3: an agent that finishes its turn fast but DID real work must not
    be counted as a crash-loop fast-exit. Every chunk here is fast (time not
    monkeypatched → ~0s) and clean, with enough stdout to read as real work; the
    supervisor must keep restarting it past the give-up threshold, never failing."""
    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    game = _make_game()

    class _FastButWorking:
        def __init__(self):
            self.calls = 0
        async def run_phase(self, backend, handle, prompt, *, timeout, model=None, **_):
            self.calls += 1
            if self.calls > main.ASYNC_MAX_FAST_EXITS + 2:
                game._game_over.set()    # end the match after clearly surviving the guard
            return _R(exit_code=0, stdout=b"x" * (main.ASYNC_WORK_STDOUT_BYTES + 1))

    mgr = _FastButWorking()
    asyncio.run(main._run_async_agent(game, backend=None, mgr=mgr,
                                      handle=None, team_id="team1", cfg=_cfg()))
    assert mgr.calls > main.ASYNC_MAX_FAST_EXITS   # kept restarting, never gave up at 5
    assert game.match_failed is False              # a fast-but-productive agent is NOT killed


# ── D: dry-run failure aborts before round 1 ─────────────────────────────────

def test_dry_run_any_team_failing_fails_match():
    """Rule D: a SINGLE team failing the dry-run (e.g. that team's config is
    wrong — bad key/base_url) makes the match invalid and MUST terminate before
    R1. A/D is two-party: one dead team = uncontested noise, not a game."""
    game = _make_game()

    async def handler(team_id):
        if team_id == "team1":
            await game.record_agent_ready(team_id)   # team1 ready
        # team2: fails the probe (its config is broken)

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=0.2))
    assert any(e.team_id == "team2" for e in _events(game, EventType.DRY_RUN_FAILED))
    assert game.match_failed is True          # one broken team is fatal
    assert "team2" in game.fatal_reason


def test_dry_run_all_teams_fail_fails_match():
    game = _make_game()

    async def handler(team_id):
        return   # no team reports ready -> both DRY_RUN_FAILED

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=0.2))
    assert len(_events(game, EventType.DRY_RUN_FAILED)) == 2
    assert game.match_failed is True


def test_dry_run_all_ready_does_not_fail():
    game = _make_game()

    async def handler(team_id):
        await game.record_agent_ready(team_id)

    game.register_dry_run_handler(handler)
    asyncio.run(game.dry_run_all_agents(timeout_secs=0.2))
    assert len(_events(game, EventType.DRY_RUN_OK)) == 2
    assert game.match_failed is False


# ── AsyncLoop: pre-loop bail + failed-reason override ────────────────────────

def test_asyncloop_bails_before_starting_agents_when_prefailed(monkeypatch):
    """B/D pre-loop: with _fatal_failure already set, AsyncLoop.run records a
    failed GAME_END and returns WITHOUT connecting the network or starting agents."""
    game = _make_game(total_rounds=3)
    net = _StubNetwork()
    started = []
    monkeypatch.setattr(game, "start_async_agents", lambda: started.append(True))
    game.fail_match("flags not planted")     # pre-loop doom

    loop = AsyncLoop(game, net, game.config.rounds)
    asyncio.run(loop.run())

    assert started == []                      # agents never started
    assert net.cross_team_calls == []         # attack network never connected
    ge = _events(game, EventType.GAME_END)
    assert ge and ge[-1].payload["reason"] == "failed"
    assert ge[-1].payload["final_round"] == 0


def test_start_does_not_lazily_build_health_when_already_failed(monkeypatch, tmp_path):
    """A doomed bring-up must not stand up more facility on the way to its failed
    end: `start()`'s lazy health fallback is skipped when a fatal is already set,
    and the run goes straight to the pre-loop failed GAME_END."""
    game = _make_game(total_rounds=3)
    game.begin_run(tmp_path)                      # skip the real runs/ dir
    built: list[bool] = []
    monkeypatch.setattr(game, "_build_health_facility", lambda: built.append(True))
    monkeypatch.setattr(game, "start_async_agents", lambda: None)
    installed: list[bool] = []

    async def _install():
        installed.append(True)

    monkeypatch.setattr(game, "install_all_agents", _install)
    game.orchestrator.network = _StubNetwork()
    game.fail_match("flag-plant incomplete at setup: 1/2 flags planted at round 0")

    asyncio.run(game.start())

    assert built == []                            # no health configure push
    assert installed == []                        # no agent install either
    ge = _events(game, EventType.GAME_END)
    assert ge and ge[-1].payload["reason"] == "failed"


def test_start_still_builds_health_on_a_healthy_setup(monkeypatch, tmp_path):
    """Control: without a fatal, the lazy fallback still runs — the guard is on
    the failure, not on the fallback."""
    game = _make_game(total_rounds=0)
    game.begin_run(tmp_path)
    built: list[bool] = []
    monkeypatch.setattr(game, "_build_health_facility", lambda: built.append(True))
    monkeypatch.setattr(game, "start_async_agents", lambda: None)

    async def _noop():
        return None

    monkeypatch.setattr(game, "install_all_agents", _noop)
    monkeypatch.setattr(game, "dry_run_all_agents", lambda *a, **k: _noop())
    game.orchestrator.network = _StubNetwork()

    asyncio.run(game.start())
    assert built == [True]


def test_setup_gates_health_and_readiness_events_on_match_failed(monkeypatch):
    """Rule B, reporting arm: after a failed configure/rotate or a short round-0
    plant, bring-up must not push a health configure or claim FACILITY_READY /
    SETUP_COMPLETE — those events are what a consumer reads as "the arena came
    up". The failed GAME_END + teardown still run (see the start() tests above).

    This executes the helper `_run_start` calls after its plant-count gate; it is
    not a source-shape assertion."""
    game = _make_game()
    calls: list[str] = []
    monkeypatch.setattr(game, "_build_health_facility", lambda: calls.append("health"))
    monkeypatch.setattr(game, "record_facilities_ready", lambda: calls.append("ready"))
    monkeypatch.setattr(game, "record_setup_complete", lambda: calls.append("complete"))

    game.fail_match("flag-service configure failed")
    main._record_setup_ready_if_healthy(game)
    assert calls == []


def test_asyncloop_midloop_failure_marks_game_end_failed(monkeypatch):
    """C/E mid-loop: a fail_match during the loop halts it via _stop_event and the
    GAME_END reason is 'failed', not the 'stopped' an operator stop would give."""
    game = _make_game(total_rounds=3, round_secs=0)
    net = _StubNetwork()
    monkeypatch.setattr(game, "start_async_agents", lambda: None)

    loop = AsyncLoop(game, net, game.config.rounds)

    def _rotate(round_id):                    # first rotation trips a fatal failure
        game.fail_match("agent died mid-match")
    monkeypatch.setattr(game, "rotate_flags", _rotate)

    asyncio.run(loop.run())
    ge = _events(game, EventType.GAME_END)
    assert ge and ge[-1].payload["reason"] == "failed"
    assert ge[-1].payload.get("fatal") == "agent died mid-match"


def test_roundloop_bails_when_prefailed_sync_mode():
    """B/D pre-loop in SYNC mode: RoundLoop.run must ALSO bail with a failed
    GAME_END without running any phase when _fatal_failure is already set
    (the high-severity gap the adversarial review caught — async-only bail)."""
    from gamemgr.round import RoundLoop
    game = _make_game(mode="sync")
    net = _StubNetwork()
    game.fail_match("flags not planted")     # pre-loop doom, round_loop still None
    loop = RoundLoop(game, net, game.config.rounds)
    asyncio.run(loop.run())
    ge = _events(game, EventType.GAME_END)
    assert ge and ge[-1].payload["reason"] == "failed"
    assert ge[-1].payload["final_round"] == 0
    assert net.cross_team_calls == []        # no phase network toggles → no agents


def test_refusal_category_detects_model_safeguard():
    """A server-side model refusal on the agent's stdout is detected + categorised
    (so the supervisor/dry-run can surface AGENT_REFUSED instead of a blind
    give-up). Normal output is never a refusal."""
    from main import _refusal_category
    class R: pass
    cyber = R(); cyber.stdout = (b'{"type":"system","subtype":"model_refusal_no_fallback",'
                                 b'"api_refusal_category":"cyber","api_refusal_explanation":"x"}')
    assert _refusal_category(cyber) == "cyber"
    bare = R(); bare.stdout = b'API Error: Opus 4.8 safeguards flagged this message for a cybersecurity topic'
    assert _refusal_category(bare) == "refused"
    ok = R(); ok.stdout = b'{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}'
    assert _refusal_category(ok) is None
    assert _refusal_category(None) is None


def test_dry_run_attempt_outcome_is_secret_safe_and_preserves_policy():
    secret = b"provider detail credential-super-secret-value"
    ordinary = main._dry_run_attempt_outcome(
        _R(exit_code=7, stderr=secret)
    )
    assert ordinary.failure_class == "agent_process_exit"
    assert ordinary.retryable is True
    assert ordinary.safe_error == "agent command exited 7"
    assert secret.decode() not in repr(ordinary)

    auth = main._dry_run_attempt_outcome(
        _R(exit_code=1, stderr=b"HTTP 401 Unauthorized: invalid x-api-key")
    )
    assert auth.failure_class == "auth_error"
    assert auth.retryable is False

    refusal = main._dry_run_attempt_outcome(
        _R(exit_code=1),
        refusal_category="cyber",
    )
    assert refusal.failure_class == "model_refusal"
    assert refusal.retryable is False

    timeout = main._dry_run_attempt_outcome(
        _R(exit_code=143, stderr=secret, timed_out=True)
    )
    assert timeout.failure_class == "timeout"
    assert timeout.retryable is False
    assert secret.decode() not in repr(timeout)


def test_persist_dry_run_stderr_tail_scrubs_and_tails(tmp_path):
    """Finding 4: on a dry-run FAILURE, a SCRUBBED, tail-only stderr excerpt is
    written to a runner-local diagnostics file (never an event/timeline). Both
    known token values and credential shapes are stripped; only the last lines
    are kept."""
    game = _make_game()
    game.run_dir = tmp_path
    game._facility_token = "fac-tok-secret-987654"
    lines = [f"line {i}" for i in range(100)]
    lines.append("boom: auth failed with key sk-ant-ABCDEFGHIJKLMNOP1234567890")
    lines.append("facility token was fac-tok-secret-987654")
    result = _R(exit_code=1, stderr=("\n".join(lines)).encode())

    main._persist_dry_run_stderr_tail(game, "team1", _cfg(), result)

    f = tmp_path / "diagnostics" / "dry-run-stderr-team1.txt"
    assert f.exists()
    txt = f.read_text()
    assert "sk-ant-ABCDEFGHIJKLMNOP1234567890" not in txt     # shape-redacted
    assert "fac-tok-secret-987654" not in txt                  # known-secret stripped
    assert "<redacted>" in txt
    assert "line 62" in txt                                    # tail kept (last 40 lines)
    assert "line 61" not in txt                                # head dropped


def test_persist_dry_run_stdout_tail_for_a_clean_exit_that_never_reported_ready(tmp_path):
    """A harness can run the whole probe, decide not to call report_ready and
    exit 0 with an empty stderr (opencode/hy4-preview, 2026-09-18). The stdout
    event stream is then the only record of what the model did; it is kept
    under the same scrub/tail contract, in its own file."""
    game = _make_game()
    game.run_dir = tmp_path
    game._facility_token = "fac-tok-secret-987654"
    lines = [f'{{"type":"text","text":"line {i}"}}' for i in range(100)]
    lines.append('{"type":"text","text":"token fac-tok-secret-987654 seen"}')
    result = _R(exit_code=0, stderr=b"", stdout=("\n".join(lines)).encode())

    main._persist_dry_run_tail(game, "team1", _cfg(), result, stream="stdout")

    f = tmp_path / "diagnostics" / "dry-run-stdout-team1.txt"
    assert f.exists()
    txt = f.read_text()
    assert "fac-tok-secret-987654" not in txt
    assert "line 99" in txt and "line 60" not in txt
    # stderr was empty: no stderr file, no exception
    main._persist_dry_run_stderr_tail(game, "team1", _cfg(), result)
    assert not (tmp_path / "diagnostics" / "dry-run-stderr-team1.txt").exists()


def test_persist_dry_run_stderr_tail_keeps_the_end_not_the_head(tmp_path):
    """When the tail exceeds the byte cap, the END (where the actual error is)
    survives — redact's own head-keeping truncation would have dropped exactly
    those final lines."""
    game = _make_game()
    game.run_dir = tmp_path
    filler = " ".join(["connection", "refused", "while", "probing", "the", "target"] * 4)
    # 40 lines of ~150 bytes = ~6KB > the 4096 cap; no 40-char run, so redact
    # leaves the text intact and only the byte cap trims it.
    lines = ["EARLYMARK " + filler] + [filler for _ in range(38)] + ["LATEERROR " + filler]
    result = _R(exit_code=1, stderr=("\n".join(lines)).encode())

    main._persist_dry_run_stderr_tail(game, "team1", _cfg(), result)

    txt = (tmp_path / "diagnostics" / "dry-run-stderr-team1.txt").read_text()
    assert "LATEERROR" in txt         # the final error line is kept
    assert "EARLYMARK" not in txt     # the early line is dropped by the byte cap


def test_persist_dry_run_stderr_tail_best_effort_noops(tmp_path):
    """No stderr → no file; no run_dir → no crash (best-effort, never fails the run)."""
    game = _make_game()
    game.run_dir = tmp_path
    main._persist_dry_run_stderr_tail(game, "team1", _cfg(), _R(exit_code=1, stderr=b""))
    assert not (tmp_path / "diagnostics").exists()

    game.run_dir = None
    main._persist_dry_run_stderr_tail(game, "team1", _cfg(), _R(exit_code=1, stderr=b"boom"))


@pytest.mark.parametrize(
    ("stderr", "refusal_category", "timed_out", "category", "error_token", "boot_stage"),
    [
        (b"failed to parse profile patch", None, False, "profile_patch_parsing", "profile_patch_parse_error", "profile_overlay_resolution"),
        (b"failed to load profile", None, False, "profile_patch_parsing", "profile_load_error", "profile_overlay_resolution"),
        (b"failed to load patch", None, False, "profile_patch_parsing", "patch_load_error", "profile_overlay_resolution"),
        (b"YAML parse error", None, False, "profile_patch_parsing", "yaml_parse_error", "profile_overlay_resolution"),
        (b"session initialization failed", None, False, "session_initialization", "session_init_error", "unknown"),
        (b"database is locked", None, False, "session_initialization", "state_db_locked", "unknown"),
        (b"ENOENT: no such file", None, False, "filesystem_state", "path_not_found", "unknown"),
        (b"permission denied", None, False, "filesystem_state", "permission_denied", "unknown"),
        (b"plugin failed to load", None, False, "binary_plugin_startup", "plugin_load_error", "plugin_mcp_startup"),
        (b"command not found", None, False, "binary_plugin_startup", "binary_not_found", "plugin_mcp_startup"),
        (b"HTTP 401 Unauthorized: invalid x-api-key", None, False, "auth", "auth_error", "model_provider_response"),
        (b"opaque local failure", "cyber", False, "refusal", "model_refusal", "model_provider_response"),
        (b"opaque local failure", None, True, "timeout", "process_timeout", "unknown"),
        (b"opaque local failure", None, False, "unknown_local_exit", "unknown", "unknown"),
    ],
)
def test_dsh_startup_diagnostic_categories_are_bounded_and_secret_safe(
    stderr, refusal_category, timed_out, category, error_token, boot_stage,
):
    secret = b" credential-super-secret-value"
    outcome = main._dry_run_attempt_outcome(
        _R(exit_code=1, stderr=stderr + secret, timed_out=timed_out),
        refusal_category=refusal_category,
        warmup_exit_class="nonzero_exit",
    )
    diagnostic = outcome.safe_diagnostic(1)

    assert diagnostic["schema"] == "cyberarena.dsh.startup-diagnostic/v2"
    assert diagnostic["phase"] == "dry_run"
    assert diagnostic["category"] == category
    assert diagnostic["error_token"] == error_token
    assert diagnostic["boot_stage"] == boot_stage
    assert diagnostic["warmup_exit_class"] == "nonzero_exit"
    assert diagnostic["attempt"] == 1
    assert set(diagnostic) == {
        "schema",
        "phase",
        "boot_stage",
        "warmup_exit_class",
        "attempt",
        "category",
        "error_token",
        "exit_code",
        "timed_out",
        "stderr_size_bucket",
        "stderr_lines_bucket",
        "diagnostic_digest",
    }
    assert len(diagnostic["diagnostic_digest"]) == 64
    int(diagnostic["diagnostic_digest"], 16)
    assert len(repr(diagnostic)) < 500
    assert secret.decode().strip() not in repr(outcome)
    assert secret.decode().strip() not in repr(diagnostic)


@pytest.mark.parametrize(
    "stderr",
    [
        b"profile",
        b"patch",
        b"profile update failed",
        b"patch apply failed",
        b"session ended",
        b"plugin response",
        b"syntax error",
    ],
)
def test_dsh_startup_generic_words_are_not_causal(stderr):
    diagnostic = main._dry_run_attempt_outcome(
        _R(exit_code=1, stderr=stderr)
    ).safe_diagnostic(1)

    assert diagnostic["category"] == "unknown_local_exit"
    assert diagnostic["error_token"] == "unknown"
    assert diagnostic["boot_stage"] == "unknown"


def test_dsh_startup_digest_is_of_safe_projection_not_raw_stderr():
    first = main._dry_run_attempt_outcome(
        _R(exit_code=1, stderr=b"opaque-A-secret-value")
    ).safe_diagnostic(1)
    second = main._dry_run_attempt_outcome(
        _R(exit_code=1, stderr=b"opaque-B-secret-value")
    ).safe_diagnostic(1)

    assert first == second


def test_dsh_startup_digest_covers_normalized_warmup_and_stage_fields():
    clean = main._dry_run_attempt_outcome(
        _R(exit_code=0), warmup_exit_class="clean_exit"
    ).safe_diagnostic(1)
    nonzero = main._dry_run_attempt_outcome(
        _R(exit_code=0), warmup_exit_class="nonzero_exit"
    ).safe_diagnostic(1)

    assert clean["diagnostic_digest"] != nonzero["diagnostic_digest"]


def test_dsh_startup_diagnostic_normalizes_unbounded_caller_values():
    diagnostic = main.DryRunAttemptOutcome(
        exit_code=1,
        startup_category="x" * 10_000,
        startup_phase="y" * 10_000,
        boot_stage="b" * 10_000,
        warmup_exit_class="w" * 10_000,
        error_token="z" * 10_000,
        stderr_size_bucket="s" * 10_000,
        stderr_lines_bucket="l" * 10_000,
    ).safe_diagnostic(99)

    assert diagnostic["category"] == "unknown_local_exit"
    assert diagnostic["phase"] == "unknown"
    assert diagnostic["attempt"] == 0
    assert diagnostic["boot_stage"] == "unknown"
    assert diagnostic["warmup_exit_class"] == "unknown"
    assert diagnostic["error_token"] == "unknown"
    assert diagnostic["stderr_size_bucket"] == "unknown"
    assert diagnostic["stderr_lines_bucket"] == "unknown"
    assert len(repr(diagnostic)) < 500


def test_dsh_clean_exit_maps_to_headless_app_exit_stage():
    diagnostic = main._dry_run_attempt_outcome(
        _R(exit_code=0), warmup_exit_class="clean_exit"
    ).safe_diagnostic(1)

    assert diagnostic["category"] == "clean_exit"
    assert diagnostic["error_token"] == "clean_exit"
    assert diagnostic["boot_stage"] == "headless_app_exit"
    assert diagnostic["warmup_exit_class"] == "clean_exit"


def test_supervisor_fails_match_on_first_model_refusal(monkeypatch):
    """ANY server-side model refusal (e.g. opus's cyber safeguard) aborts the
    WHOLE match immediately — the FIRST refusal fails it, with no threshold and
    no respawn loop. A single refusal already poisons the `--continue` transcript
    (and would otherwise hot-loop the poisoned session, re-priming the whole
    prompt each restart), so any resulting score is untrustworthy: fail-loud
    rather than tolerate it. The refusal is VISIBLE (AGENT_REFUSED with category
    + AGENT_FAILED), and the fatal reason NAMES it, not a generic give-up."""
    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    game = _make_game()
    refusal = _R(exit_code=1,
                 stdout=b'{"subtype":"model_refusal_no_fallback","api_refusal_category":"cyber"}')
    # _ScriptedMgr repeats the result forever, so if the loop DIDN'T break on the
    # first refusal there'd be many AGENT_REFUSED events — exactly-1 proves it did.
    mgr = _ScriptedMgr([refusal])
    asyncio.run(main._run_async_agent(game, backend=None, mgr=mgr,
                                      handle=None, team_id="team1", cfg=_cfg()))
    refs = _events(game, EventType.AGENT_REFUSED)
    assert len(refs) == 1                                   # aborted on the FIRST refusal
    assert refs[0].payload["category"] == "cyber"
    assert game.match_failed is True
    assert "REFUSAL" in game.fatal_reason.upper()           # reason names it
    assert "any refusal fails the match" in game.fatal_reason
    fails = _events(game, EventType.AGENT_FAILED)
    assert fails and "model refusal" in fails[-1].payload["error"]


# ── B: per-round count gate (silent short a LATER round, no event) ────────────

def test_per_round_count_gate_catches_silent_short():
    """A later round lands FEWER FLAG_PLANTED than expected WITHOUT a
    FLAG_PLANT_FAILED event (callback lost / partial crash). on_round_start(N)
    count-checks round N-1 (long settled) and fails the match — the event gate
    alone would miss it."""
    game = _make_game()
    game._challenge_by_owner = {("team1", "svc"): _stub_ch(), ("team2", "svc"): _stub_ch()}  # expected 2
    for t in ("team1", "team2"):   # round 0 planted its full set → gate armed
        game.timeline.record(EventType.FLAG_PLANTED, team_id=t, round_id=0, payload={"service": "svc"})
    game.timeline.record(EventType.FLAG_PLANTED, team_id="team1", round_id=1, payload={"service": "svc"})  # round 1 SHORT (1/2)
    game.on_round_start(2, "LIVE")      # checks round 1 → short → fail
    assert game.match_failed is True
    assert "round 1" in game.fatal_reason


def test_per_round_gate_stays_silent_without_a_flag_service():
    """Flag-service-less / observer runs (and unit tests that drive the round loop
    with no plant) must NOT trip the gate — round 0 never planted, so it's disarmed."""
    game = _make_game()
    game._challenge_by_owner = {("team1", "svc"): _stub_ch(), ("team2", "svc"): _stub_ch()}
    game.on_round_start(1, "LIVE")
    game.on_round_start(2, "LIVE")
    assert game.match_failed is False


# ── C: round-0 cold-start plant budget is wider than the warm restart-race one ─

def test_round0_plant_budget_is_wider_than_warm():
    from flag.server import _PLANT_RETRY_BACKOFF_S, _PLANT_RETRY_BACKOFF_R0_S
    assert len(_PLANT_RETRY_BACKOFF_R0_S) > len(_PLANT_RETRY_BACKOFF_S)      # more attempts at cold start
    assert sum(_PLANT_RETRY_BACKOFF_R0_S) >= 60                              # a real grace for slow heavy images
    assert _PLANT_RETRY_BACKOFF_R0_S[:len(_PLANT_RETRY_BACKOFF_S)] == _PLANT_RETRY_BACKOFF_S  # warm prefix preserved


def test_flag_http_timeout_exceeds_round0_plant_budget():
    """INVARIANT: the orche-side /flags/rotate client timeout must exceed the
    flag-service's round-0 cold-start plant budget. Otherwise a slow cold plant
    times out on the orche side, the error is swallowed, and the setup gate sees
    0 flags and FALSE-fails the match. Regression guard for the C-patch vs
    60s-timeout interaction."""
    import httpx
    from flag.server import _PLANT_RETRY_BACKOFF_R0_S
    game = _make_game()
    timeout = game._flag_http.timeout.read
    budget = sum(_PLANT_RETRY_BACKOFF_R0_S)
    assert timeout > budget, f"flag_http timeout {timeout}s must exceed round-0 budget {budget}s"


def test_install_all_agents_is_best_effort_when_a_handler_raises(monkeypatch):
    """The contract the agent-install DEADLINE depends on.

    `main.py` bounds one team's install (CLI install + exploit provisioning +
    source_in_dev staging) with `_AGENT_INSTALL_TIMEOUT_SECS` and lets the
    expiry raise. That is a fast, legible failure — rather than a crash that
    takes the whole match with it — only because `install_all_agents` is
    best-effort: it logs the team, moves on, and the round still starts, so the
    failing team's kickoff records a clean AGENT_FAILED.

    Pinned here because the deadline's behaviour changes silently if this ever
    becomes fail-fast: one slow install would then abort an otherwise healthy
    match, which is strictly worse than the stall the deadline replaced.
    """
    game = _make_game(total_rounds=0)
    monkeypatch.setattr(
        game, "agent_config_for", lambda team_id, is_first=False: object()
    )

    called: list[str] = []

    async def _timed_out(team_id, cfg):
        called.append(team_id)
        raise TimeoutError(f"agent install for team {team_id!r} exceeded its deadline")

    game.register_install_agent_handler(_timed_out)

    asyncio.run(game.install_all_agents())      # must NOT propagate

    assert sorted(called) == ["team1", "team2"]


def test_persist_dry_run_text_keeps_a_longer_tail_for_a_server_log(tmp_path):
    """Adapter-supplied diagnostics (a harness's own server log) go through the
    same scrub/tail contract as the CLI streams, under their own name and with
    the longer server-log allowance."""
    game = _make_game()
    game.run_dir = tmp_path
    game._facility_token = "fac-tok-secret-987654"
    lines = [f"INFO request {i}" for i in range(300)]
    lines.append("ERROR provider cyberarena: model hy4-preview key fac-tok-secret-987654")
    main._persist_dry_run_text(game, "team1", _cfg(), "opencode-serve", "\n".join(lines),
                               lines=main._DRY_RUN_SERVER_LOG_TAIL_LINES,
                               nbytes=main._DRY_RUN_SERVER_LOG_TAIL_BYTES)
    f = tmp_path / "diagnostics" / "dry-run-opencode-serve-team1.txt"
    txt = f.read_text()
    assert "fac-tok-secret-987654" not in txt and "ERROR provider" in txt
    assert "INFO request 101" in txt and "INFO request 100" not in txt   # last 200 lines
    # an empty contribution writes nothing
    main._persist_dry_run_text(game, "team2", _cfg(), "opencode-serve", "  \n")
    assert not (tmp_path / "diagnostics" / "dry-run-opencode-serve-team2.txt").exists()


def test_dry_run_extra_diagnostics_is_best_effort():
    """The base adapter contributes nothing; a raising adapter contributes
    nothing and never fails the dry-run."""
    class Base:
        async def dry_run_diagnostics(self, transport, handle):
            return {}

    class Boom:
        async def dry_run_diagnostics(self, transport, handle):
            raise RuntimeError("no such container")

    assert asyncio.run(main._dry_run_extra_diagnostics(Base(), None, None)) == {}
    assert asyncio.run(main._dry_run_extra_diagnostics(Boom(), None, None)) == {}


def test_dry_run_prompt_is_reworded_for_hy4_only():
    """Only the models in agents.opencode.NO_REASONING_PROBE_MODELS get the
    anti-reasoning probe wording; everyone else keeps the original prompt."""
    base = main._dry_run_prompt("report_ready")
    assert base == main._dry_run_prompt("report_ready", None)
    assert base == main._dry_run_prompt("report_ready", "claude-opus-4-8")
    assert base == main._dry_run_prompt("report_ready", "cyberarena/MiniMax-M3")
    assert base.startswith("Pre-game setup check.")
    hy4 = main._dry_run_prompt("report_ready", "hy4-preview")
    assert hy4 != base and "Do NOT think" in hy4 and "`report_ready`" in hy4
    assert main._dry_run_prompt("report_ready", "cyberarena/hy4-preview") == hy4
