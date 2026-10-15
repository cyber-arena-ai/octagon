"""FlagServer unit tests.

FlagServer owns plant + rotation + submission verification. Wired
into GameMgr but tested here in isolation against stubbed deps so we
don't need docker.

Covers both the single-store path (`SingleFlagHandler`) and the multi-store
"flag IDs" path (a `FlagHandler` with several named stores), plus the retry /
orphan-cleanup / bring-up-validation invariants the multiflag design demands.
"""

from __future__ import annotations

import re

import flag.server as flag_server
from challenges.interface import (
    DEFAULT_STORE,
    Challenge,
    ChallengeDocs,
    Flag,
    FlagHandler,
    FlagObservation,
    FunctionalityTest,
    ObservationStatus,
    SingleFlagHandler,
    VulboxImage,
    VulboxTarget,
    validate_flag_stores,
)
from facility import FacilityContext, LocalEventSink, LocalStatusPort
from flag import FlagServer
from gamemgr.events import EventType
from gamemgr.timeline import Timeline


def _ctx(timeline: Timeline, status: dict) -> FacilityContext:
    """`status` is MUTABLE so a test can advance the round the same way the real
    round loop does — the capture paths only credit the ACTIVE round, so a plant
    and the status clock have to move together."""
    return FacilityContext(
        status=LocalStatusPort(lambda: dict(status)),
        events=LocalEventSink(timeline),
    )


class _StubFlagHandler(SingleFlagHandler):
    """Single-store handler stand-in; records every plant + returns a
    deterministic handle so tests can assert what got dispatched."""

    def __init__(self) -> None:
        self.plants: list[tuple[VulboxTarget, str]] = []

    @property
    def name(self) -> str:
        return "stub-flag-handler"

    def plant_one(self, target: VulboxTarget, value: str) -> str:
        self.plants.append((target, value))
        return f"h-{len(self.plants)}"

    def retrieve(self, target: VulboxTarget, handle: str,
                 expected: str | None = None) -> FlagObservation:
        return FlagObservation(ObservationStatus.NOT_FOUND)


class _StubMultiFlagHandler(FlagHandler):
    """Multi-store handler: records the `{store: Flag}` mapping it received and
    returns one handle per store. Optionally omits one store's handle on the
    first attempt (to exercise the all-or-nothing rejection + retry path)."""

    def __init__(self, *, drop_store_first_attempt: str | None = None) -> None:
        self.calls: list[dict[str, Flag]] = []
        self.targets: list[VulboxTarget] = []
        self._drop = drop_store_first_attempt

    @property
    def name(self) -> str:
        return "stub-multi-flag-handler"

    def plant(self, target: VulboxTarget, flags):
        self.targets.append(target)
        self.calls.append(dict(flags))
        attempt = len(self.calls)
        handles = {store: f"h-{store}-{attempt}" for store in flags}
        if self._drop and attempt == 1:
            handles.pop(self._drop)  # partial return -> framework rejects -> retry
        return handles

    def retrieve(self, target: VulboxTarget, handle: str,
                 expected: str | None = None) -> FlagObservation:
        return FlagObservation(ObservationStatus.NOT_FOUND)


class _StubFunctionalityTest(FunctionalityTest):
    @property
    def name(self) -> str:
        return "stub-functionality"

    def run(self, target):  # pragma: no cover — unused in these tests
        raise NotImplementedError


class _StubChallenge(Challenge):
    """Minimal single-store Challenge (default `flag_stores()`)."""

    def __init__(self, name: str = "notes", port: int = 5000) -> None:
        self._name = name
        self._port = port
        self._flag_handler = _StubFlagHandler()
        self._functionality_test = _StubFunctionalityTest()
        self._vulbox = VulboxImage(reference=f"stub/{name}", exposed_ports={"service": port})
        self._docs = ChallengeDocs(intro="", agent_prompts={}, func_test_spec="")

    @property
    def name(self) -> str:
        return self._name

    @property
    def vulbox(self) -> VulboxImage:
        return self._vulbox

    @property
    def functionality_test(self) -> FunctionalityTest:
        return self._functionality_test

    @property
    def flag_handler(self) -> FlagHandler:
        return self._flag_handler

    @property
    def docs(self) -> ChallengeDocs:
        return self._docs


