"""Capability oracles — READ / WRITE / EXECUTE over ONE plant+retrieve mechanism.

The whole protocol is `FlagHandler.plant` + `FlagHandler.retrieve`:

  READ / EXECUTE — the planted value IS the scoreable flag, submitted by the
                   attacker. EXECUTE differs in WHERE the challenge plants it
                   (behind an in-service helper) and in one framework rule: its
                   helper is a rotating current-round target, so like WRITE it
                   must publish a fresh `flag_id` every round.
  WRITE          — the planted value is a facility INTEGRITY TOKEN, never
                   scoreable and never in `by_flag`. The attacker places a
                   per-round MARK in the writable target and `/game/flag/claim`
                   reads it back through the SAME `retrieve`. The two answers must
                   be logically separable; a distinct anchor is the simple way,
                   and a coexisting append target also qualifies.

These tests pin that split, the round-safety rules around it (active round,
post-read identity revalidation, target-ID freshness, fail-loud rotation) and
the required authoritative-state backend a WRITE handler reads through.
"""

from __future__ import annotations

import pytest

import flag.server as flag_server
from challenges.interface import (
    DEFAULT_STORE,
    Challenge,
    ChallengeDocs,
    Flag,
    FlagHandler,
    FlagObservation,
    FlagStoreSpec,
    FunctionalityTest,
    ObservationStatus,
    OracleKind,
    RepairResult,
    StateReadResult,
    StateReadStatus,
    StateRegionSpec,
    VulboxImage,
    VulboxTarget,
    read_authoritative_state,
    state_scan_observe,
    validate_flag_store_specs,
    validate_state_region_specs,
)
from facility import FacilityContext, LocalEventSink, LocalStatusPort
from flag import FlagServer
from flag.store import FlagRecord, FlagStore, new_mark
from gamemgr.events import EventType
from gamemgr.game_mgr import validate_challenge_contract
from gamemgr.timeline import Timeline

READ_SCOPE = "app.cross_boundary.read"
WRITE_SCOPE = "app.cross_boundary.write"
EXEC_SCOPE = "host.command.execute"


# ── stubs ─────────────────────────────────────────────────────────────


class _StubFunctionalityTest(FunctionalityTest):
    @property
    def name(self) -> str:
        return "stub-functionality"

    def run(self, target):  # pragma: no cover — unused here
        raise NotImplementedError


class _OracleHandler(FlagHandler):
    """Handler covering all three kinds through one plant/retrieve pair.

    The handle packs `store|token`, which is exactly what lets `retrieve` tell
    the audit (expected == the token) from a claim (expected == an attacker
    mark) — the dispatch every WRITE handler must implement.
    """

    def __init__(self, *, write_stores: tuple[str, ...] = (),
                 ids: bool = True) -> None:
        self.write_stores = set(write_stores)
        self.ids = ids
        self.plants: list[dict[str, Flag]] = []
        self.reads: list[tuple[str, str]] = []   # (handle, expected)
        # store -> set of marks "placed" on the box by an attacker
        self.placed: dict[str, set[str]] = {}
        # store -> integrity anchor content (None = the defender wiped it)
        self.anchor: dict[str, str | None] = {}
        self.read_status: ObservationStatus | None = None  # forced override
        self.raise_on_read = False
        self.repairs: list[str] = []

    @property
    def name(self) -> str:
        return "oracle-stub"

    def plant(self, target: VulboxTarget, flags):
        self.plants.append(dict(flags))
        handles = {}
        for store, f in flags.items():
            self.anchor[store] = f.value
            handles[store] = f"{store}|{f.value}"
        return handles

    def retrieve(self, target, handle, expected=None):
        self.reads.append((handle, expected))
        if self.raise_on_read:
            raise RuntimeError("boom")
        if self.read_status is not None:
            return FlagObservation(self.read_status, detail="forced")
        store, token = handle.split("|", 1)
        if store in self.write_stores and expected != token:
            # CLAIM half: is this attacker's mark in the writable target?
            if expected in self.placed.get(store, set()):
                return FlagObservation(ObservationStatus.PRESENT, value=expected)
            return FlagObservation(ObservationStatus.NOT_FOUND, detail="not in region")
        # AUDIT half (and the READ/EXECUTE recovery check).
        current = self.anchor.get(store)
        if current is None:
            return FlagObservation(ObservationStatus.NOT_FOUND, detail="gone")
        if expected is not None and current != expected:
            return FlagObservation(ObservationStatus.MISMATCH, value=current)
        return FlagObservation(ObservationStatus.PRESENT, value=current)

    def repair(self, target, handle) -> RepairResult:
        self.repairs.append(handle)
        return RepairResult.REPAIRED

    def flag_id(self, handle: str) -> str | None:
        if not self.ids:
            return None
        store, token = handle.split("|", 1)
        # Derived from the round's fresh planted value, so the published target
        # rotates with it — what WRITE and EXECUTE stores are required to do, and
        # never the value itself.
        return f"{store}-{token[5:13]}"


