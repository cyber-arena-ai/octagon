"""Tests for `challenges.check.game_phase` — the mini-orchestration phase.

Covers the pure parts: config generation, the scripted-attacker body, the
YAML round-trip, event assertions, event parsing, and challenge resolution.
The end-to-end driver (`run_mini_game`) brings up real containers via
`main.py start`, so it needs Docker + image builds and is validated manually,
not here (same policy as the --build phase).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from challenges.check import game_phase, resolve
from challenges.check.report import Report, Status
from challenges.interface import FlagStoreSpec, OracleKind, VulboxImage


# ── helpers ───────────────────────────────────────────────────────────


def _fake_ch(ports: dict[str, int], ref: str = "cyberarena/chal-fake:v1",
             specs: list[FlagStoreSpec] | None = None):
    """A duck-typed Challenge: config gen only reads `ch.vulbox`."""
    declared = [FlagStoreSpec("default")] if specs is None else specs
    return SimpleNamespace(
        vulbox=VulboxImage(reference=ref, exposed_ports=ports),
        flag_store_specs=lambda: list(declared),
    )


def _cfg(name: str = "fake-chal", ports: dict[str, int] | None = None,
         specs: list[FlagStoreSpec] | None = None, **kw):
    """build_game_config with the per-run ports + subnet octet filled in (free in production)."""
    kw.setdefault("api_port", 8000)
    kw.setdefault("flag_port", 9001)
    kw.setdefault("health_port", 9201)
    kw.setdefault("subnet_octet", 100)
    return game_phase.build_game_config(
        name, _fake_ch(ports or {"service": 4265}, specs=specs), **kw)


def _healthy_events() -> list[dict]:
    """A timeline a clean 2-team / 1-round match would produce — only the
    challenge-relevant events the checker asserts on."""
    return (
        [
            {"type": "SERVICE_DEPLOYED", "payload": {"service": "c", "container": "team1_prod"}},
            {"type": "SERVICE_DEPLOYED", "payload": {"service": "c", "container": "team2_prod"}},
        ]
        + [
            {"type": "SERVICE_HEALTH_CHECK", "team_id": f"team{t}",
             "payload": {"level": lvl, "passed": True}}
            for t in (1, 2)
            for lvl in ("process", "tcp", "checker")
        ]
        + [{"type": "FLAG_PLANTED", "payload": {"service": "c"}}] * 2
        # async (the default) attacks at LIVE; sync attacks at ATTACK.
        + [{"type": "FLAG_SUBMITTED", "phase": "LIVE", "payload": {"accepted": False}}]
    )


def _fails(rep: Report) -> list[str]:
    return [c.name for c in rep.checks if c.status is Status.FAIL]


# ── config generation ─────────────────────────────────────────────────


def test_build_config_shape():
    cfg = _cfg("fake-chal", {"service": 4265})

    # two scripted teams, both deploying the challenge
    assert [t["agent"]["provider"] for t in cfg["teams"]] == ["script", "script"]
    assert cfg["challenges"][0]["deploy_on"] == ["team1", "team2"]
    assert cfg["challenges"][0]["name"] == "fake-chal"
    assert cfg["challenges"][0]["port"] == 4265

    # v3: dev runs the harness-less agent base image (scripted teams need no CLI);
    # the off-agent facilities are wired as nodes
    nodes = {n["id"]: n for n in cfg["nodes"]}
    assert nodes["team1_dev"]["image"] == "cyberarena/agent-base"
    assert "flag_service" in nodes and "health_poller" in nodes
    assert cfg["flag_service"]["container"] == "flag_service"
    assert cfg["health_poller"]["container"] == "health_poller"

    # async (the default), single short round bounded by one flag-rotation tick
    assert cfg["round_config"]["total_rounds"] == 1
    assert cfg["round_config"]["mode"] == "async"
    assert cfg["round_config"]["round_secs"] == game_phase.ROUND_SECS


def test_build_config_sync_escape_hatch():
    """`mode="sync"` emits the strict DEFENSE→ATTACK round_config instead."""
    cfg = _cfg("fake-chal", {"service": 80}, mode="sync")
    rc = cfg["round_config"]
    assert "mode" not in rc  # sync is main.py's default, so left implicit
    assert rc["defense_secs"] == game_phase.DEFENSE_SECS
    assert rc["attack_secs"] == game_phase.ATTACK_SECS
    assert rc["total_rounds"] == 1

    # v3 topology: two internal team nets + a shared internal attack net, plus a
    # NON-internal egress_net the off-agent facilities use to reach GameMgr.
    assert set(cfg["networks"]) == {"team1_net", "team2_net", "attack_net", "egress_net"}
    assert all(cfg["networks"][n]["internal"]
               for n in ("team1_net", "team2_net", "attack_net"))
    assert cfg["networks"]["egress_net"].get("internal") is not True
    prods = [n for n in cfg["nodes"] if n["id"].endswith("_prod")]
    assert all(n["image"] == "cyberarena/chal-fake:v1" for n in prods)
    # only the off-agent facilities publish a host port (GameMgr reaches them);
    # the team dev/prod nodes don't — the cross-team attack is on-net.
    ported = {n["id"] for n in cfg["nodes"] if n.get("ports")}
    assert ported == {"flag_service", "health_poller"}


def test_build_config_namespace_shifts_subnets():
    """`_subnet_octet` maps a namespace to a per-run octet (run_mini_game probes
    it free, then passes it as `subnet_octet`); that octet shifts the config's
    subnets out of the bundled 10.0-10.12 block. Names stay bare — the
    orchestrator namespaces them at runtime via `--namespace`."""
    assert game_phase._subnet_octet("chk777") == 177  # 100 + 777 % 100
    cfg = _cfg("fake-chal", {"service": 4265}, subnet_octet=177)

    # names stay bare (the orchestrator namespaces them at runtime via --namespace)
    assert {n["id"] for n in cfg["nodes"]} >= {"team1_dev", "team1_prod", "flag_service"}
    assert set(cfg["networks"]) == {"team1_net", "team2_net", "attack_net", "egress_net"}
    for t in cfg["teams"]:
        assert t["id"] in ("team1", "team2")

    # node network refs resolve to declared networks
    declared = set(cfg["networks"])
    for n in cfg["nodes"]:
        assert set(n["networks"]) <= declared

    # subnets clear of the bundled 10.0-10.12 block
    octets = {net["subnet"].split(".")[1] for net in cfg["networks"].values()}
    assert octets == {"177"}  # 100 + 777 % 100
    assert cfg["challenges"][0]["deploy_on"] == ["team1", "team2"]


def test_build_config_cross_team_topology():
    """Both prods + both devs share the single attack_net, so each dev can reach
    the OPPOSING prod; the facilities sit on the team nets to plant/probe prod."""
    cfg = _cfg("fake-chal", {"service": 80})
    nodes = {n["id"]: n for n in cfg["nodes"]}
    for nid in ("team1_dev", "team1_prod", "team2_dev", "team2_prod"):
        assert "attack_net" in nodes[nid]["networks"]
    # facilities sit on both team nets (to plant/probe prod) + the egress net
    # (to reach GameMgr on the host); the agents never touch egress.
    assert set(nodes["flag_service"]["networks"]) == {"team1_net", "team2_net", "egress_net"}
    assert "egress_net" not in nodes["team1_dev"]["networks"]


def test_pick_port_prefers_service():
    assert game_phase.pick_port(_fake_ch({"service": 4265, "admin": 9000})) == 4265


def test_pick_port_single_fallback():
    assert game_phase.pick_port(_fake_ch({"http": 8080})) == 8080


def test_pick_port_ambiguous_raises():
    with pytest.raises(ValueError, match="single primary port"):
        game_phase.pick_port(_fake_ch({"a": 1, "b": 2}))


# ── scripted attacker body ────────────────────────────────────────────


def test_script_body_bakes_port_and_compiles():
    body = game_phase.script_body(4265)
    assert "PORT = 4265" in body
    assert "create_connection" in body  # the cross-team reach
    assert "CYBERARENA_SUBMIT_URL" in body  # the submit rail
    # the inner python (between the heredoc markers) must be valid
    inner = body.split("<<'PYEOF'", 1)[1].split("PYEOF", 1)[0]
    compile(inner, "<probe>", "exec")


def test_script_body_enables_packaged_pov():
    body = game_phase.script_body(4265, proof_required=True)
    assert "PROOF_REQUIRED = True" in body
    assert "CYBERARENA_EXPLOIT_PATH" in body
    assert "CYBERARENA_VERIFICATION_ATTEMPT_ID" in body
    assert game_phase.RAIL_ATTEMPT_PREFIX in body
    assert "X-CyberArena-Verification-Attempt" in body


def test_build_config_runs_packaged_pov_when_requested():
    default_cfg = _cfg()
    assert "PROOF_REQUIRED = False" in default_cfg["teams"][0]["agent"]["cli"]

    read_cfg = _cfg(proof_required=True)
    assert "PROOF_REQUIRED = True" in read_cfg["teams"][0]["agent"]["cli"]


def test_exploit_path_detects_only_adjacent_poc(tmp_path, monkeypatch):
    challenge_py = tmp_path / "challenge.py"
    challenge_py.write_text("")
    monkeypatch.setattr(game_phase.inspect, "getfile", lambda _cls: str(challenge_py))
    ch = _fake_ch({"service": 4265})
    assert game_phase._exploit_path(ch) is None

    exploit = tmp_path / "_exploit.py"
    exploit.write_text("print('proof')\n")
    assert game_phase._exploit_path(ch) == exploit


def test_script_body_is_ascii():
    # non-ASCII in the body forces PyYAML out of literal-block style (and is
    # needless in a script shipped into a container).
    game_phase.script_body(80).encode("ascii")


# ── YAML round-trip ───────────────────────────────────────────────────


def test_yaml_round_trips_and_uses_literal_block():
    cfg = _cfg("fake-chal", {"service": 4265})
    text = game_phase.yaml_dump(cfg)
    assert yaml.safe_load(text) == cfg
    assert "    cli: |" in text  # readable literal block, not an escaped scalar


# ── event assertions ──────────────────────────────────────────────────


def test_assert_events_passes_on_healthy_timeline():
    rep = Report(challenge="x")
    game_phase.assert_events(_healthy_events(), rep)
    assert rep.ok, _fails(rep)
    # only challenge-scoped checks — no infra milestones
    assert {c.group for c in rep.checks} == {"challenge"}


def test_assert_events_requires_one_flag_per_team_and_store():
    rep = Report(challenge="x")
    game_phase.assert_events(_healthy_events(), rep, n_stores=2)
    assert "flag plant (live)" in _fails(rep)

    events = _healthy_events() + [
        {"type": "FLAG_PLANTED", "payload": {"service": "c"}},
        {"type": "FLAG_PLANTED", "payload": {"service": "c"}},
    ]
    rep = Report(challenge="x")
    game_phase.assert_events(events, rep, n_stores=2)
    assert "flag plant (live)" not in _fails(rep)


def test_assert_events_empty_is_harness_failure():
    rep = Report(challenge="x")
    game_phase.assert_events([], rep)
    assert "match ran" in _fails(rep)


def test_assert_events_fails_without_attack_submit():
    """Reachability: no FLAG_SUBMITTED in the attack window ⇒ service wasn't reached."""
    events = [e for e in _healthy_events() if e["type"] != "FLAG_SUBMITTED"]
    rep = Report(challenge="x")
    game_phase.assert_events(events, rep)
    assert "service reachable cross-team" in _fails(rep)