class _StubMultiChallenge(_StubChallenge):
    """Multi-store Challenge with named stores + a multi-store handler."""

    def __init__(self, name: str = "multinotes",
                 stores: tuple[str, ...] = ("document", "profile"),
                 handler: FlagHandler | None = None) -> None:
        super().__init__(name=name)
        self._stores = list(stores)
        self._flag_handler = handler or _StubMultiFlagHandler()

    def flag_stores(self) -> list[str]:
        return list(self._stores)


def _target_factory(team_id: str, ch: Challenge) -> VulboxTarget:
    return VulboxTarget(
        host=f"{team_id}_prod",
        ports=dict(ch.vulbox.exposed_ports),
        meta={"team_id": team_id, "exec_in_container": lambda *_: (0, "")},
    )


def _server(challenge_by_owner: dict | None = None, *, phase: str = "ATTACK",
            round: int = 1, team_ids: list[str] | None = None):
    timeline = Timeline()
    status = {"round": round, "phase": phase}
    server = FlagServer(
        _ctx(timeline, status),
        challenge_by_owner=challenge_by_owner if challenge_by_owner is not None else {},
        target_factory=_target_factory,
        team_ids=team_ids,
    )
    server.test_status = status          # test handle on the mutable round clock
    return server, timeline


def _rotate(server: FlagServer, round_id: int):
    """Advance the round clock and plant it — what the round loop does."""
    server.test_status["round"] = round_id
    return server.plant_flags(round_id=round_id)


def _value(server: FlagServer, team: str, service: str, store: str = DEFAULT_STORE) -> str:
    return server.store.planted[(team, service, store)].flag.value


# ── planting (single store) ───────────────────────────────────────────


def test_plant_dispatches_to_flag_handler_and_updates_store():
    ch = _StubChallenge()
    server, _tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch})

    planted = server.plant_flags(round_id=1)
    assert len(planted) == 2  # one (team, service, store, value) per team
    assert {t[0] for t in planted} == {"team1", "team2"}
    assert {t[2] for t in planted} == {DEFAULT_STORE}
    assert {p[0].host for p in ch._flag_handler.plants} == {"team1_prod", "team2_prod"}
    assert set(server.store.planted.keys()) == {
        ("team1", "notes", DEFAULT_STORE), ("team2", "notes", DEFAULT_STORE)}


def test_plant_stashes_handle_on_the_record():
    ch = _StubChallenge()
    server, _tl = _server({("team1", "notes"): ch})
    server.plant_flags(round_id=1)
    # handle now lives on the FlagRecord (not a separate _handles dict).
    assert server.store.planted[("team1", "notes", DEFAULT_STORE)].handle == "h-1"


def test_replant_replaces_prior_flag_in_store():
    ch = _StubChallenge()
    server, _tl = _server({("team1", "notes"): ch})

    p1 = server.plant_flags(round_id=1)
    old_flag = p1[0][3]  # (team, service, store, value)
    p2 = server.plant_flags(round_id=2)
    new_flag = p2[0][3]
    assert old_flag != new_flag
    assert old_flag not in server.store.by_flag
    assert new_flag in server.store.by_flag


def test_round_context_seed_is_stable_across_a_plant_retry(monkeypatch):
    handler = _StubMultiFlagHandler(drop_store_first_attempt="profile")
    ch = _StubMultiChallenge(handler=handler)
    server, _tl = _server(
        {("victim", "multinotes"): ch}, team_ids=["victim", "attacker"])
    monkeypatch.setattr(flag_server, "_PLANT_RETRY_BACKOFF_S", (0,))

    server.plant_flags(round_id=7)

    seeds = [target.meta["round_context_seed"] for target in handler.targets]
    assert len(seeds) == 2 and seeds[0] == seeds[1]
    assert re.fullmatch(r"[0-9a-f]{64}", seeds[0])
    assert handler.calls[0] == handler.calls[1]