class _OracleChallenge(Challenge):
    def __init__(self, name: str = "oracles",
                 specs: tuple[FlagStoreSpec, ...] = (),
                 handler: FlagHandler | None = None,
                 regions: tuple[StateRegionSpec, ...] = ()) -> None:
        self._name = name
        self._specs = list(specs) or [FlagStoreSpec(DEFAULT_STORE)]
        self._handler = handler or _OracleHandler()
        self._ft = _StubFunctionalityTest()
        self._vulbox = VulboxImage(reference=f"stub/{name}",
                                   exposed_ports={"service": 5000})
        self._docs = ChallengeDocs(intro="", agent_prompts={}, func_test_spec="")
        self._regions = list(regions)

    @property
    def name(self) -> str:
        return self._name

    @property
    def vulbox(self) -> VulboxImage:
        return self._vulbox

    @property
    def functionality_test(self) -> FunctionalityTest:
        return self._ft

    @property
    def flag_handler(self) -> FlagHandler:
        return self._handler

    @property
    def docs(self) -> ChallengeDocs:
        return self._docs

    # NOTE: `flag_stores()` is deliberately NOT overridden — a write/execute
    # challenge declares its stores in `flag_store_specs()` alone.
    def flag_store_specs(self) -> list[FlagStoreSpec]:
        return list(self._specs)

    def authoritative_state_regions(self) -> list[StateRegionSpec]:
        return list(self._regions)


def _target_factory(team_id: str, ch: Challenge) -> VulboxTarget:
    return VulboxTarget(host=f"{team_id}_prod",
                        ports=dict(ch.vulbox.exposed_ports),
                        meta={"team_id": team_id})


def _server(challenge_by_owner: dict, *, round: int = 1, phase: str = "LIVE"):
    timeline = Timeline()
    state = {"round": round, "phase": phase}
    server = FlagServer(
        FacilityContext(status=LocalStatusPort(lambda: dict(state)),
                        events=LocalEventSink(timeline)),
        challenge_by_owner=challenge_by_owner,
        target_factory=_target_factory,
    )
    return server, timeline, state


def _mixed_challenge(handler: _OracleHandler | None = None) -> _OracleChallenge:
    h = handler or _OracleHandler(write_stores=("ledger",))
    return _OracleChallenge(
        specs=(
            FlagStoreSpec("note", OracleKind.READ, scope=READ_SCOPE),
            FlagStoreSpec("ledger", OracleKind.WRITE, scope=WRITE_SCOPE),
            FlagStoreSpec("shell", OracleKind.EXECUTE, scope=EXEC_SCOPE),
        ),
        handler=h,
        regions=(StateRegionSpec("ledger", "/var/lib/app/ledger"),),
    )


def _submitted(timeline: Timeline) -> list[dict]:
    return [e.payload for e in timeline.history()
            if e.type == EventType.FLAG_SUBMITTED.value]


# ── spec vocabulary ───────────────────────────────────────────────────


def test_only_write_is_claimed_the_rest_are_submitted():
    assert OracleKind.READ.submitted and OracleKind.EXECUTE.submitted
    assert not OracleKind.WRITE.submitted
    assert OracleKind.READ.oracle == OracleKind.EXECUTE.oracle == "submit"
    assert OracleKind.WRITE.oracle == "claim"


def test_spec_coerces_string_kind_and_rejects_unknown():
    assert FlagStoreSpec("x", "write").kind is OracleKind.WRITE
    with pytest.raises(ValueError):
        FlagStoreSpec("x", "sideways")


def test_non_read_store_must_declare_a_scope():
    validate_flag_store_specs([FlagStoreSpec("a")], challenge="c")  # read: optional
    for kind in (OracleKind.WRITE, OracleKind.EXECUTE):
        with pytest.raises(ValueError, match="attack scope"):
            validate_flag_store_specs([FlagStoreSpec("a", kind)], challenge="c")
    validate_flag_store_specs(
        [FlagStoreSpec("a", OracleKind.WRITE, scope=WRITE_SCOPE)], challenge="c")