def test_assert_events_submit_in_defense_does_not_count():
    """A submit recorded during DEFENSE doesn't prove cross-team reach."""
    events = [
        e for e in _healthy_events() if e["type"] != "FLAG_SUBMITTED"
    ] + [{"type": "FLAG_SUBMITTED", "phase": "DEFENSE", "payload": {"accepted": False}}]
    rep = Report(challenge="x")
    game_phase.assert_events(events, rep)
    assert "service reachable cross-team" in _fails(rep)


def test_assert_events_reachable_keys_on_mode_phase():
    """async keys on a LIVE submit; sync keys on an ATTACK submit — and each
    rejects the other mode's window."""
    base = [e for e in _healthy_events() if e["type"] != "FLAG_SUBMITTED"]
    live = base + [{"type": "FLAG_SUBMITTED", "phase": "LIVE", "payload": {"accepted": False}}]
    attack = base + [{"type": "FLAG_SUBMITTED", "phase": "ATTACK", "payload": {"accepted": False}}]

    # async accepts LIVE, rejects ATTACK-only
    rep = Report(challenge="x"); game_phase.assert_events(live, rep, mode="async")
    assert "service reachable cross-team" not in _fails(rep)
    rep = Report(challenge="x"); game_phase.assert_events(attack, rep, mode="async")
    assert "service reachable cross-team" in _fails(rep)

    # sync accepts ATTACK, rejects LIVE-only
    rep = Report(challenge="x"); game_phase.assert_events(attack, rep, mode="sync")
    assert "service reachable cross-team" not in _fails(rep)
    rep = Report(challenge="x"); game_phase.assert_events(live, rep, mode="sync")
    assert "service reachable cross-team" in _fails(rep)