def test_round_context_seed_separates_every_placement_coordinate(monkeypatch):
    monkeypatch.setattr(flag_server.secrets, "token_bytes", lambda _n: b"s" * 32)

    def observed_seed(*, round_id=1, victim="victim", service="service",
                      team_ids=("victim", "attacker"), stores=("document",)):
        handler = _StubMultiFlagHandler()
        ch = _StubMultiChallenge(name=service, stores=stores, handler=handler)
        server, _tl = _server(
            {(victim, service): ch}, team_ids=list(team_ids))
        server.plant_flags(round_id=round_id)
        return handler.targets[0].meta["round_context_seed"]

    baseline = observed_seed()
    assert observed_seed() == baseline
    variants = {
        observed_seed(round_id=2),
        observed_seed(victim="other", team_ids=("other", "attacker")),
        observed_seed(service="other-service"),
        observed_seed(team_ids=("victim", "other-attacker")),
        observed_seed(stores=("profile",)),
    }
    assert baseline not in variants and len(variants) == 5


def test_round_context_seed_is_private_to_plant_target():
    class _ScopeHandler(_StubFlagHandler):
        def __init__(self):
            super().__init__()
            self.retrieve_meta: list[dict] = []

        def retrieve(self, target, handle, expected=None):
            self.retrieve_meta.append(dict(target.meta))
            return FlagObservation(ObservationStatus.PRESENT, value=expected)

    ch = _StubChallenge()
    ch._flag_handler = _ScopeHandler()
    timeline = Timeline()
    status = {"round": 1, "phase": "ATTACK"}
    shared_target = _target_factory("victim", ch)
    server = FlagServer(
        _ctx(timeline, status),
        challenge_by_owner={("victim", "notes"): ch},
        target_factory=lambda *_args: shared_target,
        team_ids=["victim", "attacker"],
    )
    server.rotate(round_id=1)
    seed = ch._flag_handler.plants[0][0].meta["round_context_seed"]

    server.audit(round_id=1)

    assert "round_context_seed" not in shared_target.meta
    assert all("round_context_seed" not in meta
               for meta in ch._flag_handler.retrieve_meta)
    assert seed not in str(server.store.snapshot())
    assert seed not in str(timeline.history())
    assert seed not in str(server.attack_info("attacker"))


# ── rotate ────────────────────────────────────────────────────────────


def test_rotate_records_flag_planted_per_team():
    ch = _StubChallenge()
    server, tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch})

    server.rotate(round_id=1)
    ev = [e for e in tl.history() if e.type == EventType.FLAG_PLANTED.value]
    assert len(ev) == 2
    assert {e.team_id for e in ev} == {"team1", "team2"}
    for e in ev:
        assert e.round_id == 1
        assert e.payload["service"] == "notes"
        assert e.payload["store"] == DEFAULT_STORE
        assert "FLAG{" not in str(e.payload)  # flag strings never hit the timeline


def test_rotate_is_idempotent_per_round():
    ch = _StubChallenge()
    server, tl = _server({("team1", "notes"): ch})

    server.rotate(round_id=1)
    first = _value(server, "team1", "notes")
    server.rotate(round_id=1)  # duplicate
    assert _value(server, "team1", "notes") == first
    planted_r1 = [e for e in tl.history()
                  if e.type == EventType.FLAG_PLANTED.value and e.round_id == 1]
    assert len(planted_r1) == 1


def test_plant_flags_run_concurrently():
    import threading
    barrier = threading.Barrier(2, timeout=5)

    class _BarrierHandler(_StubFlagHandler):
        def plant_one(self, target: VulboxTarget, value: str) -> str:
            barrier.wait()
            return super().plant_one(target, value)

    c1 = _StubChallenge(); c1._flag_handler = _BarrierHandler()
    c2 = _StubChallenge(); c2._flag_handler = _BarrierHandler()
    server, _tl = _server({("team1", "notes"): c1, ("team2", "notes"): c2})

    planted = server.plant_flags(round_id=1)
    assert {p[0] for p in planted} == {"team1", "team2"}