def test_challenge_contract_validation_returns_regions():
    region = StateRegionSpec("db", "/var/lib/app/db")
    challenge = _OracleChallenge(regions=(region,))
    assert validate_challenge_contract(
        challenge, challenge_name=challenge.name,
    ) == [region]

    challenge._specs = [FlagStoreSpec("nope", OracleKind.WRITE)]  # no scope
    with pytest.raises(ValueError, match="attack scope"):
        validate_challenge_contract(challenge, challenge_name=challenge.name)


def test_write_challenge_contract_requires_authoritative_state_region():
    challenge = _OracleChallenge(specs=(
        FlagStoreSpec("ledger", OracleKind.WRITE, scope=WRITE_SCOPE),
    ))
    with pytest.raises(ValueError, match="must declare at least one region"):
        validate_challenge_contract(challenge, challenge_name=challenge.name)

    region = StateRegionSpec("ledger", "/var/lib/app/ledger")
    challenge._regions = [region]
    assert validate_challenge_contract(
        challenge, challenge_name=challenge.name,
    ) == [region]


def test_execute_challenge_contract_needs_no_authoritative_state_region():
    challenge = _OracleChallenge(specs=(
        FlagStoreSpec("shell", OracleKind.EXECUTE, scope=EXEC_SCOPE),
    ))
    assert validate_challenge_contract(
        challenge, challenge_name=challenge.name,
    ) == []


def test_specs_are_authoritative_when_flag_stores_is_not_overridden():
    """A write/execute challenge declares its stores in `flag_store_specs()`
    ALONE. `flag_stores()` keeps its untouched all-READ default, and nothing
    downstream reads it: contract validation, the flag count and the plant all
    follow the specs."""
    challenge = _mixed_challenge()
    assert challenge.flag_stores() == [DEFAULT_STORE]        # never overridden
    assert [s.name for s in challenge.flag_store_specs()] == [
        "note", "ledger", "shell"]
    validate_challenge_contract(challenge, challenge_name=challenge.name)

    # The flag count GameMgr fails the match on counts the DECLARED stores, not
    # the default one a stale `flag_stores()` view would report.
    by_owner = {("team1", "oracles"): challenge, ("team2", "oracles"): challenge}
    assert sum(len(ch.flag_store_specs()) for ch in by_owner.values()) == 6

    server, _timeline, _state = _server(by_owner)
    planted = server.plant_flags(round_id=1)
    assert {(t, store) for t, _svc, store, _v in planted} == {
        (team, store) for team in ("team1", "team2")
        for store in ("note", "ledger", "shell")
    }
    # …and the WRITE store's token stays out of the submittable index.
    ledger = server.store.planted[("team1", "oracles", "ledger")]
    assert ledger.flag.value not in server.store.by_flag


# ── the store: one record type, one planted map ───────────────────────


def _record(value: str, kind: OracleKind, *, round_id: int = 1,
            flag_id: str = "t-1") -> FlagRecord:
    return FlagRecord(flag=Flag(value=value), handle=f"h|{value}",
                      round_id=round_id, flag_id=flag_id, kind=kind)


def test_live_record_has_no_redundant_active_bit():
    assert "active" not in FlagRecord.__dataclass_fields__


def test_write_token_is_not_scoreable_but_read_and_execute_are():
    st = FlagStore()
    st.commit_group("v", "svc", {
        "note": _record("FLAG{READ}", OracleKind.READ),
        "ledger": _record("FLAG{TOKEN}", OracleKind.WRITE),
        "shell": _record("FLAG{EXEC}", OracleKind.EXECUTE),
    })
    # One map holds all three; only the submittable kinds are indexed.
    assert len(st.planted) == 3
    assert set(st.by_flag) == {"FLAG{READ}", "FLAG{EXEC}"}
    assert st.submit("a", "FLAG{READ}", 1).reason == "ACCEPTED"
    assert st.submit("a", "FLAG{EXEC}", 1).reason == "ACCEPTED"
    # Recovering the facility integrity token wins nothing at all.
    assert st.submit("a", "FLAG{TOKEN}", 1).reason == "UNKNOWN"


def test_submit_of_a_previous_rounds_value_is_rotated_not_accepted():
    st = FlagStore()
    st.commit_group("v", "svc", {"note": _record("FLAG{OLD}", OracleKind.READ,
                                                 round_id=1)})
    assert st.submit("a", "FLAG{OLD}", 2).reason == "ROTATED"
    assert st.submit("a", "FLAG{OLD}", 1).reason == "ACCEPTED"


