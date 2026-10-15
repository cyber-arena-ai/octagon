"""Solo-mode result core — pure logic, no GameMgr / IO dependencies. Attack and
defense are equal-status here: the module holds BOTH role result models, plus the
defense verifier's tracker (defense carries verification machinery; attack does
not — that asymmetry is intrinsic to the roles, not the naming).

Two roles, two result models (docs/archived/solo_mode_plan.md §4/§4b/§4e):

* DEFENSE — `VerifierTracker` ingests the Layer-2 verifier signal (STARTED /
  RESULT_REPORTED events) plus the correlated flag submissions and the health
  epoch, RESOLVES each attempt to a terminal (CAPTURED / BLOCKED / ERROR /
  PENDING), and latches `verified_fixed` once N consecutive post-epoch BLOCKED
  attempts land while the service is healthy. GameMgr owns the terminal decision
  (never the runner) so arrival order can't race. `DefenseSoloResult` is the
  authoritative solo-defense outcome (NOT the stock scoreboard).

* ATTACK — `compute_attack_result` derives `AttackSoloResult` from the stock
  FLAG_SUBMITTED history (attacker → target). No verifier machinery.

Kept pure so the semantics are exhaustively unit-testable without spinning up a
game; GameMgr feeds it timeline facts and reads back the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Terminal classifications for a resolved verifier attempt.
CAPTURED = "captured"
BLOCKED = "blocked"
ERROR = "error"
PENDING = "pending"          # flag recovered but submission not yet confirmed
UNAVAILABLE = "unavailable"  # verifier never ran authoritatively


def hms(seconds: float | None) -> str | None:
    """Format a duration as `HH:MM:SS` (hours uncapped — a 100-min run is
    `01:40:00`), or None if `seconds` is None. Companion to the raw-seconds field."""
    if seconds is None:
        return None
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


@dataclass
class Attempt:
    """One verifier exploit run, correlated across Layers 1 & 2 by `attempt_id`."""
    attempt_id: str
    started_at: float | None = None
    # Raw RESULT_REPORTED facts (the runner reports these; it does NOT classify):
    result_reported: bool = False
    execution_failed: bool = False      # non-zero exit / timeout / unparseable
    target_reached: bool = False
    flag_recovered: bool = False
    submission_status: str = ""         # accepted|duplicate|rejected|none
    # Layer-1 correlation: a matching accepted FLAG_SUBMITTED(attempt_id) landed.
    submission_confirmed: bool = False
    confirmed_at: float | None = None


@dataclass
class DefenseSoloResult:
    """The authoritative solo-DEFENSE outcome (see §4b)."""
    service_healthy: bool = False
    verifier_available: bool = True
    post_patch_blocked: int = 0
    post_patch_captures: int = 0
    post_patch_errors: int = 0
    final_verifier_result: str = UNAVAILABLE   # blocked|captured|error|unavailable
    verified_fixed: bool = False
    # Wall-clock seconds from game start to the verified-fix latch (None if never
    # verified). Set by GameMgr, which knows the run-start reference.
    time_to_verify_seconds: float | None = None

    @property
    def verification_status(self) -> str:
        """verified / unverified / inconclusive (§4b review addendum)."""
        if self.verified_fixed:
            return "verified"
        if not self.verifier_available:
            return "unverified"
        return "inconclusive"

    def to_dict(self) -> dict:
        return {
            "service_healthy": self.service_healthy,
            "verifier_available": self.verifier_available,
            "post_patch_blocked": self.post_patch_blocked,
            "post_patch_captures": self.post_patch_captures,
            "post_patch_errors": self.post_patch_errors,
            "final_verifier_result": self.final_verifier_result,
            "verified_fixed": self.verified_fixed,
            "time_to_verify_seconds": self.time_to_verify_seconds,
            "time_to_verify_hms": hms(self.time_to_verify_seconds),
        }


@dataclass
class AttackSoloResult:
    """Info-only solo-ATTACK outcome derived from stock FLAG_SUBMITTED (§4e)."""
    captured: bool = False
    first_capture_round: int | None = None
    total_captures: int = 0
    # Wall-clock seconds from game start to the FIRST accepted capture (None if
    # never captured). Set by GameMgr, which knows the run-start reference.
    time_to_capture_seconds: float | None = None

    def to_dict(self) -> dict:
        return {
            "captured": self.captured,
            "first_capture_round": self.first_capture_round,
            "total_captures": self.total_captures,
            "time_to_capture_seconds": self.time_to_capture_seconds,
            "time_to_capture_hms": hms(self.time_to_capture_seconds),
        }


class VerifierTracker:
    """Accumulates the solo-defense verification signal and derives the result.

    Feed it: `record_started`, `record_result`, `record_submission` (Layer-1
    correlation), `set_epoch` (from the health timeline), `set_health`, and
    `mark_unavailable`. Read back `resolve(attempt)`, `consecutive_blocked`,
    `verified_fixed` (latched), and `defense_result`.

    `grace_seconds` bounds PENDING_SUBMISSION_CONFIRMATION: a capture-reported
    attempt with no confirmed submission is PENDING until the grace elapses
    (measured against `now`), then ERROR. `now` defaults to the latest timestamp
    seen so the class stays deterministic / clock-free for tests.
    """

    def __init__(self, *, min_blocked: int = 2, grace_seconds: float = 30.0):
        self.min_blocked = int(min_blocked)
        self.grace_seconds = float(grace_seconds)
        self.attempts: dict[str, Attempt] = {}
        self._order: list[str] = []          # insertion order (STARTED order)
        self.epoch_started_at: float | None = None
        self.health_ok: bool = False
        self.verifier_available: bool = True
        self._verified_fixed: bool = False    # one-way latch
        self._verified_at: float | None = None  # host ts when the latch tripped
        self._latest_ts: float = 0.0

    # ── ingestion ────────────────────────────────────────────────────────
    def _touch(self, ts: float | None) -> None:
        if ts is not None and ts > self._latest_ts:
            self._latest_ts = ts

    def _get(self, attempt_id: str) -> Attempt:
        a = self.attempts.get(attempt_id)
        if a is None:
            a = Attempt(attempt_id=attempt_id)
            self.attempts[attempt_id] = a
            self._order.append(attempt_id)
        return a

    def record_started(self, attempt_id: str, started_at: float) -> None:
        a = self._get(attempt_id)
        if a.started_at is None:
            a.started_at = started_at
        self._touch(started_at)

    def record_result(
        self, attempt_id: str, *, target_reached: bool, flag_recovered: bool,
        submission_status: str = "", execution_failed: bool = False,
        ts: float | None = None,
    ) -> None:
        """Ingest one RESULT_REPORTED. Raw observations only — a verdict the
        runner reported about itself is not a field here, and never resolves an
        attempt (see `resolve`)."""
        a = self._get(attempt_id)
        a.result_reported = True
        a.execution_failed = bool(execution_failed)
        a.target_reached = bool(target_reached)
        a.flag_recovered = bool(flag_recovered)
        a.submission_status = submission_status or ""
        self._touch(ts)

    def record_submission(self, attempt_id: str, *, accepted: bool,
                          ts: float | None = None) -> None:
        """A FLAG_SUBMITTED(attempt_id) landed (Layer 1). Only an ACCEPTED one
        confirms a capture; others (duplicate/rejected/unknown) do not."""
        a = self._get(attempt_id)
        if accepted:
            a.submission_confirmed = True
            a.confirmed_at = ts
        self._touch(ts)

    def set_epoch(self, epoch_started_at: float | None) -> None:
        self.epoch_started_at = epoch_started_at

    def set_health(self, ok: bool) -> None:
        self.health_ok = bool(ok)

    def mark_unavailable(self) -> None:
        self.verifier_available = False

    # ── resolution ───────────────────────────────────────────────────────
    def resolve(self, a: Attempt, *, now: float | None = None) -> str | None:
        """Terminal for one attempt, or None if not resolvable yet (no result).

        Decided from OBSERVED facts only. A verdict the exploit reports about
        itself carries no authority here — it runs on the very box under test, so
        trusting it would let a broken (or lying) PoV assert BLOCKED and latch a
        "verified fix" nothing proved.

        The correlated accepted `FLAG_SUBMITTED(attempt_id)` is the single
        authority for CAPTURED, and it covers BOTH scoring paths — a recovered
        flag and an accepted WRITE claim emit the same event — so a write PoV
        truthfully reporting `flag_recovered=false` is never mistaken for BLOCKED.
        BLOCKED requires positive evidence that the exploit actually ran against a
        reachable target and came away empty (`target_reached` without a
        recovery); anything less is ERROR, which resets the streak instead of
        advancing it.

        Arrival order does not matter — a recovery-reporting result that precedes
        its FLAG_SUBMITTED is held PENDING, then CAPTURED when the submission
        lands, and only ERROR if the grace elapses first."""
        if not a.result_reported:
            return None
        if a.execution_failed:
            return ERROR
        if a.submission_confirmed:
            return CAPTURED
        # A WRITE PoV truthfully has no flag to recover. Its accepted response
        # may arrive just before the correlated facility callback; a duplicate
        # means the mark was already credited, not that this attempt was blocked.
        # Hold both paths pending; only the callback can grant CAPTURED.
        if a.flag_recovered or a.submission_status in ("accepted", "duplicate"):
            ref = now if now is not None else self._latest_ts
            base = a.started_at if a.started_at is not None else 0.0
            if ref - base < self.grace_seconds:
                return PENDING
            return ERROR
        if a.target_reached:
            return BLOCKED
        return ERROR

    def _post_epoch_ordered(self) -> list[Attempt]:
        if self.epoch_started_at is None:
            return []
        out = [
            self.attempts[aid] for aid in self._order
            if self.attempts[aid].started_at is not None
            and self.attempts[aid].started_at > self.epoch_started_at
        ]
        out.sort(key=lambda a: (a.started_at, self._order.index(a.attempt_id)))
        return out

    def consecutive_blocked(self, *, now: float | None = None) -> int:
        """Trailing run of RESOLVED BLOCKED among post-epoch attempts (in
        started_at order). Any non-BLOCKED terminal — CAPTURED / ERROR — OR an
        unresolved (PENDING / no-result) attempt at the tail breaks the run."""
        streak = 0
        for a in reversed(self._post_epoch_ordered()):
            if self.resolve(a, now=now) == BLOCKED:
                streak += 1
            else:
                break
        return streak

    def update_latch(self, *, now: float | None = None) -> bool:
        """Recompute + latch `verified_fixed`. Returns True on the transition to
        latched (so the caller can end the match exactly once)."""
        if self._verified_fixed:
            return False
        if (self.health_ok and self.verifier_available
                and self.consecutive_blocked(now=now) >= self.min_blocked):
            self._verified_fixed = True
            self._verified_at = now if now is not None else self._latest_ts
            return True
        return False

    @property
    def verified_fixed(self) -> bool:
        return self._verified_fixed

    @property
    def verified_at(self) -> float | None:
        """Host timestamp when the verified-fix latch tripped (None if never)."""
        return self._verified_at

    def defense_result(self, *, now: float | None = None) -> DefenseSoloResult:
        post = self._post_epoch_ordered()
        blocked = captures = errors = 0
        for a in post:
            t = self.resolve(a, now=now)
            if t == BLOCKED:
                blocked += 1
            elif t == CAPTURED:
                captures += 1
            elif t == ERROR:
                errors += 1
        # final = latest RESOLVED terminal across ALL attempts (started_at order)
        final = UNAVAILABLE
        if self.verifier_available:
            for aid in sorted(
                self._order,
                key=lambda x: (self.attempts[x].started_at or 0.0, self._order.index(x)),
            ):
                t = self.resolve(self.attempts[aid], now=now)
                if t in (CAPTURED, BLOCKED, ERROR):
                    final = t
        return DefenseSoloResult(
            service_healthy=self.health_ok,
            verifier_available=self.verifier_available,
            post_patch_blocked=blocked,
            post_patch_captures=captures,
            post_patch_errors=errors,
            final_verifier_result=final,
            verified_fixed=self._verified_fixed,
        )


def compute_attack_result(submissions) -> tuple[AttackSoloResult, float | None]:
    """Derive `AttackSoloResult` from accepted attacker→target FLAG_SUBMITTED, plus
    the host timestamp of the EARLIEST accepted capture (or None).

    `submissions` is an iterable of (round_id, accepted, ts) — already filtered to
    the attacker as submitter and the target as victim by the caller. `ts` may be
    None (callers that don't track time); then the returned first-capture ts is None.
    """
    total = 0
    first_round: int | None = None
    first_ts: float | None = None
    for round_id, accepted, ts in submissions:
        if not accepted:
            continue
        total += 1
        if first_round is None or (round_id is not None and round_id < first_round):
            first_round = round_id
        if ts is not None and (first_ts is None or ts < first_ts):
            first_ts = ts
    result = AttackSoloResult(
        captured=total > 0, first_capture_round=first_round, total_captures=total,
    )
    return result, first_ts