def test_plant_flags_invokes_on_planted_per_store():
    c1 = _StubChallenge(); c2 = _StubChallenge()
    server, _tl = _server({("team1", "notes"): c1, ("team2", "notes"): c2})

    seen: list[tuple[str, str, str]] = []
    server.plant_flags(round_id=1,
                       on_planted=lambda t, s, store, _m: seen.append((t, s, store)))
    assert sorted(seen) == [("team1", "notes", DEFAULT_STORE),
                            ("team2", "notes", DEFAULT_STORE)]


# ── multiflag (N named stores per service) ────────────────────────────


def test_multiflag_plants_distinct_flag_per_store():
    ch = _StubMultiChallenge()  # stores: document, profile
    server, tl = _server({("team1", "multinotes"): ch})

    server.rotate(round_id=1)
    # two records, one per store, with DISTINCT values
    keys = {k for k in server.store.planted if k[0] == "team1"}
    assert keys == {("team1", "multinotes", "document"),
                    ("team1", "multinotes", "profile")}
    v_doc = _value(server, "team1", "multinotes", "document")
    v_prof = _value(server, "team1", "multinotes", "profile")
    assert v_doc != v_prof
    # the handler received a name-indexed mapping of Flag objects
    assert set(ch._flag_handler.calls[0]) == {"document", "profile"}
    assert isinstance(ch._flag_handler.calls[0]["document"], Flag)
    # two FLAG_PLANTED events, each carrying its store name
    ev = [e for e in tl.history() if e.type == EventType.FLAG_PLANTED.value]
    assert {e.payload["store"] for e in ev} == {"document", "profile"}


def test_multiflag_flags_submit_and_score_independently():
    ch = _StubMultiChallenge()
    server, _tl = _server({("team1", "multinotes"): ch, ("team2", "multinotes"): ch})
    server.plant_flags(round_id=1)
    v_doc = _value(server, "team1", "multinotes", "document")
    v_prof = _value(server, "team1", "multinotes", "profile")

    assert server.submit("team2", v_doc).reason == "ACCEPTED"
    assert server.submit("team2", v_prof).reason == "ACCEPTED"      # independent
    assert server.submit("team2", v_doc).reason == "DUPLICATE"      # per-flag dedup
    assert server.submit("team1", v_prof).reason == "OWN"


def test_multiflag_rotation_invalidates_all_prior_stores():
    ch = _StubMultiChallenge()
    server, _tl = _server({("team1", "multinotes"): ch})
    server.plant_flags(round_id=1)
    old_doc = _value(server, "team1", "multinotes", "document")
    old_prof = _value(server, "team1", "multinotes", "profile")
    server.plant_flags(round_id=2)
    assert old_doc not in server.store.by_flag and old_prof not in server.store.by_flag
    assert _value(server, "team1", "multinotes", "document") in server.store.by_flag


def test_missing_store_handle_fails_plant(monkeypatch):
    """A handler that returns handles for only some stores fails the plant (the
    framework's all-or-nothing acceptance), so nothing is committed for it."""
    ch = _StubMultiChallenge(handler=_StubMultiFlagHandler(drop_store_first_attempt="profile"))
    server, _tl = _server({("team1", "multinotes"): ch})
    monkeypatch.setattr(flag_server, "_PLANT_RETRY_BACKOFF_S", (0,))  # no real sleeps
    # round 1 (warm) so it uses the short backoff we just zeroed
    server.plant_flags(round_id=1)
    # first attempt dropped "profile" -> rejected -> retried; second attempt full
    assert len(ch._flag_handler.calls) == 2
    assert {k[2] for k in server.store.planted if k[0] == "team1"} == {"document", "profile"}


def test_objective_specs_are_snapshotted_at_server_construction():
    ch = _StubMultiChallenge(stores=("document", "profile"))
    server, _tl = _server({("team1", "multinotes"): ch})
    server.plant_flags(round_id=1)
    old_profile = _value(server, "team1", "multinotes", "profile")
    ch._stores = ["document"]
    server.plant_flags(round_id=2)
    assert ("team1", "multinotes", "profile") in server.store.planted
    assert old_profile not in server.store.by_flag