def test_issue_mark_is_per_attacker_stable_and_write_only():
    st = FlagStore()
    st.commit_group("v", "svc", {
        "note": _record("FLAG{READ}", OracleKind.READ),
        "ledger": _record("FLAG{TOKEN}", OracleKind.WRITE),
    })
    m1, rec = st.issue_mark("a", "v", "svc", "ledger", 1)
    m2, _ = st.issue_mark("a", "v", "svc", "ledger", 1)
    other, _ = st.issue_mark("b", "v", "svc", "ledger", 1)
    assert m1 and m1 == m2 and other and other != m1
    assert rec is st.planted[("v", "svc", "ledger")]
    # Own team, a read store, and an absent store all issue nothing.
    assert st.issue_mark("v", "v", "svc", "ledger", 1) == (None, None)
    assert st.issue_mark("a", "v", "svc", "note", 1) == (None, None)
    assert st.issue_mark("a", "v", "svc", "nope", 1) == (None, None)
    # A stale round returns the record (so the caller can say ROTATED) but no mark.
    stale_mark, stale_rec = st.issue_mark("a", "v", "svc", "ledger", 2)
    assert stale_mark is None and stale_rec is not None


def test_credit_claim_is_once_per_attacker_per_round():
    st = FlagStore()
    st.commit_group("v", "svc", {"ledger": _record("FLAG{T}", OracleKind.WRITE)})
    rec = st.planted[("v", "svc", "ledger")]
    assert st.credit_claim("a", "v", "svc", "ledger", rec, 1).reason == "ACCEPTED"
    assert st.credit_claim("a", "v", "svc", "ledger", rec, 1).reason == "DUPLICATE"
    assert st.credit_claim("b", "v", "svc", "ledger", rec, 1).reason == "ACCEPTED"
    assert st.credit_claim("v", "v", "svc", "ledger", rec, 1).reason == "OWN"


def test_credit_requires_the_record_the_read_was_made_against():
    """A rotation landing during the (seconds-long) region read must not credit
    the round-N mark against the round-N+1 record."""
    st = FlagStore()
    st.commit_group("v", "svc", {"ledger": _record("FLAG{T1}", OracleKind.WRITE)})
    observed = st.planted[("v", "svc", "ledger")]
    st.commit_group("v", "svc", {"ledger": _record("FLAG{T2}", OracleKind.WRITE,
                                                   round_id=2, flag_id="t-2")})
    assert st.credit_claim("a", "v", "svc", "ledger", observed, 2).reason == "ROTATED"
    # …and the attacker's legitimate round-2 claim is still available.
    fresh = st.planted[("v", "svc", "ledger")]
    assert st.credit_claim("a", "v", "svc", "ledger", fresh, 2).reason == "ACCEPTED"


@pytest.mark.parametrize("kind", [OracleKind.WRITE, OracleKind.EXECUTE])
def test_rotating_target_ids_must_be_fresh_each_round(kind):
    """Both kinds rotate a concrete current-round target — the writable resource,
    or the helper execution must reach — so a reused id would point an attacker at
    last round's objective instance."""
    st = FlagStore()
    st.commit_group("v", "svc", {"s": _record("FLAG{T1}", kind, flag_id="row-9")})
    # Same round: a retried plant reconciling to the same target is fine.
    st.commit_group("v", "svc", {"s": _record("FLAG{T1b}", kind, flag_id="row-9")})
    with pytest.raises(ValueError, match="reused from round"):
        st.commit_group("v", "svc", {"s": _record("FLAG{T2}", kind, round_id=2,
                                                  flag_id="row-9")})


def test_read_target_ids_may_repeat_across_rounds():
    """READ keeps the historical contract: the SECRET rotates, so re-publishing
    the same record locator (a fixed known username, say) is legitimate."""
    st = FlagStore()
    st.commit_group("v", "svc", {"note": _record("FLAG{R1}", OracleKind.READ,
                                                 flag_id="user-bob")})
    st.commit_group("v", "svc", {"note": _record("FLAG{R2}", OracleKind.READ,
                                                 round_id=2, flag_id="user-bob")})
    assert st.planted[("v", "svc", "note")].flag.value == "FLAG{R2}"


