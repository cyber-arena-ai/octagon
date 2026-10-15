"""Phase 3 — async mini-orchestration (needs Docker + the agent image).

Run a real tiny 2-team / 1-round async match driven by a deterministic scripted
attacker (no LLM), then read the run's events.jsonl to confirm the challenge
behaves end-to-end in a live game:

  • deploys   — the image builds, the container comes up, the service deploys
  • healthy   — the live functionality_test goes green against the deploy
  • flag plant— flag_handler.plant works against the live container
  • reachable — an opponent connects to the service on its declared port and a
                submission round-trips (catches a service bound to 127.0.0.1
                that a localhost health check would miss)
  • write rail — each WRITE store is published and an unplaced claim comes back
                NOT_OBSERVED through the live facility
  • capability proof — when a challenge ships `_exploit.py`, run it and require
                       an accepted capture for at least one declared objective

The infra milestones (containers ready, facilities, the round/phase loop, agent
install) are deliberately NOT asserted — they fire the same for every challenge,
so they'd tell an author nothing about their own work. A harness that never runs
surfaces as "no run dir," not as a challenge failure.

The scripted attacker first retries a raw TCP connect, a tagged rejected sentinel
flag, and tagged unplaced WRITE claims. Once those negative rails pass, the
challenge's provisioned `_exploit.py` runs once. Accepted FLAG_SUBMITTED events
after that checker-owned boundary are the authoritative proof; a proof attempt id
is stronger optional correlation, not part of the ordinary PoC contract. Exploit
stdout is diagnostic only. With no PoC, semantic coverage is reported explicitly
as UNVERIFIED.

The match runs in async mode by default — one continuous LIVE phase with
cross-team networks always on, the same mode real game runs use — and the check
keys on a submission recorded during LIVE. Pass mode="sync" for the strict
DEFENSE->ATTACK match (investigation only, not part of the readiness gate).
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from challenges.interface import Challenge, OracleKind

from .docker_env import free_ports
from .report import Report, Status
from .resolve import resolve_challenge

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNS_DIR = REPO_ROOT / "runs"

# Tiny match: one round, short phases. 120s (not 60): a heavy service (CMS+DB, a
# big Node app) can take ~60s to become checker-healthy after its port binds, so
# the live functionality_test and the cross-team attack both need a window that
# outlasts that warmup.
ROUND_SECS = 120
DEFENSE_SECS = 25
ATTACK_SECS = 35
# How long the scripted attacker keeps retrying its connect+submit — a touch
# longer than one round, so the loop runs until the game-end kill stops it
# rather than self-terminating mid-round.
ATTACK_BUDGET_SECS = ROUND_SECS + 30
# Outer cap on the whole `main.py start` subprocess: a COLD challenge-image
# build (compile from source) dominates and can run minutes; bring-up + one
# round + teardown is well under a minute on a warm host.
GAME_TIMEOUT_SECS = 900.0
# Teardown is bounded separately and far tighter than the match: a hung
# teardown must not wedge the run (or an --all sweep) after the match finished.
TEARDOWN_TIMEOUT_SECS = 120.0
# How long to let the LIVE phase run before stopping the match ourselves. The
# scripted async agents loop forever (they never "finish"), so `main.py start`
# never self-terminates — we drive it through one LIVE window long enough for a
# heavy service to warm up + get probed/attacked, then stop it. The assertion
# events (deploy/health/plant/submit) are all in by then.
LIVE_WINDOW_SECS = 75.0
PROOF_ATTEMPT_PREFIX = "challenge-check-proof-"
RAIL_ATTEMPT_PREFIX = "challenge-check-rail-"


# ── generic scripted attacker ─────────────────────────────────────────

_SCRIPT_TEMPLATE = """\
cat > /tmp/check_game_probe.py <<'PYEOF'
import os, sys, socket, json, time, subprocess, urllib.request

PORT = __PORT__
CONNECT_TIMEOUT = 8
ATTACK_BUDGET = __BUDGET__
RETRY_INTERVAL = 5
PROOF_REQUIRED = __PROOF_REQUIRED__
PROOF_TIMEOUT = 45

prompt = sys.stdin.read()
team = os.environ.get("CYBERARENA_TEAM_ID", "?")
rail_attempt_id = "__RAIL_ATTEMPT_PREFIX__" + team
print(f"[check_game team={team}] phase prompt: {prompt[:60]!r}", flush=True)