def test_assert_events_fails_on_single_team_deploy():
    events = [e for e in _healthy_events() if e["type"] != "SERVICE_DEPLOYED"]
    events.append({"type": "SERVICE_DEPLOYED", "payload": {"service": "c"}})  # one team only
    rep = Report(challenge="x")
    game_phase.assert_events(events, rep)
    assert "service deploys" in _fails(rep)


def test_assert_events_fails_when_service_never_healthy():
    """Service deployed but the live functionality_test never went green."""
    events = [e for e in _healthy_events() if e["type"] != "SERVICE_HEALTH_CHECK"]
    events += [
        {"type": "SERVICE_HEALTH_CHECK", "team_id": "team1",
         "payload": {"level": "tcp", "passed": False}},
        {"type": "SERVICE_HEALTH_CHECK", "team_id": "team2",
         "payload": {"level": "tcp", "passed": False}},
    ]
    rep = Report(challenge="x")
    game_phase.assert_events(events, rep)
    assert "service healthy (live functionality_test)" in _fails(rep)


def test_assert_events_health_uses_last_probe_per_dimension():
    """A dimension that fails early but passes by the end is healthy; the
    reverse (passes early, ends failing) is not. team2 stays green throughout
    so this isolates last-probe-wins from the per-team coverage requirement."""
    base = [e for e in _healthy_events() if e["type"] != "SERVICE_HEALTH_CHECK"]
    team2_ok = [{"type": "SERVICE_HEALTH_CHECK", "team_id": "team2",
                 "payload": {"level": "tcp", "passed": True}}]
    # team1/tcp recovers (fail → pass).
    recovering = base + team2_ok + [
        {"type": "SERVICE_HEALTH_CHECK", "team_id": "team1", "payload": {"level": "tcp", "passed": False}},
        {"type": "SERVICE_HEALTH_CHECK", "team_id": "team1", "payload": {"level": "tcp", "passed": True}},
    ]
    rep = Report(challenge="x")
    game_phase.assert_events(recovering, rep)
    assert "service healthy (live functionality_test)" not in _fails(rep)

    # team1/tcp degrades (pass → fail).
    degrading = base + team2_ok + [
        {"type": "SERVICE_HEALTH_CHECK", "team_id": "team1", "payload": {"level": "tcp", "passed": True}},
        {"type": "SERVICE_HEALTH_CHECK", "team_id": "team1", "payload": {"level": "tcp", "passed": False}},
    ]
    rep2 = Report(challenge="x")
    game_phase.assert_events(degrading, rep2)
    assert "service healthy (live functionality_test)" in _fails(rep2)