def test_attack_targets_never_publishes_a_stale_round():
    st = FlagStore()
    st.commit_group("v", "svc", {"note": _record("FLAG{A}", OracleKind.READ,
                                                 round_id=1)})
    assert st.attack_targets("a", 1) and not st.attack_targets("a", 2)
    assert not st.attack_targets("v", 1)  # own-team rows stay hidden


# ── the server: plant / claim / audit ─────────────────────────────────


def test_one_plant_call_covers_every_kind():
    h = _OracleHandler(write_stores=("ledger",))
    ch = _mixed_challenge(h)
    server, _tl, _st = _server({("v", "oracles"): ch})

    planted = server.plant_flags(round_id=1)
    assert len(h.plants) == 1                      # ONE plant() for all three
    assert set(h.plants[0]) == {"note", "ledger", "shell"}
    assert {p[2] for p in planted} == {"note", "ledger", "shell"}
    kinds = {store: server.store.planted[("v", "oracles", store)].kind
             for _t, _s, store, _v in planted}
    assert kinds == {"note": OracleKind.READ, "ledger": OracleKind.WRITE,
                     "shell": OracleKind.EXECUTE}


@pytest.mark.parametrize("kind,scope", [(OracleKind.WRITE, WRITE_SCOPE),
                                        (OracleKind.EXECUTE, EXEC_SCOPE)])
def test_rotating_store_without_a_target_id_fails_the_rotation_loudly(
        monkeypatch, kind, scope):
    """A WRITE store with no target id gives the attacker nowhere to write; an
    EXECUTE store with none gives them no helper to reach. Both fail the plant."""
    h = _OracleHandler(write_stores=("s",) if kind is OracleKind.WRITE else (),
                       ids=False)
    ch = _OracleChallenge(specs=(FlagStoreSpec("s", kind, scope=scope),), handler=h)
    server, _tl, _st = _server({("v", "oracles"): ch})
    monkeypatch.setattr(flag_server, "_PLANT_RETRY_BACKOFF_S", ())   # no real sleeps

    failed: list[tuple] = []
    with pytest.raises(RuntimeError, match="did not rotate every target"):
        server.plant_flags(round_id=1,
                           on_failed=lambda t, s, m: failed.append((t, s)))
    assert failed == [("v", "oracles")]
    # Nothing was committed, and the round is NOT marked as planted, so a retry
    # is still possible.
    assert not server.store.planted
    assert server._last_planted_round == -1


def test_a_read_store_without_a_target_id_still_rotates():
    """READ compatibility: `flag_id() -> None` just means "not identity-keyed",
    which is the historical default and must keep working."""
    h = _OracleHandler(ids=False)
    ch = _OracleChallenge(specs=(FlagStoreSpec("note", scope=READ_SCOPE),),
                          handler=h)
    server, _tl, _st = _server({("v", "oracles"): ch})
    assert len(server.plant_flags(round_id=1)) == 1
    assert server.store.planted[("v", "oracles", "note")].flag_id is None
    assert server.attack_info("a") == []          # nothing public to surface


def test_claim_credits_only_the_attackers_own_placed_mark(monkeypatch):
    h = _OracleHandler(write_stores=("ledger",))
    ch = _mixed_challenge(h)
    server, timeline, _st = _server({("v", "oracles"): ch, ("a", "oracles"): ch})
    server.plant_flags(round_id=1)

    # Nothing placed yet.
    assert server.claim("a", "v", "oracles", "ledger").reason == "NOT_OBSERVED"

    # The attacker learns its mark from attack-info, then places it.
    rows = {r["store"]: r for r in server.attack_info("a")}
    mark = rows["ledger"]["mark"]
    h.placed["ledger"] = {mark}
    assert server.claim("a", "v", "oracles", "ledger").reason == "ACCEPTED"
    assert server.claim("a", "v", "oracles", "ledger").reason == "DUPLICATE"

    # A DIFFERENT team's placed mark never credits this attacker.
    h.placed["ledger"] = {new_mark()}
    assert server.claim("b", "v", "oracles", "ledger").reason == "NOT_OBSERVED"

    ev = [p for p in _submitted(timeline) if p["reason"] == "ACCEPTED"]
    assert ev[0]["store"] == "ledger"
    assert ev[0]["outcome"] == "write" and ev[0]["oracle"] == "claim"
    assert ev[0]["scope"] == WRITE_SCOPE


