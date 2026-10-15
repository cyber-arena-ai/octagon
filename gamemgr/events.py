"""Event types + factory. Append-only telemetry goes through
`gamemgr.timeline.Timeline`; this module just owns the data shapes."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class EventType(str, Enum):
    ROUND_START = "ROUND_START"
    PHASE_CHANGE = "PHASE_CHANGE"
    ROUND_STOP = "ROUND_STOP"
    SERVICE_DEPLOYED = "SERVICE_DEPLOYED"
    # Arena bring-up telemetry (pre-round-loop), so the live panel shows
    # the arena coming online instead of a blank timeline until round 1.
    # Emitted by main._run_start as each setup step completes.
    SETUP_STARTED = "SETUP_STARTED"      # run dir + sink attached; bring-up begins
    IMAGE_BUILT = "IMAGE_BUILT"          # one per challenge image (built or cached)
    CONTAINER_READY = "CONTAINER_READY"  # one per arena container created + started
    FACILITY_READY = "FACILITY_READY"    # a facility (flag / health) is constructed
    SETUP_COMPLETE = "SETUP_COMPLETE"    # bring-up done (payload: containers/services/facilities); round loop next
    # Agent lifecycle: host-emitted INSTALLED (prep done — env / mcp.json /
    # SSH pivot / workdir staged) → MCP-emitted READY (LLM proved the
    # channel works by calling report_ready). Per-phase invocation events
    # (AGENT_PROMPT_SENT / COMPLETED / TIMEOUT / FAILED) cover everything
    # downstream of install. No separate launch step — claude is spawned
    # per-phase by `AgentManager.run_phase`.
    AGENT_INSTALLED = "AGENT_INSTALLED"
    AGENT_READY = "AGENT_READY"
    # Agent read-only MCP queries, surfaced so the live panel shows the agent's
    # polling activity (the consequential tools already emit FLAG_SUBMITTED /
    # SERVICE_RESTART_* / DEPLOY_COMPLETE / AGENT_READY). MCP_ATTACKS_QUERY is the
    # high-signal one — it's the agent checking whether its own flag was stolen
    # (get_attacks_received); payload carries the returned counts.
    MCP_ATTACKS_QUERY = "MCP_ATTACKS_QUERY"
    MCP_STATE_QUERY = "MCP_STATE_QUERY"
    # Agent read its own recent health-check results (get_health_checks).
    # Read-only; payload carries how many runs were returned. The fresh
    # probes themselves are agent-triggered via trigger_health_check, which
    # surfaces as SERVICE_HEALTH_CHECK events tagged trigger="manual".
    MCP_HEALTH_QUERY = "MCP_HEALTH_QUERY"
    # Agent read its own flag-persistence status (get_flag_checks): did each
    # planted flag survive the last completed round's audit. Read-only; payload
    # carries how many stores were returned.
    MCP_FLAG_CHECK_QUERY = "MCP_FLAG_CHECK_QUERY"
    FLAG_PLANTED = "FLAG_PLANTED"
    # A per-(team, round) plant that never succeeded even after retries (e.g. the
    # box stayed down through the plant window). Emitted so a dropped rotation is
    # VISIBLE — operators/post-processing can exclude the round instead of having
    # to infer it from a missing FLAG_PLANTED.
    FLAG_PLANT_FAILED = "FLAG_PLANT_FAILED"
    FLAG_SUBMITTED = "FLAG_SUBMITTED"
    # End-of-round flag-persistence audit — one per (team, service, store) each
    # round: did the defender's OWN flag survive the round before the next
    # rotation overwrites it? Record-only (unlike FLAG_PLANT_FAILED it never fails
    # the match — a defender deleting its own flag is gameplay, not infra).
    # Payload = {service, store, initial_status, status, detail, repair}:
    #   status         — the FINAL FlagObservation status: PRESENT (survived) /
    #                    NOT_FOUND / MISMATCH (flag tampering) / AUTH_REJECTED /
    #                    ERROR (inconclusive: service unhealthy/unreachable).
    #   initial_status — the FIRST observation, before any repair; == status unless
    #                    a repair ran (i.e. unless the first read was AUTH_REJECTED).
    #   repair         — null if no repair ran; else the RepairResult of the attempt
    #                    an AUTH_REJECTED triggered: REPAIRED / NO_ACTION / FAILED.
    # INVARIANT: `repair != null` <=> `initial_status == AUTH_REJECTED` <=> a
    # read-precondition tamper WAS observed this round, regardless of repair
    # outcome. "Attempted but failed" is repair == "FAILED" (non-null): consumers
    # key precondition-tamper on `repair != null` (or initial_status), NEVER on
    # `status` (which may read PRESENT after a successful repair).
    FLAG_PERSISTENCE = "FLAG_PERSISTENCE"
    # Agent-self-reported deploy completion (agent → MCP).
    # Distinct from SERVICE_DEPLOYED which is the host-side initial install.
    DEPLOY_COMPLETE = "DEPLOY_COMPLETE"
    # DEFCON framework restart-job chain (orchestrator-emitted). Async by
    # design — STARTED fires before the background task, COMPLETED/FAILED
    # fire when the task settles. All three share a `job_id` in the payload.
    SERVICE_RESTART_STARTED = "SERVICE_RESTART_STARTED"
    SERVICE_RESTART_COMPLETED = "SERVICE_RESTART_COMPLETED"
    SERVICE_RESTART_FAILED = "SERVICE_RESTART_FAILED"
    # restart_service called outside DEFENSE (e.g. an attack agent's
    # --continue session finishing a leftover defense restart). Rejected
    # before any rebuild runs — distinct from _FAILED (which means the
    # rebuild ran and failed) so it doesn't inflate the failed count.
    SERVICE_RESTART_REJECTED = "SERVICE_RESTART_REJECTED"
    # Per-level service health probe emitted after each restart, one event
    # per HealthLevel (PROCESS → TCP → PROTOCOL → CHECKER). Payload carries
    # {level, passed, detail}. Catches "hollow patch" cheese paths where
    # lower levels pass but CHECKER (the lifted upstream poller) fails.
    SERVICE_HEALTH_CHECK = "SERVICE_HEALTH_CHECK"
    # DEFCON defense kickoff — emitted once per (team, service) on
    # PHASE_CHANGE → DEFENSE entry. Signals that a defense agent should
    # be spawned for that team. The actual `ClaudeAgentManager.install()`
    # is delegated to a handler registered via
    # `GameMgr.register_defense_kickoff_handler` so GameMgr doesn't have
    # to own the agent supervisor (lets the API server / demo / tests
    # wire whichever manager they need).
    DEFENSE_KICKOFF = "DEFENSE_KICKOFF"
    # Mirror of DEFENSE_KICKOFF — emitted once per team on
    # PHASE_CHANGE → ATTACK entry. One attack agent per team per round
    # (vs. defense's per-team-per-service) because an attack agent
    # attacks every opposing team in a single run.
    ATTACK_KICKOFF = "ATTACK_KICKOFF"
    # Round loop terminated — fired exactly once at the end of the game,
    # whether via natural exit (round counter == total_rounds), stop()
    # signal, or unhandled exception. Payload carries the cause and the
    # final round / phase the loop exited from.
    GAME_END = "GAME_END"
    # Fail-loud: the match was declared INVALID and terminated early because a
    # party stopped being a party — an agent gave up / fatally failed (auth /
    # endpoint / crash-loop), a team failed the pre-R1 dry-run, or the
    # flag-service planted fewer flags than expected. Emitted once by
    # GameMgr.fail_match (first reason wins); payload carries {reason}. Distinct
    # from GAME_END (which still fires afterwards, with reason="failed"). Drives
    # a non-zero orche exit -> the platform marks the job FAILED, not `succeeded`.
    MATCH_FAILED = "MATCH_FAILED"
    # Persistent-memory agent (v1): per-phase invocation events fired
    # by the kickoff handlers around each `ClaudeAgentManager.run_phase`
    # call. AGENT_INSTALLED already exists; these cover the per-phase
    # cycle that lives inside the round loop.
    AGENT_PROMPT_SENT = "AGENT_PROMPT_SENT"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_FAILED = "AGENT_FAILED"
    # A server-side model safeguard refused the prompt (e.g. opus cyber
    # safeguard) — the agent isn't broken; surfaced so the cause is visible.
    AGENT_REFUSED = "AGENT_REFUSED"
    # Async mode: the orchestrator intentionally SIGKILLs the long-lived
    # agent at game end. Distinct from AGENT_FAILED (a real mid-game crash)
    # so the panel doesn't paint normal end-of-game teardown as a failure.
    AGENT_STOPPED = "AGENT_STOPPED"
    # Solo DEFENSE verification signal (Layer 2). The `script` verifier team runs
    # the ground-truth exploit against the defender on a cadence and reports each
    # attempt via the gateway → GameMgr `/verify/attempt` relay. STARTED = the
    # exploit was launched (liveness); RESULT_REPORTED carries the exploit's raw
    # structured result {target_reached, flag_recovered, submission_status}.
    # GameMgr (not the runner) resolves the terminal outcome by correlating
    # RESULT_REPORTED with the matching FLAG_SUBMITTED(attempt_id). See
    # docs/archived/solo_mode_plan.md §4. Solo-defense only.
    VERIFIER_ATTEMPT_STARTED = "VERIFIER_ATTEMPT_STARTED"
    VERIFIER_ATTEMPT_RESULT_REPORTED = "VERIFIER_ATTEMPT_RESULT_REPORTED"
    # No runnable exploit for the deployed challenge and `verifier.required:
    # false` — the run continues as explicitly UNVERIFIED (the runner idles).
    VERIFIER_UNAVAILABLE = "VERIFIER_UNAVAILABLE"
    # The verifier confirmed the fix (N consecutive post-epoch BLOCKED) and the
    # match is ending successfully; the verifier's own exit is expected (not a
    # failure). Emitted once at the verified-fix latch.
    VERIFIER_COMPLETED = "VERIFIER_COMPLETED"
    # Pre-R1 dry-run readiness probe.
    # DRY_RUN_START — orchestrator emits per team before invoking the
    #   agent with the dry-run prompt.
    # DRY_RUN_OK    — orchestrator emits when AGENT_READY arrives for
    #   that team within the dry-run window (agent successfully called
    #   the existing `report_ready` MCP tool — proves the whole setup
    #   chain works end-to-end).
    # DRY_RUN_FAILED — orchestrator emits on timeout / process crash
    #   when AGENT_READY never arrives for that team.
    DRY_RUN_START = "DRY_RUN_START"
    DRY_RUN_OK = "DRY_RUN_OK"
    DRY_RUN_FAILED = "DRY_RUN_FAILED"


@dataclass(frozen=True)
class Event:
    id: str
    ts: float
    type: str
    team_id: str | None
    round_id: int
    phase: str
    payload: dict = field(default_factory=dict)


def new_event(
    type_: str | EventType,
    *,
    team_id: str | None = None,
    round_id: int = 0,
    phase: str = "",
    payload: dict | None = None,
) -> Event:
    return Event(
        id=uuid.uuid4().hex[:12],
        ts=time.time(),
        type=str(type_.value if isinstance(type_, EventType) else type_),
        team_id=team_id,
        round_id=round_id,
        phase=phase,
        payload=payload or {},
    )


def _slugify(s: str) -> str:
    """Filesystem-safe slug: keep alnum / - _ + . , collapse the rest to '-'."""
    return re.sub(r"[^A-Za-z0-9_+.-]+", "-", s).strip("-")


def new_run_dir(base: Path | str = "runs", *, label: str | None = None,
                ns: str | None = None) -> Path:
    """Create runs/<utc-timestamp>[_<ns>][_<label>]/ and return the path.

    `label` (e.g. the deployed challenge name) is slugified and appended so a run
    folder is identifiable at a glance, e.g. `runs/20260619T0102Z_nautilus-ico/`.

    `ns` (the per-run docker namespace — `s0`/`s1`/… under single-host
    concurrency) is included so two jobs that start in the SAME second on one
    host get DISTINCT run dirs. Without it they'd share
    `runs/<ts>_<challenge>/` and clobber each other's `compose/*.yaml`, events,
    and artifacts (observed: a same-second concurrent pair collided on the
    team2 compose file → docker container-name conflict). Empty/None `ns`
    (serial runs) leaves the name byte-identical to before.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    suffix = "_".join(s for s in (ns, _slugify(label) if label else None) if s)
    if suffix:
        stamp = f"{stamp}_{suffix}"
    # Uniquify on collision instead of reusing (exist_ok): two runs on one host in
    # the SAME second with the same (ns, label) would otherwise share a dir and
    # clobber each other's compose/events/artifacts. `ns` disambiguates concurrent
    # slots, but serial runs (ns="") — and any future non-namespaced caller — still
    # collide, so append -2/-3/…. exist_ok=False makes the create atomic, so this
    # is race-safe across processes on the same host.
    base = Path(base)
    candidate, n = stamp, 1
    while True:
        p = base / candidate
        try:
            p.mkdir(parents=True, exist_ok=False)
            return p
        except FileExistsError:
            n += 1
            candidate = f"{stamp}-{n}"