def test_assert_events_health_requires_both_teams_probed():
    """A team whose service never binds emits zero SERVICE_HEALTH_CHECK events;
    the healthy gate must FAIL, not pass on the surviving team's green probes."""
    base = [e for e in _healthy_events() if e["type"] != "SERVICE_HEALTH_CHECK"]
    only_team1 = base + [
        {"type": "SERVICE_HEALTH_CHECK", "team_id": "team1",
         "payload": {"level": lvl, "passed": True}}
        for lvl in ("process", "tcp", "checker")
    ]
    rep = Report(challenge="x")
    game_phase.assert_events(only_team1, rep)
    assert "service healthy (live functionality_test)" in _fails(rep)


def _claim_event(submitter: str, victim: str, *, accepted: bool = False,
                 reason: str = "NOT_OBSERVED") -> dict:
    return {
        "type": "FLAG_SUBMITTED",
        "phase": "LIVE",
        "payload": {
            "accepted": accepted,
            "reason": reason,
            "attempt_id": f"{game_phase.RAIL_ATTEMPT_PREFIX}{submitter}",
            "oracle": "claim",
            "submitter_team_id": submitter,
            "victim_team_id": victim,
            "service": "x",
            "store": "ledger",
        },
    }


def _rail_submit_event(submitter: str) -> dict:
    return {
        "type": "FLAG_SUBMITTED",
        "phase": "LIVE",
        "payload": {
            "accepted": False,
            "reason": "NOT_FOUND",
            "attempt_id": f"{game_phase.RAIL_ATTEMPT_PREFIX}{submitter}",
            "submitter_team_id": submitter,
        },
    }