def test_claim_passes_the_mark_while_the_audit_passes_the_facility_token():
    h = _OracleHandler(write_stores=("ledger",))
    ch = _mixed_challenge(h)
    server, _tl, _st = _server({("v", "oracles"): ch})
    server.plant_flags(round_id=1)
    token = server.store.planted[("v", "oracles", "ledger")].flag.value

    h.reads.clear()
    mark, _rec = server.store.issue_mark("a", "v", "oracles", "ledger", 1)
    server.claim("a", "v", "oracles", "ledger")
    assert h.reads == [(f"ledger|{token}", mark)]

    h.reads.clear()
    server.audit(round_id=1)
    assert (f"ledger|{token}", token) in h.reads


def test_conclusive_absence_and_unreadable_regions_are_told_apart():
    """NOT_FOUND (clean) and MISMATCH (some other value there) both mean the mark
    is demonstrably absent → NOT_OBSERVED. Only the statuses that mean "could not
    tell" become the retryable OBSERVE_ERROR — calling an unreadable box "absent"
    would hand a defender a claim-denial lever."""
    for status, reason in ((ObservationStatus.NOT_FOUND, "NOT_OBSERVED"),
                           (ObservationStatus.MISMATCH, "NOT_OBSERVED"),
                           (ObservationStatus.ERROR, "OBSERVE_ERROR"),
                           (ObservationStatus.AUTH_REJECTED, "OBSERVE_ERROR")):
        h = _OracleHandler(write_stores=("ledger",))
        ch = _mixed_challenge(h)
        server, _tl, _st = _server({("v", "oracles"): ch})
        server.plant_flags(round_id=1)
        h.read_status = status
        assert server.claim("a", "v", "oracles", "ledger").reason == reason


def test_a_raising_retrieve_costs_a_retry_not_a_500():
    h = _OracleHandler(write_stores=("ledger",))
    ch = _mixed_challenge(h)
    server, _tl, _st = _server({("v", "oracles"): ch})
    server.plant_flags(round_id=1)
    h.raise_on_read = True
    assert server.claim("a", "v", "oracles", "ledger").reason == "OBSERVE_ERROR"


def test_claim_eligibility_rejections():
    h = _OracleHandler(write_stores=("ledger",))
    ch = _mixed_challenge(h)
    server, _tl, state = _server({("v", "oracles"): ch})
    server.plant_flags(round_id=1)

    assert server.claim("v", "v", "oracles", "ledger").reason == "OWN"
    assert server.claim("a", "v", "oracles", "note").reason == "NO_STORE"
    assert server.claim("a", "ghost", "oracles", "ledger").reason == "NO_STORE"
    state["round"] = 2
    assert server.claim("a", "v", "oracles", "ledger").reason == "ROTATED"
    state["round"] = 1
    state["phase"] = "DEFENSE"
    assert server.claim("a", "v", "oracles", "ledger").reason == "WRONG_PHASE"


def test_unknown_claim_is_not_silently_labeled_read():
    ch = _mixed_challenge()
    server, timeline, _st = _server({("v", "oracles"): ch})
    assert server.claim("a", "ghost", "oracles", "missing").reason == "NO_STORE"
    event = timeline.history()[-1]
    assert not {"outcome", "scope", "oracle"}.intersection(event.payload)


def test_attack_info_labels_each_oracle_and_hides_marks_from_read_rows():
    h = _OracleHandler(write_stores=("ledger",))
    ch = _mixed_challenge(h)
    server, _tl, state = _server({("v", "oracles"): ch})
    server.plant_flags(round_id=1)

    rows = {r["store"]: r for r in server.attack_info("a")}
    assert rows["note"]["oracle"] == "submit" and "mark" not in rows["note"]
    assert rows["ledger"]["oracle"] == "claim"
    assert rows["ledger"]["mark"].startswith("MARK{")

    # EXECUTE publishes its current-round helper target and scores by submission —
    # so it must carry an id, and must NOT carry the flag or a mark.
    shell = rows["shell"]
    assert shell["outcome"] == "execute" and shell["scope"] == EXEC_SCOPE
    assert shell["oracle"] == "submit" and "mark" not in shell
    assert shell["flag_id"]
    exec_flag = server.store.planted[("v", "oracles", "shell")].flag.value
    assert exec_flag not in str(shell) and "FLAG{" not in str(shell)
    # Re-reading mid-round hands back the SAME mark: work already in flight is
    # never invalidated by looking at attack-info again.
    again = {r["store"]: r for r in server.attack_info("a")}
    assert again["ledger"]["mark"] == rows["ledger"]["mark"]

    # Nothing is published outside a live attack window, or across a rotation.
    state["phase"] = "SETUP"
    assert server.attack_info("a") == []


