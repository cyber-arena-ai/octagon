"""Match-scoped baseline principals: each attacker gets its OWN application
identity on every opponent service, minted once at `/flags/configure` and fixed
for the match while objectives rotate around it.

Same fakes as `test_flag_service.py` (no real SSH/prod): the host is stubbed with
httpx.MockTransport, the exec port and the flag_handler are fakes.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from challenges.interface import (
    BaselinePrincipal,
    FlagObservation,
    ObservationStatus,
    SingleFlagHandler,
)
from facility.ports import ExecResult
from flag.service_app import build_flag_service_app

HDR_FAC = {"X-Facility-Token": "fac"}
TEAMS = ("team1", "team2", "team3")

# How much of the seed a handler turns into its password — tests read the
# recorded seeds back to assert the credential is derived from them.
_PW_LEN = 24


class _FakeExec:
    def exec(self, team_id, role, cmd, *, timeout_s=120.0):
        return ExecResult(exit_code=0, stdout="", stderr="")


class _PrincipalHandler(SingleFlagHandler):
    """Opt-in handler: mints one baseline principal per attacker, derived from
    that attacker's seed (never from its team id)."""

    name = "fake-principals"

    def __init__(self, calls: list):
        self.calls = calls

    def plant_one(self, target, flag):
        # flag-id rotates with the flag, principals must NOT.
        return f"rec-{flag[5:13]}#{flag}"

    def flag_id(self, handle):
        return handle.split("#", 1)[0]

    def retrieve(self, target, handle, expected=None):
        return FlagObservation(ObservationStatus.NOT_FOUND)

    def provision_principals(self, target, seeds):
        self.calls.append((target.meta["team_id"], dict(seeds)))
        return {
            attacker: BaselinePrincipal(
                principal_id=f"user-{seed[:8]}",
                credentials={"username": f"user-{seed[:8]}",
                             "password": seed[8:8 + _PW_LEN]},
            )
            for attacker, seed in seeds.items()
        }


class _OptOutHandler(SingleFlagHandler):
    """Anonymous or self-registering handler: the default hook is a no-op."""

    name = "fake-opt-out"

    def __init__(self, calls: list):
        self.calls = calls

    def plant_one(self, target, flag):
        return f"rec-{flag[5:13]}#{flag}"

    def flag_id(self, handle):
        return handle.split("#", 1)[0]

    def retrieve(self, target, handle, expected=None):
        return FlagObservation(ObservationStatus.NOT_FOUND)


def _service(handler_cls=_PrincipalHandler, status=None):
    """(TestClient, calls, clock) over a 3-team, single-service deployment."""
    calls: list = []
    clock = status if status is not None else {"phase": "LIVE", "round": 1}

    def host(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/game/status":
            return httpx.Response(200, json=dict(clock))
        if req.url.path == "/facility/events":
            return httpx.Response(200, json={"ok": True, "recorded": True})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(host))
    app = build_flag_service_app(
        gm_base_url="http://host", facility_token="fac", client=client,
        exec_port=_FakeExec(), probe_loader=lambda package: handler_cls(calls),
    )
    return TestClient(app), calls, clock


def _configure(tc):
    return tc.post("/flags/configure", headers=HDR_FAC, json={
        "tokens": {f"tok{i}": t for i, t in enumerate(TEAMS, start=1)},
        "hosts": {t: f"{t}_prod" for t in TEAMS},
        "deployment": [
            {"team_id": t, "service": "notes", "package": "notes",
             "ports": {"service": 5000},
             "flag_store_specs": [
                 {"name": "default", "kind": "read", "scope": None},
             ]}
            for t in TEAMS
        ],
    })


def _info(tc, token):
    return tc.post("/game/attack-info", headers={"X-Team-Token": token}).json()


# ── setup: one batch per victim, distinct bundles, requester scoping ──────────


def test_each_attacker_gets_a_distinct_bundle_and_sees_only_its_own_rows():
    tc, _, _ = _service()
    _configure(tc)

    p2 = {(r["team_id"], r["service"]): r for r in _info(tc, "tok2")["principals"]}
    p3 = {(r["team_id"], r["service"]): r for r in _info(tc, "tok3")["principals"]}
    # opponents only — team2 sees team1 + team3, never itself
    assert set(p2) == {("team1", "notes"), ("team3", "notes")}
    assert set(p3) == {("team1", "notes"), ("team2", "notes")}
    # on the SAME victim, the two attackers hold different identities + secrets
    a, b = p2[("team1", "notes")], p3[("team1", "notes")]
    assert a["principal_id"] != b["principal_id"]
    assert a["credentials"] != b["credentials"]
    assert a["host"] == "team1_prod"
    assert a["credentials"]["password"] and a["credentials"]["username"]
    # nobody else's bundle leaks into a requester's view
    assert b["credentials"]["password"] not in json.dumps(_info(tc, "tok2"))


def test_victim_is_excluded_and_the_handler_sees_the_whole_attacker_set():
    tc, calls, _ = _service()
    _configure(tc)
    by_victim = {victim: seeds for victim, seeds in calls}
    assert set(by_victim) == set(TEAMS)
    for victim, seeds in by_victim.items():
        # one batch call per victim, carrying every opponent and no self-entry
        assert set(seeds) == set(TEAMS) - {victim}
    # seed material is independent per assignment: no reuse anywhere, and
    # nothing derivable from a team id
    all_seeds = [s for _, seeds in calls for s in seeds.values()]
    assert len(set(all_seeds)) == len(all_seeds) == 6
    assert all(len(s) == 64 and not any(t in s for t in TEAMS) for s in all_seeds)