targets = [h for h in os.environ.get("CYBERARENA_OPPOSING_PRODS", "").split(",") if h]
submit_url = os.environ.get("CYBERARENA_SUBMIT_URL", "")
token = os.environ.get("CYBERARENA_TEAM_TOKEN", "")
flag = "FLAG{cyberarena-check-game-probe}"
flag_base = submit_url.rsplit("/game/flag/submit", 1)[0]
attack_info_url = flag_base + "/game/attack-info"
claim_url = flag_base + "/game/flag/claim"

def post_json(url, body, timeout=CONNECT_TIMEOUT, attempt_id=""):
    headers = {"X-Team-Token": token, "content-type": "application/json"}
    if attempt_id:
        headers["X-CyberArena-Verification-Attempt"] = attempt_id
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())

def probe_write(host):
    # Claim every published WRITE target WITHOUT placing the mark. The only
    # correct answer is NOT_OBSERVED; an ACCEPTED here would mean the rail
    # credits an attacker who did nothing.
    try:
        _, info = post_json(attack_info_url, {})
        rows = info.get("targets") or []
    except Exception as e:
        print(f"[check_game] ATTACK_INFO_ERR {e!r}", flush=True)
        return False
    ok = True
    for row in rows:
        if row.get("oracle") != "claim" or row.get("host") != host:
            continue
        body = {
            "victim_team_id": row.get("team_id"),
            "service": row.get("service"),
            "store": row.get("store"),
        }
        try:
            status, result = post_json(
                claim_url, body, timeout=30, attempt_id=rail_attempt_id)
            print(f"[check_game] CLAIM_OK {status} {result}", flush=True)
            ok = ok and result.get("reason") == "NOT_OBSERVED"
        except Exception as e:
            print(f"[check_game] CLAIM_ERR {e!r}", flush=True)
            ok = False
    return ok