def test_assert_events_write_rail_requires_negative_claims_without_false_positive():
    events = _healthy_events() + [
        _claim_event("team1", "team2"),
        _claim_event("team2", "team1"),
    ]
    rep = Report(challenge="x")
    game_phase.assert_events(events, rep, n_write=1)
    assert "write claim rail (live)" not in _fails(rep)

    rep = Report(challenge="x")
    game_phase.assert_events(
        events + [_claim_event("team1", "team2", accepted=True,
                               reason="ACCEPTED")],
        rep,
        n_write=1,
    )
    assert "write claim rail (live)" in _fails(rep)


def _proof_event(store: str, outcome: str, oracle: str, *,
                 accepted: bool = True, reason: str = "ACCEPTED",
                 correlated: bool = False, submitter: str = "team1") -> dict:
    event = {
        "type": "FLAG_SUBMITTED",
        "phase": "LIVE",
        "payload": {
            "accepted": accepted,
            "reason": reason,
            "submitter_team_id": submitter,
            "victim_team_id": "team2" if submitter == "team1" else "team1",
            "service": "x",
            "store": store,
            "outcome": outcome,
            "oracle": oracle,
        },
    }
    if correlated:
        event["payload"]["attempt_id"] = (
            f"{game_phase.PROOF_ATTEMPT_PREFIX}{submitter}")
    return event


def test_assert_events_accepts_proof_after_rail_without_requiring_poc_header():
    events = (
        _healthy_events()
        + [{"type": "FLAG_PLANTED", "payload": {"service": "c"}}] * 2
        + [_rail_submit_event("team1"), _claim_event("team1", "team2"),
           _rail_submit_event("team2"), _claim_event("team2", "team1"),
           _proof_event("ledger", "write", "claim"),
           _proof_event("shell", "execute", "submit", correlated=True)]
    )
    stores = {"ledger": "write", "shell": "execute"}
    rep = Report(challenge="x")
    game_phase.assert_events(
        events, rep, n_stores=2, n_write=1,
        proof_stores=stores, proof_available=True)
    assert "packaged PoV" not in _fails(rep)
    assert "write claim rail (live)" not in _fails(rep)

    rep = Report(challenge="x")
    game_phase.assert_events(
        events[:-1], rep, n_stores=2, n_write=1,
        proof_stores=stores, proof_available=True)
    assert "packaged PoV" not in _fails(rep)

    rep = Report(challenge="x")
    game_phase.assert_events(
        events[:-2], rep, n_stores=2, n_write=1,
        proof_stores=stores, proof_available=True)
    assert "packaged PoV" in _fails(rep)