def test_principals_survive_objective_rotation():
    clock = {"phase": "LIVE", "round": 1}
    tc, _, _ = _service(status=clock)
    _configure(tc)
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    first = _info(tc, "tok2")

    clock["round"] = 2
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 2})
    second = _info(tc, "tok2")

    # objectives rotated…
    ids = lambda info: {(r["team_id"], r["flag_id"]) for r in info["targets"]}  # noqa: E731
    assert ids(first) and ids(first) != ids(second)
    # …the match-long principals did not
    assert first["principals"] == second["principals"]


def test_opted_out_handler_yields_no_principals_and_leaves_targets_untouched():
    tc, _, _ = _service(handler_cls=_OptOutHandler)
    assert _configure(tc).status_code == 200
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    info = _info(tc, "tok2")
    assert info["principals"] == []
    # a targets-only caller (the direct scripted verifier reads
    # `info.get("targets")`) sees exactly what it saw before
    assert {r["team_id"] for r in info["targets"]} == {"team1", "team3"}
    assert all(set(r) >= {"service", "host", "store", "flag_id", "round_id"}
               for r in info["targets"])


def test_credentials_never_ride_on_a_target_row():
    tc, _, _ = _service()
    _configure(tc)
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    info = _info(tc, "tok2")
    issued = [r["credentials"]["password"] for r in info["principals"]]
    blob = json.dumps(info["targets"])
    assert issued and not any(s in blob for s in issued)


# ── bad handler output must prevent publication ──────────────────────────────


_LEAKY = "s3cret-should-never-be-logged"
_SENTINEL_KEY = "attacker-key-carrying-s3cret"


def _partial(seeds):
    return {sorted(seeds)[0]: BaselinePrincipal("u", {"password": _LEAKY})}


def _unexpected_key(seeds):
    out = {a: BaselinePrincipal(f"u-{a}", {"password": f"pw-{a}"}) for a in seeds}
    out[_SENTINEL_KEY] = BaselinePrincipal("u-x", {"password": "pw-x"})
    return out


def _empty_id(seeds):
    return {a: BaselinePrincipal("", {"password": _LEAKY}) for a in seeds}


def _empty_creds(seeds):
    return {a: BaselinePrincipal(f"u-{a}", {}) for a in seeds}


def _dup_id(seeds):
    return {a: BaselinePrincipal("same-user", {"password": f"{_LEAKY}-{a}"})
            for a in seeds}


def _dup_creds(seeds):
    return {a: BaselinePrincipal(f"u-{a}", {"password": _LEAKY}) for a in seeds}


def _dup_creds_across_victims(seeds):
    # Derived from the attacker alone, so team2's login on team1 is byte-identical
    # to its login on team3 — caught only by the match-wide registry.
    return {a: BaselinePrincipal(f"user-{a}", {"username": f"user-{a}",
                                               "password": "pw-shared"})
            for a in seeds}


def _raises(seeds):
    raise RuntimeError(f"boom while creating account with password {_LEAKY}")


def _returns_none(seeds):
    return None


def _handler(make):
    """A handler whose provision_principals returns/raises whatever `make` does."""
    class _Bad(_PrincipalHandler):
        def provision_principals(self, target, seeds):
            self.calls.append((target.meta["team_id"], dict(seeds)))
            return make(seeds)
    return _Bad


@pytest.mark.parametrize(
    "make", [_partial, _unexpected_key, _empty_id, _empty_creds, _dup_id,
             _dup_creds, _dup_creds_across_victims, _raises, _returns_none],
    ids=["partial", "unexpected-key", "empty-id", "empty-creds", "duplicate-id",
         "duplicate-creds", "duplicate-creds-across-victims", "handler-raised",
         "returned-none"])
def test_bad_provisioning_fails_setup_without_publishing_or_logging_secrets(
    make, caplog,
):
    tc, _, _ = _service(handler_cls=_handler(make))
    with caplog.at_level(logging.DEBUG):
        r = _configure(tc)
    # setup fails loudly — GameMgr's _flag_service_post turns this into a
    # RuntimeError and the match never goes live
    assert r.status_code == 500 and r.json()["error"] == "principal provisioning failed"
    # …and NOTHING was published: the service is still unconfigured
    assert tc.get("/healthz").json()["configured"] is False
    assert _info(tc, "tok2") == {"targets": [], "principals": []}
    assert tc.post("/game/flag/submit", json={"flag": "x"},
                   headers={"X-Team-Token": "tok2"}).json()["reason"] == "ERROR"
    # the failure path never echoes a credential, nor a key the handler invented
    # (it could itself carry one — it is reported as a count)
    for secret in (_LEAKY, "pw-shared", _SENTINEL_KEY):
        assert secret not in caplog.text and secret not in r.text


# ── delivery is authenticated, opponent-scoped and live-only ─────────────────


def test_unconfigured_outside_live_and_bad_token_are_secret_safe():
    clock = {"phase": "LIVE", "round": 1}
    tc, _, _ = _service(status=clock)
    # before configure: both arrays empty, no error oracle
    assert tc.post("/game/attack-info").json() == {"targets": [], "principals": []}
    _configure(tc)
    tc.post("/flags/rotate", headers=HDR_FAC, json={"round_id": 1})
    issued = _info(tc, "tok2")["principals"][0]["credentials"]["password"]

    # no token / unknown token -> BAD_TOKEN, never a bundle
    for headers in ({}, {"X-Team-Token": "nope"}):
        body = tc.post("/game/attack-info", headers=headers).json()
        assert body["error"] == "BAD_TOKEN" and "principals" not in body
        assert issued not in json.dumps(body)

    # outside a live attack window nothing is handed out at all
    clock["phase"] = "DEFENSE"
    assert _info(tc, "tok2") == {"targets": [], "principals": []}