# ── attack info (public flag-ids) ─────────────────────────────────────


class _FlagIdHandler(SingleFlagHandler):
    """Single-store handler whose opaque handle packs BOTH a public id (a fake
    victim username) and the flag — like the real flag-id challenges. `flag_id()`
    returns ONLY the public-id half, never the flag."""

    @property
    def name(self) -> str:
        return "flagid-handler"

    def plant_one(self, target: VulboxTarget, value: str) -> str:
        return f"user-{target.meta['team_id']}#{value}"

    def retrieve(self, target: VulboxTarget, handle: str,
                 expected: str | None = None) -> FlagObservation:
        return FlagObservation(ObservationStatus.NOT_FOUND)

    def flag_id(self, handle: str) -> str | None:
        return handle.split("#", 1)[0]


class _MultiFlagIdHandler(_StubMultiFlagHandler):
    """Multi-store handler exposing a public id per store."""

    def flag_id(self, handle: str) -> str | None:
        return f"id-{handle}"


class _BrokenFlagIdHandler(_StubFlagHandler):
    """`flag_id()` throws — the framework must guard it (→ no attack-info) and
    never let it fail the plant."""

    def flag_id(self, handle: str) -> str | None:
        raise RuntimeError("boom")


def _flagid_challenge() -> _StubChallenge:
    ch = _StubChallenge()
    ch._flag_handler = _FlagIdHandler()
    return ch


def test_attack_info_surfaces_only_opponents_flag_ids():
    server, _tl = _server({("team1", "notes"): _flagid_challenge(),
                           ("team2", "notes"): _flagid_challenge()})
    server.plant_flags(round_id=1)

    tgts = server.attack_info("team1")           # attacker = team1
    assert len(tgts) == 1                          # only the OTHER team's id
    t = tgts[0]
    assert (t["team_id"], t["service"], t["store"]) == ("team2", "notes", DEFAULT_STORE)
    assert t["host"] == "team2_prod" and t["flag_id"] == "user-team2" and t["round_id"] == 1
    # the flag value is never in the surfaced attack-info
    assert "FLAG{" not in str(t) and _value(server, "team2", "notes") not in str(t)
    # symmetric: team2 sees only team1
    assert [x["team_id"] for x in server.attack_info("team2")] == ["team1"]


def test_attack_info_stashes_flag_id_on_record():
    server, _tl = _server({("team1", "notes"): _flagid_challenge()})
    server.plant_flags(round_id=1)
    rec = server.store.planted[("team1", "notes", DEFAULT_STORE)]
    assert rec.flag_id == "user-team1"


def test_attack_info_empty_when_challenge_has_no_flag_id():
    # plain SingleFlagHandler stub → flag_id() defaults to None
    server, _tl = _server({("team1", "notes"): _StubChallenge(),
                           ("team2", "notes"): _StubChallenge()})
    server.plant_flags(round_id=1)
    assert server.attack_info("team1") == []
    assert server.store.planted[("team1", "notes", DEFAULT_STORE)].flag_id is None


def test_attack_info_multiflag_surfaces_id_per_store():
    ch = _StubMultiChallenge(handler=_MultiFlagIdHandler())  # stores: document, profile
    server, _tl = _server({("team1", "multinotes"): ch, ("team2", "multinotes"): ch})
    server.plant_flags(round_id=1)
    tgts = server.attack_info("team1")
    assert {(t["team_id"], t["store"]) for t in tgts} == {
        ("team2", "document"), ("team2", "profile")}
    assert all(t["flag_id"].startswith("id-") for t in tgts)


def test_attack_info_refreshes_id_on_rotation():
    ch = _StubMultiChallenge(handler=_MultiFlagIdHandler())
    server, _tl = _server({("team2", "multinotes"): ch})
    server.plant_flags(round_id=1)
    r1 = {t["store"]: t["flag_id"] for t in server.attack_info("team1")}
    _rotate(server, 2)
    r2 = {t["store"]: t["flag_id"] for t in server.attack_info("team1")}
    assert set(r1) == set(r2) and r1 != r2       # same stores, fresh ids