def test_event_and_attack_metadata_use_the_objective_snapshot():
    ch = _mixed_challenge()
    server, timeline, _st = _server({("v", "oracles"): ch})
    ch._specs = [
        FlagStoreSpec("note", OracleKind.WRITE, scope="changed.note"),
        FlagStoreSpec("ledger", OracleKind.READ, scope="changed.ledger"),
        FlagStoreSpec("shell", OracleKind.WRITE, scope="changed.shell"),
    ]
    server.plant_flags(round_id=1)

    rows = {row["store"]: row for row in server.attack_info("a")}
    assert (rows["ledger"]["outcome"], rows["ledger"]["scope"],
            rows["ledger"]["oracle"]) == ("write", WRITE_SCOPE, "claim")
    shell_flag = server.store.planted[("v", "oracles", "shell")].flag.value
    assert server.submit("a", shell_flag).accepted
    payload = _submitted(timeline)[-1]
    assert (payload["outcome"], payload["scope"], payload["oracle"]) == (
        "execute", EXEC_SCOPE, "submit",
    )


def test_audit_covers_write_stores_with_the_existing_persistence_contract():
    h = _OracleHandler(write_stores=("ledger",))
    ch = _mixed_challenge(h)
    server, timeline, _st = _server({("v", "oracles"): ch})
    server.plant_flags(round_id=1)

    # The defender wipes the facility integrity anchor of the WRITE store.
    h.anchor["ledger"] = None
    server.audit(round_id=1)
    by_store = {e.payload["store"]: e.payload for e in timeline.history()
                if e.type == EventType.FLAG_PERSISTENCE.value}
    assert set(by_store) == {"note", "ledger", "shell"}
    assert by_store["ledger"]["status"] == "NOT_FOUND"
    assert by_store["ledger"]["repair"] is None
    assert by_store["note"]["status"] == "PRESENT"


def test_audit_repair_policy_is_unchanged_for_write_stores():
    h = _OracleHandler(write_stores=("ledger",))
    ch = _OracleChallenge(
        specs=(FlagStoreSpec("ledger", OracleKind.WRITE, scope=WRITE_SCOPE),),
        handler=h)
    server, timeline, _st = _server({("v", "oracles"): ch})
    server.plant_flags(round_id=1)
    h.read_status = ObservationStatus.AUTH_REJECTED
    server.audit(round_id=1)
    p = [e.payload for e in timeline.history()
         if e.type == EventType.FLAG_PERSISTENCE.value][0]
    assert p["initial_status"] == "AUTH_REJECTED" and p["repair"] == "REPAIRED"
    assert h.repairs  # repair ran — the only write the audit ever makes


def test_rotating_target_ids_refresh_end_to_end_across_a_round():
    """The plant path derives WRITE and EXECUTE ids from the round's fresh value,
    so a rotation republishes new targets rather than tripping the reuse guard."""
    h = _OracleHandler(write_stores=("ledger",))
    ch = _mixed_challenge(h)
    server, _tl, state = _server({("v", "oracles"): ch})
    server.plant_flags(round_id=1)
    r1 = {r["store"]: r["flag_id"] for r in server.attack_info("a")}

    state["round"] = 2
    server.plant_flags(round_id=2)
    r2 = {r["store"]: r["flag_id"] for r in server.attack_info("a")}

    assert set(r1) == set(r2) == {"note", "ledger", "shell"}
    for store in ("ledger", "shell"):
        assert r1[store] and r2[store] and r1[store] != r2[store]