def attack(host, probe_claims=True):
    # Cross-team reach to the opponent's service on its declared port.
    try:
        with socket.create_connection((host, PORT), timeout=CONNECT_TIMEOUT):
            pass
    except Exception as e:
        print(f"[check_game] REACH_FAIL {host}:{PORT} {e!r}", flush=True)
        return False
    print(f"[check_game] REACH_OK {host}:{PORT}", flush=True)
    # Exercise the submit rail with a sentinel flag. It is wrong on purpose
    # (we never solve the challenge), so the host rejects it; the check asserts
    # the FLAG_SUBMITTED event fired, not that it was accepted.
    try:
        req = urllib.request.Request(
            submit_url,
            data=json.dumps({"flag": flag}).encode(),
            headers={
                "X-Team-Token": token,
                "X-CyberArena-Verification-Attempt": rail_attempt_id,
                "content-type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT)
        print(f"[check_game] SUBMIT_OK {resp.status} {resp.read().decode()[:120]}", flush=True)
    except Exception as e:
        print(f"[check_game] SUBMIT_ERR {e!r}", flush=True)
    return probe_write(host) if probe_claims else True

def run_proof():
    path = os.environ.get("CYBERARENA_EXPLOIT_PATH", "")
    if not path or not os.path.isfile(path):
        print(f"[check_game] PROOF_UNAVAILABLE {path!r}", flush=True)
        return
    attempt_id = "__PROOF_ATTEMPT_PREFIX__" + team
    env = dict(os.environ)
    env["CYBERARENA_VERIFICATION_ATTEMPT_ID"] = attempt_id
    try:
        proc = subprocess.run(
            [sys.executable, path], cwd=os.path.dirname(path) or ".", env=env,
            capture_output=True, text=True, timeout=PROOF_TIMEOUT,
        )
        print(
            f"[check_game] PROOF_DONE rc={proc.returncode} "
            f"stdout={proc.stdout[-300:]!r} stderr={proc.stderr[-300:]!r}",
            flush=True,
        )
    except Exception as e:
        print(f"[check_game] PROOF_ERR {e!r}", flush=True)

# Keep attacking until the budget elapses (the game-end kill usually stops us
# first). Staying alive past the supervisor's fast-exit threshold avoids a
# premature give-up; retrying rides out a still-booting opponent service.
deadline = time.monotonic() + ATTACK_BUDGET
proof_done = not PROOF_REQUIRED
while time.monotonic() < deadline:
    rail_ok = True
    for host in targets:
        rail_ok = attack(host, probe_claims=not proof_done) and rail_ok
    if not proof_done and rail_ok:
        run_proof()
        proof_done = True
    time.sleep(RETRY_INTERVAL)

print("[check_game] done", flush=True)
sys.exit(0)
PYEOF
exec python3 /tmp/check_game_probe.py
"""


def script_body(port: int, *, proof_required: bool = False) -> str:
    """The generic scripted-attacker `cli:` body for the given service port."""
    return (
        _SCRIPT_TEMPLATE
        .replace("__PORT__", str(int(port)))
        .replace("__BUDGET__", str(int(ATTACK_BUDGET_SECS)))
        .replace("__PROOF_REQUIRED__", "True" if proof_required else "False")
        .replace("__PROOF_ATTEMPT_PREFIX__", PROOF_ATTEMPT_PREFIX)
        .replace("__RAIL_ATTEMPT_PREFIX__", RAIL_ATTEMPT_PREFIX)
    )


# ── config generation ─────────────────────────────────────────────────


def pick_port(ch: Challenge) -> int:
    """The single service port to probe across teams.

    Prefers the conventional "service" port; falls back to the sole exposed
    port. A multi-port challenge with no "service" entry is ambiguous for this
    generic check, so it's a hard error (raise, surfaced as a FAIL upstream).
    """
    ports = ch.vulbox.exposed_ports
    if "service" in ports:
        return int(ports["service"])
    if len(ports) == 1:
        return int(next(iter(ports.values())))
    raise ValueError(
        f"challenge exposes {len(ports)} ports {dict(ports)} and none is named "
        f"'service'; mini-orchestration needs a single primary port"
    )


def _subnet_octet(namespace: str) -> int:
    """A second octet in 100-199 derived from the namespace, clear of the
    bundled configs' 10.0-10.12 block. Distinct namespaces are unlikely to
    collide; two concurrent runs that do would just fail fast at network
    create rather than touch each other's nets."""
    digits = "".join(c for c in namespace if c.isdigit()) or "0"
    return 100 + int(digits) % 100


def _used_second_octets() -> set[int]:
    """Second octets of every `10.x.0.0/16` docker network already on this host.
    Best-effort: any docker hiccup (or no docker) yields an empty set, so the
    caller just falls back to its namespace-derived octet."""
    try:
        import docker
        nets = docker.from_env().networks.list()
    except Exception:  # noqa: BLE001 — probing is advisory, never fatal
        return set()
    used: set[int] = set()
    for n in nets:
        for cfg in ((n.attrs or {}).get("IPAM") or {}).get("Config") or []:
            bits = (cfg.get("Subnet") or "").split(".")
            if len(bits) >= 2 and bits[0] == "10":
                try:
                    used.add(int(bits[1]))
                except ValueError:
                    pass
    return used


def _free_subnet_octet(namespace: str) -> int:
    """A second octet in 100-199 for this run's /24s that no existing docker
    network already uses — so a --game check runs correctly beside a live arena
    OR another concurrent sweep worker. Seeded by the namespace (so the scan
    starts where _subnet_octet would have), then walks forward past any octet
    in use. Falls back to the bare seed if all 100 are taken (absurd) or docker
    can't be queried."""
    seed = _subnet_octet(namespace)
    used = _used_second_octets()
    for i in range(100):
        cand = 100 + (seed - 100 + i) % 100
        if cand not in used:
            return cand
    return seed


def _facility_env(api_port: int) -> dict:
    """Env both off-agent facility containers need: where GameMgr lives (on the
    host), the shared facility<->GameMgr token, and the injected SSH key path."""
    return {
        "CYBERARENA_GM_BASE_URL": f"http://host.docker.internal:{api_port}",
        "CYBERARENA_FACILITY_TOKEN": "${CYBERARENA_FACILITY_TOKEN}",
        "CYBERARENA_SSH_KEY_PATH": "/keys/facility",
    }


def build_game_config(
    name: str, ch: Challenge, *, api_port: int, flag_port: int,
    health_port: int, subnet_octet: int, mode: str = "async",
    proof_required: bool = False,
) -> dict:
    """Synthesize a minimal 2-team / 1-round v3 match config (scripted, no LLM).

    v3 topology: each team runs a dev (cyberarena/agent-base — the shared agent
    tooling image with no harness CLI, enough for the scripted attacker)
    and a prod (the challenge image); the flag-service and health-poller run as
    OFF-agent containers that plant/probe prod over SSH and report to GameMgr on
    the host. Both teams attack the opposing prod, so one round produces a
    cross-team attack each way.

    Team nets are `internal` (clean isolation + clean termination); a shared
    `attack_net` carries cross-team prod reach. The off-agent facilities also
    join a NON-internal `egress_net` so they can reach GameMgr on the host
    (host.docker.internal) to fetch status and post health/plant/submit events;
    the agents (dev/prod) stay internal-only. The dev submits straight to the
    on-net flag-service (run_mini_game points CYBERARENA_PUBLIC_BASE_URL at it),
    so the agent needs no host route. Facility host ports and the GameMgr
    `api_port` are distinct per-run free ports, and subnets use the caller's
    `subnet_octet`, so parallel checks don't collide.

    `mode` is "async" (the gate — one continuous LIVE phase) or "sync"
    (investigation only). Validated here so a typo fails before image builds.
    """
    if mode not in ("async", "sync"):
        raise ValueError(f"mode must be 'async' or 'sync', got {mode!r}")
    port = pick_port(ch)
    image = ch.vulbox.reference  # == build.py:build_image() output
    body = script_body(port, proof_required=proof_required)
    octet = subnet_octet

    teams = [
        {
            "id": f"team{i}",  # logical id (tokens / identity); NOT a container name
            "dev": f"team{i}_dev",
            "prod": f"team{i}_prod",
            "token": f"dev-team{i}-token",
            "agent": {"provider": "script", "cli": body},
        }
        for i in (1, 2)
    ]
    nodes = [
        {"id": "team1_dev", "image": "cyberarena/agent-base",
         "networks": ["team1_net", "attack_net"],
         "extra_hosts": {"host.docker.internal": "host-gateway"}},
        {"id": "team1_prod", "image": image, "networks": ["team1_net", "attack_net"]},
        {"id": "team2_dev", "image": "cyberarena/agent-base",
         "networks": ["team2_net", "attack_net"],
         "extra_hosts": {"host.docker.internal": "host-gateway"}},
        {"id": "team2_prod", "image": image, "networks": ["team2_net", "attack_net"]},
        {"id": "flag_service", "image": "cyberarena/flag-service",
         "networks": ["team1_net", "team2_net", "egress_net"],
         "ports": [f"127.0.0.1:{flag_port}:8080"],
         "extra_hosts": {"host.docker.internal": "host-gateway"},
         "env": _facility_env(api_port)},
        {"id": "health_poller", "image": "cyberarena/health-poller",
         "networks": ["team1_net", "team2_net", "egress_net"],
         "ports": [f"127.0.0.1:{health_port}:8080"],
         "extra_hosts": {"host.docker.internal": "host-gateway"},
         "env": _facility_env(api_port)},
    ]
    networks = {
        "team1_net": {"subnet": f"10.{octet}.1.0/24", "internal": True},
        "team2_net": {"subnet": f"10.{octet}.2.0/24", "internal": True},
        "attack_net": {"subnet": f"10.{octet}.11.0/24", "internal": True},
        # Non-internal: facilities need a route to host.docker.internal (GameMgr);
        # the internal team/attack nets have none. See the docstring above.
        "egress_net": {"subnet": f"10.{octet}.99.0/24"},
    }
    return {
        "teams": teams,
        "nodes": nodes,
        "networks": networks,
        "challenges": [
            {"name": name, "type": name, "port": port, "deploy_on": ["team1", "team2"]}
        ],
        "flag_service": {"base_url": f"http://127.0.0.1:{flag_port}", "container": "flag_service"},
        "health_poller": {"base_url": f"http://127.0.0.1:{health_port}", "container": "health_poller"},
        "round_config": (
            {"mode": "async", "round_secs": ROUND_SECS, "total_rounds": 1}
            if mode == "async"
            else {
                "defense_secs": DEFENSE_SECS,
                "attack_secs": ATTACK_SECS,
                "total_rounds": 1,
            }
        ),
    }


def yaml_dump(cfg: dict) -> str:
    """Dump the config, rendering multi-line strings (the cli body) as literal
    `|` blocks so a --keep'd config reads like a hand-written one."""
    import yaml

    class _Dumper(yaml.SafeDumper):
        pass

    def _str_rep(dumper, data):
        style = "|" if "\n" in data else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    _Dumper.add_representer(str, _str_rep)
    return yaml.dump(cfg, Dumper=_Dumper, default_flow_style=False, sort_keys=False)


# ── event assertions ──────────────────────────────────────────────────


def assert_events(
    events: list[dict], rep: Report, *, n_teams: int = 2, n_stores: int = 1,
    n_write: int = 0, mode: str = "async",
    proof_stores: dict[str, str] | None = None,
    proof_available: bool = False,
) -> None:
    """Assert the CHALLENGE behaved correctly in the live match.

    This is a challenge check, not an orchestrator regression test: it asserts
    only outcomes that depend on how the challenge is packaged. The infra
    milestones (containers ready, setup complete, facilities, round/phase loop,
    agent install) are the harness — they fire identically for every challenge,
    so failing on them would tell an author nothing about their own work. If
    the harness itself never ran, that surfaces upstream as "no run dir"
    (`run_mini_game`), not as a challenge failure here.

    The challenge-specific outcomes:
      • deploys   — the image built, the container came up, the service
                    deployed (Dockerfile / entrypoint / initial_start)
      • healthy   — the live `functionality_test` went green against the real
                    deploy, with no dimension still failing at the end
      • flag plant— `flag_handler.plant` succeeded against the live container
      • reachable — an attacker on another team connected to the service on its
                    declared port and a submission round-tripped, during the
                    legitimate attack window (LIVE in async, ATTACK in sync).
                    Catches the classic packaging bug a localhost health check
                    can't: a service bound to 127.0.0.1 instead of 0.0.0.0.
      • write rail — every WRITE store's live claim path reports an UNPLACED
                    mark as NOT_OBSERVED, and never credits one.
      • capability proof — after the tagged negative rail, a provisioned
                           `_exploit.py` produces an accepted capture for at
                           least one declared objective.
    """
    by_type: dict[str, list[dict]] = {}
    for e in events:
        by_type.setdefault(e.get("type", ""), []).append(e)

    def evts(t: str) -> list[dict]:
        return by_type.get(t, [])

    def check(name: str, ok: bool, detail: str) -> None:
        rep.add("challenge", name, Status.PASS if ok else Status.FAIL, detail)

    if not events:
        check("match ran", False,
              "no events recorded — the match did not run (harness/environment "
              "issue, not necessarily the challenge)")
        return

    # deploys — one SERVICE_DEPLOYED per team for the challenge service.
    deployed = evts("SERVICE_DEPLOYED")
    svcs = sorted({(e.get("payload") or {}).get("service", "") for e in deployed})
    check("service deploys", len(deployed) >= n_teams,
          f"SERVICE_DEPLOYED x{len(deployed)} for services={svcs} (need {n_teams})")

    # healthy — the live functionality_test. Take the LAST probe per
    # (team, dimension) and require all green, so transient bring-up failures
    # don't count but a service that ends unhealthy (or never came up) does.
    health = evts("SERVICE_HEALTH_CHECK")
    final: dict[tuple, bool] = {}
    for e in health:
        p = e.get("payload") or {}
        final[(e.get("team_id"), p.get("level"))] = bool(p.get("passed"))
    # Require BOTH teams to have been probed: a team whose service never binds
    # emits zero health events, so without this its absence is invisible and
    # `all(final.values())` would pass on the other team's green dimensions alone.
    teams_probed = {t for (t, _lvl) in final}
    healthy = (
        bool(final)
        and all(final.values())
        and len(teams_probed) >= n_teams
    )
    failing = sorted(f"{t}/{lvl}" for (t, lvl), ok in final.items() if not ok)
    if not health:
        detail = "no SERVICE_HEALTH_CHECK events — service never probed"
    elif len(teams_probed) < n_teams:
        detail = (f"{len(health)} health checks but only {len(teams_probed)}/"
                  f"{n_teams} teams probed (a team's service never came up)")
    else:
        detail = (f"{len(health)} health checks; final state "
                  + ("all green" if healthy else f"FAILING={failing}"))
    check("service healthy (live functionality_test)", healthy, detail)

    # flag plant — flag_handler.plant against the live container, per team.
    planted = evts("FLAG_PLANTED")
    needed = n_teams * n_stores
    check("flag plant (live)", len(planted) >= needed,
          f"FLAG_PLANTED x{len(planted)} (need {needed}: "
          f"{n_teams} teams x {n_stores} stores)")

    # reachable — the script only submits after a successful cross-team connect
    # to the service's declared port, so a FLAG_SUBMITTED recorded during the
    # attack window proves the deployed service was reachable by an opponent and
    # the submit rail carried it. The window is LIVE in async (networks always
    # on) and ATTACK in sync; a DEFENSE-phase submit never counts (rejected
    # WRONG_PHASE), so this exercises the full intended path. Sentinel flag →
    # rejected; acceptance is not the point (correctness is covered by --build).
    attack_phase = "LIVE" if mode == "async" else "ATTACK"
    submits = evts("FLAG_SUBMITTED")
    attack_submits = [
        e for e in submits if (e.get("phase") or "").upper() == attack_phase
    ]
    check("service reachable cross-team", len(attack_submits) >= 1,
          f"attacker connected + submitted x{len(attack_submits)} during "
          f"{attack_phase} (sentinel flag, acceptance not required)")

    # The harness tags the events it owns. Once one submitter has a rejected
    # sentinel and every WRITE negative claim, later accepted events from that
    # submitter can be attributed to the PoC without changing the PoC contract.
    negative_claims: set[tuple] = set()
    rail_claims_by_submitter: dict[str, set[tuple]] = {}
    rail_submitters: set[str] = set()
    rail_completed_at: dict[str, int] = {}
    false_positives = 0
    for index, e in enumerate(attack_submits):
        p = e.get("payload") or {}
        attempt_id = str(p.get("attempt_id") or "")
        if not attempt_id.startswith(RAIL_ATTEMPT_PREFIX):
            continue
        submitter = str(p.get("submitter_team_id") or "")
        if p.get("oracle") == "claim":
            claim = (
                submitter, p.get("victim_team_id"),
                p.get("service"), p.get("store"),
            )
            if p.get("accepted"):
                false_positives += 1
            elif p.get("reason") == "NOT_OBSERVED":
                negative_claims.add(claim)
                rail_claims_by_submitter.setdefault(submitter, set()).add(claim)
        elif not p.get("accepted"):
            rail_submitters.add(submitter)
        if (submitter in rail_submitters
                and len(rail_claims_by_submitter.get(submitter, ())) >= n_write):
            rail_completed_at.setdefault(submitter, index)

    if n_write:
        needed = n_teams * n_write
        check("write claim rail (live)",
              len(negative_claims) >= needed and false_positives == 0,
              f"NOT_OBSERVED claims x{len(negative_claims)} (need {needed}: "
              f"{n_teams} teams x {n_write} write stores); "
              f"false-positive accepts x{false_positives}")

    proof_stores = proof_stores or {}
    if proof_stores:
        if not proof_available:
            rep.add(
                "capability proof", "packaged PoV", Status.NOTE,
                "UNVERIFIED: challenge declares objectives "
                f"{sorted(proof_stores)} but ships no _exploit.py",
            )
        else:
            proven: set[str] = set()
            metadata_errors: list[str] = []
            explicitly_correlated: set[str] = set()
            for index, e in enumerate(attack_submits):
                p = e.get("payload") or {}
                attempt_id = str(p.get("attempt_id") or "")
                submitter = str(p.get("submitter_team_id") or "")
                rail_index = rail_completed_at.get(submitter)
                store = p.get("store")
                if (not p.get("accepted")
                        or rail_index is None
                        or index <= rail_index
                        or store not in proof_stores):
                    continue
                expected_outcome = proof_stores[store]
                expected_oracle = "claim" if expected_outcome == "write" else "submit"
                if (p.get("outcome"), p.get("oracle")) != (
                        expected_outcome, expected_oracle):
                    metadata_errors.append(
                        f"{store}: got outcome/oracle "
                        f"{p.get('outcome')!r}/{p.get('oracle')!r}, expected "
                        f"{expected_outcome!r}/{expected_oracle!r}"
                    )
                    continue
                proven.add(store)
                if attempt_id.startswith(PROOF_ATTEMPT_PREFIX):
                    explicitly_correlated.add(store)
            ok = bool(proven) and not metadata_errors
            rep.add(
                "capability proof", "packaged PoV",
                Status.PASS if ok else Status.FAIL,
                (f"accepted captures after checker rail for stores {sorted(proven)}; "
                 f"explicit attempt ids for {sorted(explicitly_correlated)}"
                 if ok else
                 f"accepted objective stores: {sorted(proven) or 'none'}; "
                 f"metadata errors: {metadata_errors or 'none'}"),
            )


def parse_events(run_dir: Path) -> list[dict]:
    """Read runs/<ts>/events.jsonl into a list of event dicts (bad lines skipped)."""
    import json

    out: list[dict] = []
    path = run_dir / "events.jsonl"
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ── driver ────────────────────────────────────────────────────────────


def _exploit_path(ch: Challenge) -> Path | None:
    """Return the optional PoC beside the concrete challenge class."""
    try:
        path = Path(inspect.getfile(type(ch))).resolve().parent / "_exploit.py"
    except (OSError, TypeError):
        return None
    return path if path.is_file() else None


def _run_dir_for(before: set[Path], suffix: str) -> Path | None:
    """A run dir created by this invocation: a new runs/* whose name ends with
    the challenge kebab (the suffix GameMgr appends) and has events.jsonl. The
    suffix lets concurrent checks on DIFFERENT challenges each pick their own."""
    if not RUNS_DIR.is_dir():
        return None
    fresh = [
        d for d in RUNS_DIR.iterdir()
        if d.is_dir() and d not in before
        and d.name.endswith(f"_{suffix}") and (d / "events.jsonl").exists()
    ]
    return max(fresh, key=lambda d: d.name) if fresh else None


def _assertions_met(
    events: list[dict], mode: str, n_stores: int, n_write: int = 0,
    proof_stores: dict[str, str] | None = None,
    proof_available: bool = False,
) -> bool:
    """True once the live events would PASS every assert_events check — per-team
    counts, not just type presence — so the driver doesn't cut off a slow second
    team and false-FAIL it."""
    scratch = Report(challenge="")
    assert_events(
        events, scratch, mode=mode, n_stores=n_stores, n_write=n_write,
        proof_stores=proof_stores, proof_available=proof_available,
    )
    return scratch.ok


def _drive_then_stop(proc: subprocess.Popen, run_dir: Path, *, deadline: float,
                     mode: str, n_stores: int, n_write: int = 0,
                     proof_stores: dict[str, str] | None = None,
                     proof_available: bool = False) -> None:
    """Let the match run one LIVE window, then return so the caller stops it. The
    scripted async agents never finish, so `main.py start` won't exit on its own:
    wait until LIVE has run `LIVE_WINDOW_SECS` and the assert_events checks would
    pass, then hand back. Returns early on GAME_END or process death; never past
    `deadline`."""
    import time
    live_start: float | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        events = parse_events(run_dir) if (run_dir / "events.jsonl").exists() else []
        types = {e.get("type", "") for e in events}
        if "GAME_END" in types:
            return
        if "ROUND_START" in types and live_start is None:
            live_start = time.monotonic()
        if (live_start is not None
                and time.monotonic() - live_start >= LIVE_WINDOW_SECS
                and _assertions_met(
                    events, mode, n_stores, n_write,
                    proof_stores, proof_available,
                )):
            return
        time.sleep(3.0)


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL after a short grace — the scripted match doesn't
    self-terminate, so we stop it once LIVE produced the events we assert on."""
    import time
    if proc.poll() is not None:
        return
    proc.terminate()
    for _ in range(20):
        if proc.poll() is not None:
            return
        time.sleep(0.5)
    proc.kill()
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def run_mini_game(
    target: str, *, rep: Report | None = None, api_port: int | None = None,
    keep: bool = False, verbose: bool = False, mode: str = "async",
) -> Report:
    """Generate a tiny v3 config, run one real match, assert the plumbing events.

    Appends to `rep` if given (so --game can share one report with the earlier
    phases), else starts a fresh one. `mode` is "async" (default) or "sync".
    """
    import time
    name, ch, err = resolve_challenge(target)
    if rep is None:
        # Standalone call: the resolve outcome owns `located`. When a phase
        # shares its report, that phase already set `located` — don't clobber it.
        rep = Report(challenge=name or target, located=ch is not None)
    if err:
        rep.add("resolve", "challenge", Status.FAIL, err)
        return rep

    # Unique per process so the namespaced containers/networks don't collide with
    # any arena already up on this host (and parallel sweep workers differ).
    namespace = f"chk{os.getpid()}"
    # Distinct ports in one shot (free_ports avoids the dup a run of free_port()s
    # can return) and a subnet octet probed free of every live docker network, so
    # a concurrent sweep / a check beside a running arena doesn't clash.
    if api_port is None:
        api_port, flag_port, health_port = free_ports(3)
    else:
        flag_port, health_port = free_ports(2)
    subnet_octet = _free_subnet_octet(namespace)
    try:
        specs = list(ch.flag_store_specs())
        proof_stores = {
            sp.name: sp.kind.value for sp in specs
        }
        proof_available = bool(proof_stores) and _exploit_path(ch) is not None
        cfg = build_game_config(name, ch, api_port=api_port,
                                flag_port=flag_port, health_port=health_port,
                                mode=mode, subnet_octet=subnet_octet,
                                proof_required=proof_available)
    except Exception as e:  # noqa: BLE001 — config gen errors are the author's to fix
        rep.add("resolve", "config", Status.FAIL, f"cannot build config: {e}")
        return rep

    # The scripted attacker submits to the ON-NET flag-service (reachable from
    # the dev's internal team net), not the host GameMgr (which the isolated dev
    # can't reach) — so point its submit URL there. CYBERARENA_FACILITY_TOKEN is
    # the facility<->GameMgr shared secret (the config interpolates it).
    env = {
        **os.environ,
        "CYBERARENA_FACILITY_TOKEN": f"chkfacility{os.getpid()}",
        "CYBERARENA_PUBLIC_BASE_URL": "http://flag_service:8080",
    }

    before = set(RUNS_DIR.iterdir()) if RUNS_DIR.is_dir() else set()
    fd, tmp_path = tempfile.mkstemp(prefix="check_game_", suffix=".yaml")
    os.close(fd)  # we write through Path below; don't leak the mkstemp fd
    tmp = Path(tmp_path)
    tmp.write_text(yaml_dump(cfg))
    out_path = None if verbose else tmp.with_suffix(".log")

    def _teardown() -> None:
        try:
            subprocess.run(
                [sys.executable, "main.py", "teardown", "-c", str(tmp),
                 "--namespace", namespace],
                cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
                timeout=TEARDOWN_TIMEOUT_SECS,
            )
        except subprocess.TimeoutExpired:
            rep.add("run", "teardown", Status.NOTE,
                    f"teardown did not finish within {TEARDOWN_TIMEOUT_SECS:.0f}s; "
                    f"containers/networks may linger")

    # Defensive: clear any stale namespace from a crashed prior run (pid reuse).
    _teardown()

    proc: subprocess.Popen | None = None
    log_f = open(out_path, "w") if out_path else None
    try:
        proc = subprocess.Popen(
            [sys.executable, "main.py", "start", "-c", str(tmp),
             "--port", str(api_port), "--namespace", namespace],
            cwd=str(REPO_ROOT), env=env,
            stdout=(None if verbose else log_f),
            stderr=(None if verbose else subprocess.STDOUT),
        )
        deadline = time.monotonic() + GAME_TIMEOUT_SECS
        run_dir = None
        while time.monotonic() < deadline and proc.poll() is None:
            run_dir = _run_dir_for(before, ch.name)
            if run_dir is not None:
                break
            time.sleep(2.0)
        if run_dir is None:
            tail = ""
            if out_path and out_path.exists():
                tail = out_path.read_text()[-800:]
            rep.add("run", "match", Status.FAIL,
                    f"no run dir produced (exit {proc.poll()}); output tail: {tail}")
        else:
            n_stores = len(specs)
            n_write = sum(1 for sp in specs if sp.kind is OracleKind.WRITE)
            _drive_then_stop(proc, run_dir, deadline=deadline, mode=mode,
                             n_stores=n_stores, n_write=n_write,
                             proof_stores=proof_stores,
                             proof_available=proof_available)
            rep.add("run", "match", Status.PASS, f"events from {run_dir.name}")
            assert_events(parse_events(run_dir), rep, mode=mode,
                          n_stores=n_stores, n_write=n_write,
                          proof_stores=proof_stores,
                          proof_available=proof_available)
    finally:
        if proc is not None:
            _terminate(proc)
        if log_f is not None:
            log_f.close()
        _teardown()
        if keep:
            rep.add("run", "config", Status.NOTE, f"kept generated config at {tmp}")
        else:
            tmp.unlink(missing_ok=True)
            if out_path:
                out_path.unlink(missing_ok=True)

    return rep