def test_broken_flag_id_is_guarded_not_fatal():
    ch = _StubChallenge()
    ch._flag_handler = _BrokenFlagIdHandler()
    server, _tl = _server({("team1", "notes"): ch})
    planted = server.plant_flags(round_id=1)     # a throwing flag_id() must NOT fail the plant
    assert len(planted) == 1
    assert server.store.planted[("team1", "notes", DEFAULT_STORE)].flag_id is None
    assert server.attack_info("team2") == []


# ── bring-up validation ───────────────────────────────────────────────


def test_validate_flag_stores_rejects_illegal():
    import pytest
    for bad in ([], ["a", "a"], ["has space"], ["a/b"], [""]):
        with pytest.raises(ValueError):
            validate_flag_stores(bad, challenge="x")
    validate_flag_stores(["default"])
    validate_flag_stores(["document", "profile", "a.b-c_d"])


def test_server_rejects_invalid_specs_before_plant():
    import pytest
    ch = _StubMultiChallenge(stores=("duplicate", "duplicate"))
    with pytest.raises(ValueError, match="duplicate"):
        _server({("team1", "multinotes"): ch})


# ── submission (single) ───────────────────────────────────────────────


def test_submit_accepted_records_event_without_leaking_flag():
    ch = _StubChallenge()
    server, tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch}, round=7)
    server.plant_flags(round_id=7)
    target_flag = _value(server, "team1", "notes")

    result = server.submit("team2", target_flag)
    assert result.accepted is True and result.reason == "ACCEPTED"
    assert result.victim_team_id == "team1"
    ev = next(e for e in tl.history() if e.type == EventType.FLAG_SUBMITTED.value)
    assert ev.round_id == 7 and ev.phase == "ATTACK"
    assert ev.payload["accepted"] is True
    assert ev.payload["victim_team_id"] == "team1"
    assert target_flag not in str(ev.payload)


def test_submit_unknown_records_event_without_victim_or_service():
    server, tl = _server()
    result = server.submit("team1", "FLAG{NEVER_PLANTED}")
    assert result.accepted is False and result.reason == "UNKNOWN"
    ev = next(e for e in tl.history() if e.type == EventType.FLAG_SUBMITTED.value)
    assert ev.payload["reason"] == "UNKNOWN"
    assert ev.payload["victim_team_id"] is None


# ── phase-gating ──────────────────────────────────────────────────────


def test_submit_during_defense_rejected_as_wrong_phase():
    ch = _StubChallenge()
    server, tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch}, phase="DEFENSE")
    server.plant_flags(round_id=1)
    target_flag = _value(server, "team1", "notes")

    result = server.submit("team2", target_flag)
    assert result.accepted is False and result.reason == "WRONG_PHASE"
    assert "defense" in result.detail.lower()
    assert target_flag not in server.store.submissions.get("team2", set())
    ev = next(e for e in tl.history() if e.type == EventType.FLAG_SUBMITTED.value)
    assert ev.payload["reason"] == "WRONG_PHASE"


def test_submit_during_defense_then_attack_resubmit_accepted():
    ch = _StubChallenge()
    server, _tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch},
                          phase="DEFENSE", round=7)
    server.plant_flags(round_id=7)
    target_flag = _value(server, "team1", "notes")

    assert server.submit("team2", target_flag).reason == "WRONG_PHASE"
    server.test_status["phase"] = "ATTACK"
    r2 = server.submit("team2", target_flag)
    assert r2.accepted is True and r2.reason == "ACCEPTED"


# ── setup snapshot ────────────────────────────────────────────────────


def test_emit_planted_setup_records_one_per_planted_store():
    ch = _StubChallenge()
    server, tl = _server({("team1", "notes"): ch, ("team2", "notes"): ch})
    server.plant_initial_flags()
    before = len([e for e in tl.history() if e.type == EventType.FLAG_PLANTED.value])
    server.emit_planted_setup()
    after = len([e for e in tl.history() if e.type == EventType.FLAG_PLANTED.value])
    assert after - before == 2
    for e in tl.history():
        if e.type == EventType.FLAG_PLANTED.value:
            assert e.round_id == 0