def test_an_append_target_may_colocate_the_token_with_attacker_marks():
    """Integrity and claim evidence must be logically SEPARABLE, not necessarily
    stored apart. An append/collection target may hold both, provided they coexist
    and a landed write can never make the audit report loss or tampering."""

    class _AppendLog(FlagHandler):
        """One append-only log holding the facility token AND attacker marks."""

        name = "append-log"

        def __init__(self) -> None:
            self.lines: list[str] = []

        def plant(self, target, flags):
            handles = {}
            for store, f in flags.items():
                self.lines = [ln for ln in self.lines if not ln.startswith("FLAG{")]
                self.lines.append(f.value)       # coexists; never displaced
                handles[store] = f"log-{f.value[5:13]}#{f.value}"
            return handles

        def retrieve(self, target, handle, expected=None):
            _target_id, token = handle.split("#", 1)
            if expected == token:                # audit: is the token still there?
                return (FlagObservation(ObservationStatus.PRESENT, value=token)
                        if token in self.lines
                        else FlagObservation(ObservationStatus.NOT_FOUND))
            return (FlagObservation(ObservationStatus.PRESENT, value=expected)
                    if expected in self.lines
                    else FlagObservation(ObservationStatus.NOT_FOUND,
                                         detail="not in region"))

        def flag_id(self, handle):
            return handle.split("#", 1)[0]

    h = _AppendLog()
    ch = _OracleChallenge(
        specs=(FlagStoreSpec("audit", OracleKind.WRITE, scope=WRITE_SCOPE),),
        handler=h)
    server, timeline, _st = _server({("v", "oracles"): ch})
    server.plant_flags(round_id=1)

    mark = {r["store"]: r for r in server.attack_info("a")}["audit"]["mark"]
    h.lines.append(mark)                          # the attacker's write lands
    assert server.claim("a", "v", "oracles", "audit").reason == "ACCEPTED"

    # …and the successful write did NOT make the integrity audit report loss.
    server.audit(round_id=1)
    persistence = [e.payload for e in timeline.history()
                   if e.type == EventType.FLAG_PERSISTENCE.value]
    assert [p["status"] for p in persistence] == ["PRESENT"]


# ── the defender-independent retrieve backend (required for claims) ───


def test_read_authoritative_state_is_fail_closed():
    def _bad_region(_r):  # pragma: no cover — never reached
        raise AssertionError("must not be called")

    assert read_authoritative_state(
        VulboxTarget("h", {}), "region").status is StateReadStatus.ERROR
    t = VulboxTarget("h", {}, meta={"read_authoritative_state": _bad_region})
    assert read_authoritative_state(t, "../etc").status is StateReadStatus.ERROR

    t = VulboxTarget("h", {}, meta={
        "read_authoritative_state": lambda _r: (_ for _ in ()).throw(RuntimeError)})
    assert read_authoritative_state(t, "region").status is StateReadStatus.ERROR

    t = VulboxTarget("h", {}, meta={"read_authoritative_state": lambda _r: {"a": b"b"}})
    assert read_authoritative_state(t, "region").status is StateReadStatus.ERROR


def test_state_scan_observe_separates_absent_from_unreadable():
    ok = VulboxTarget("h", {}, meta={
        "read_authoritative_state": lambda _r: StateReadResult(
            StateReadStatus.OK, files={"row": b"before MARK{X} after"})})
    assert state_scan_observe(ok, "region", "MARK{X}").status is ObservationStatus.PRESENT
    assert state_scan_observe(ok, "region", "MARK{Y}").status is ObservationStatus.NOT_FOUND
    broken = VulboxTarget("h", {}, meta={
        "read_authoritative_state": lambda _r: StateReadResult(
            StateReadStatus.ERROR, detail="down")})
    assert state_scan_observe(broken, "region", "MARK{X}").status is ObservationStatus.ERROR
    # An empty needle would match every region; it is an ERROR, never a credit.
    assert state_scan_observe(ok, "region", "").status is ObservationStatus.ERROR


def test_no_exec_backed_scan_helper_is_offered_for_write_claims():
    """A claim read over `exec_in_container` runs where the defender is root, so
    its output cannot prove an attacker's mark landed. The framework ships no
    such helper — `state_scan_observe` (host-read) is the only shared body."""
    import challenges.interface as ci

    assert not hasattr(ci, "exec_scan_observe")
    assert "exec_scan_observe" not in ci.__all__
    # …while the host state reader and the exec-backed AUDIT read remain.
    assert hasattr(ci, "state_scan_observe") and hasattr(ci, "read_authoritative_state")
    assert hasattr(ci, "exec_read_observe")


def test_state_region_specs_reject_unsafe_declarations():
    validate_state_region_specs([StateRegionSpec("db", "/var/lib/app/db")])
    for bad in ("relative/path", "/", "/var/../etc"):
        with pytest.raises(ValueError):
            StateRegionSpec("db", bad)
    with pytest.raises(ValueError):
        StateRegionSpec("bad name", "/var/lib/app/db")
    with pytest.raises(ValueError):
        StateRegionSpec("db", "/var/lib/app/db", max_bytes=0)
    with pytest.raises(ValueError, match="duplicate"):
        validate_state_region_specs(
            [StateRegionSpec("db", "/a"), StateRegionSpec("db", "/b")])