def test_assert_events_accepts_execute_proof_without_poc_header_after_rail():
    rep = Report(challenge="x")
    game_phase.assert_events(
        _healthy_events()
        + [_rail_submit_event("team1"),
           _proof_event("shell", "execute", "submit")],
        rep,
        proof_stores={"shell": "execute"}, proof_available=True,
    )
    assert "packaged PoV" not in _fails(rep)


def test_assert_events_accepts_read_proof_after_rail():
    rep = Report(challenge="x")
    game_phase.assert_events(
        _healthy_events()
        + [_rail_submit_event("team1"),
           _proof_event("document", "read", "submit")],
        rep,
        proof_stores={"document": "read"}, proof_available=True,
    )
    assert "packaged PoV" not in _fails(rep)


@pytest.mark.parametrize(
    "event",
    [
        _proof_event("document", "execute", "submit"),
        _proof_event("unknown", "read", "submit"),
    ],
)
def test_assert_events_rejects_proof_outside_declared_objective_contract(event):
    rep = Report(challenge="x")
    game_phase.assert_events(
        _healthy_events() + [_rail_submit_event("team1"), event],
        rep,
        proof_stores={"document": "read"}, proof_available=True,
    )
    assert "packaged PoV" in _fails(rep)


def test_assert_events_does_not_accept_uncorrelated_proof_before_rail():
    rep = Report(challenge="x")
    game_phase.assert_events(
        _healthy_events()
        + [_proof_event("shell", "execute", "submit"),
           _rail_submit_event("team1")],
        rep,
        proof_stores={"shell": "execute"}, proof_available=True,
    )
    assert "packaged PoV" in _fails(rep)


def test_assert_events_reports_missing_packaged_pov_as_unverified_note():
    rep = Report(challenge="x")
    game_phase.assert_events(
        _healthy_events(), rep,
        proof_stores={"shell": "execute"}, proof_available=False)
    proof = next(c for c in rep.checks if c.name == "packaged PoV")
    assert proof.status is Status.NOTE
    assert "UNVERIFIED" in proof.detail


def test_rejected_proof_claim_cannot_substitute_for_negative_write_rail():
    rejected = _proof_event(
        "ledger", "write", "claim", accepted=False, reason="NOT_OBSERVED")
    rep = Report(challenge="x")
    game_phase.assert_events(_healthy_events() + [rejected, rejected], rep,
                             n_write=1)
    assert "write claim rail (live)" in _fails(rep)


# ── event parsing ─────────────────────────────────────────────────────


def test_parse_events_skips_blank_and_bad_lines(tmp_path):
    run_dir = tmp_path / "20260619T000000Z"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(
        '{"type": "ROUND_START"}\n'
        "\n"
        "not json\n"
        '{"type": "GAME_END"}\n'
    )
    events = game_phase.parse_events(run_dir)
    assert [e["type"] for e in events] == ["ROUND_START", "GAME_END"]


# ── resolution ────────────────────────────────────────────────────────


def test_resolve_unknown_challenge():
    name, ch, err = resolve.resolve_challenge("definitely-not-a-real-challenge")
    assert ch is None
    assert "unknown challenge" in err


def test_resolve_known_challenge():
    # `notes` is the dep-free framework challenge; always discoverable.
    name, ch, err = resolve.resolve_challenge("notes")
    assert err == ""
    assert name == "notes"
    assert ch is not None


def test_run_mini_game_reports_unknown_without_docker():
    """Resolution failure short-circuits before any Docker work."""
    rep = game_phase.run_mini_game("definitely-not-a-real-challenge")
    assert not rep.ok
    assert any(c.group == "resolve" for c in rep.checks)
