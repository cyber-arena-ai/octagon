"""Flag-service app — the containerized FlagServer (v3: plants via SSH).

Runs off the host. It owns the full flag lifecycle: it PLANTS flags into prod
**itself** over SSH (`SshExecPort`) — no host plant + push — holds the resulting
`FlagStore`, and validates submits against it. GameMgr stays the round clock: it
`/flags/configure`s the service once (tokens + prod-hosts + deployment) and then
signals `/flags/rotate {round_id}` each round; the service does the SSH planting.

Endpoints:
  POST /flags/configure  infra  (X-Facility-Token): tokens + hosts + deployment;
                                 (re)builds the FlagServer bound to SshExecPort and
                                 provisions opt-in match-long baseline principals
                                 before publishing it
  POST /flags/rotate      infra  (X-Facility-Token): plant fresh flags into prod
                                 over SSH + record FLAG_PLANTED
  POST /game/flag/submit  player (X-Team-Token):     validate + record FLAG_SUBMITTED
  POST /game/flag/claim   player (X-Team-Token):     read an attacker's mark back off
                                 the victim (write stores) + record FLAG_SUBMITTED
  POST /game/attack-info  player (X-Team-Token):     opponents' current-round flag-ids
                                 and, for write stores, the requester's own mark;
                                 plus the requester's own baseline principals
  GET  /healthz

Auth split: only `submit`, `claim` and `attack-info` are player-facing; configure/rotate require the
facility token. The service is off the agent net regardless.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

from challenges.interface import (
    DEFAULT_STORE,
    FlagHandler,
    FlagStoreSpec,
    OracleKind,
    VulboxTarget,
)
from facility import FacilityContext
from facility.adapters_http import (
    HttpAuthoritativeStatePort,
    HttpEventSink,
    HttpStatusPort,
)
from facility.adapters_ssh import SshExecPort
from facility.ports import AuthoritativeStatePort, ExecPort
from flag import FlagServer
from flag.principals import PrincipalProvisioningError

log = logging.getLogger("flag.service")


def _submit_result_body(result) -> dict:
    """Serialize one submit-or-claim result for the player HTTP boundary."""
    return {"accepted": result.accepted, "reason": result.reason, "detail": result.detail}


def _specs_from_payload(d: dict) -> list[FlagStoreSpec]:
    """Rebuild `flag_store_specs()` from one `/flags/configure` deployment row —
    the ONE store declaration the service plants from.

    GameMgr and the service are co-versioned, so a missing declaration is a
    configuration error rather than a second legacy/default protocol. An
    unrecognized kind string is likewise a challenge-side error."""
    specs = d["flag_store_specs"]
    return [
        FlagStoreSpec(
            name=s["name"],
            kind=OracleKind(s["kind"]),
            scope=s.get("scope"),
        )
        for s in specs
    ]


def _default_probe_loader(package: str):
    """Load just this challenge's flag_handler (no build_challenge / discovery).
    Lazy import so the module loads without the registry; tests inject a fake."""
    from challenges.registry import load_probe
    return load_probe(package, "flag_handler", FlagHandler)


def build_flag_service_app(
    *,
    gm_base_url: str,
    facility_token: str,
    key_path: str = "",
    client: httpx.Client | None = None,
    exec_port: ExecPort | None = None,
    state_port: AuthoritativeStatePort | None = None,
    probe_loader=_default_probe_loader,
) -> FastAPI:
    status = HttpStatusPort(gm_base_url, client=client)
    events = HttpEventSink(gm_base_url, facility_token, client=client)
    state = state_port or HttpAuthoritativeStatePort(
        gm_base_url, facility_token, client=client,
    )

    # Mutable state, populated by /flags/configure.
    st: dict = {"fs": None, "token_to_team": {}}

    app = FastAPI(title="flag-service")

    def _authed(token: str | None) -> bool:
        return bool(facility_token) and token == facility_token

    def _build_flag_server(
        hosts: dict, deployment: list[dict], team_ids: list[str],
    ) -> FlagServer:
        # SSH exec into prod, identity-resolved (team_id -> host). Tests inject
        # exec_port directly; prod builds an SshExecPort over the configured hosts.
        ex = exec_port or SshExecPort(key_path=key_path, host_by_team=hosts)
        ctx = FacilityContext(status=status, events=events, exec=ex)
        # Load only the flag_handler probe per deployed challenge; GameMgr passed
        # the package + named ports (it has the full Challenge host-side). The
        # shim gives FlagServer the .flag_handler + .vulbox.exposed_ports it
        # reads — no Challenge instantiation in the facility.
        challenge_by_owner = {
            (d["team_id"], d["service"]): SimpleNamespace(
                name=d["service"],
                flag_handler=probe_loader(d["package"]),
                vulbox=SimpleNamespace(exposed_ports=dict(d.get("ports") or {})),
                # GameMgr sends the challenge's flag_store_specs() (it has the
                # full Challenge host-side). That is the authoritative store
                # declaration — which stores to plant AND each one's oracle kind
                # — so the facility carries no second view that could drift.
                flag_store_specs=(lambda specs=_specs_from_payload(d): specs),
            )
            for d in deployment
        }

        def target_factory(team_id: str, challenge) -> VulboxTarget:
            host = hosts.get(team_id, "")

            def exec_in(_container_name: str, cmd: str):
                r = ex.exec(team_id, "prod", cmd)
                return r.exit_code, r.stdout

            def read_state(region: str):
                return state.read(team_id, challenge.name, region)

            return VulboxTarget(
                host=host,
                ports=dict(challenge.vulbox.exposed_ports),
                meta={
                    "team_id": team_id,
                    "exec_in_container": exec_in,
                    "read_authoritative_state": read_state,
                },
            )

        return FlagServer(
            ctx,
            challenge_by_owner=challenge_by_owner,
            target_factory=target_factory,
            team_ids=team_ids,
        )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "facility": "flag",
                "configured": st["fs"] is not None,
                "teams": len(st["token_to_team"])}

    @app.post("/flags/configure")
    async def configure(body: dict, x_facility_token: str | None = Header(default=None)):
        """GameMgr (re)configures the service: the token map (submit identity),
        the prod-host map (SSH targets), and the deployment (which (team, service)
        to plant). Builds the FlagServer bound to SshExecPort, provisions the
        baseline principals requested by opt-in challenges, and only THEN
        publishes it — a provisioning failure 500s so GameMgr aborts setup."""
        if not _authed(x_facility_token):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        tokens = dict(body.get("tokens") or {})
        hosts = dict(body.get("hosts") or {})
        deployment = list(body.get("deployment") or [])
        # Build a CANDIDATE server and provision baseline principals on it BEFORE
        # publishing it: every opt-in challenge must finish its match-long
        # assignments before round 0, rather than leave a live match with a
        # half-provisioned victim.
        team_ids = sorted(set(tokens.values()) | {d["team_id"] for d in deployment})
        fs = _build_flag_server(hosts, deployment, team_ids)
        try:
            # Off the event loop like rotate/audit: minting principals talks to
            # every prod through the challenge's supported administration path.
            await asyncio.to_thread(fs.provision_principals, team_ids)
        except PrincipalProvisioningError as e:
            # `e` is log-safe by construction (ids + reason, never a credential).
            log.error("baseline-principal provisioning failed: %s", e)
            return JSONResponse(status_code=500,
                                content={"error": "principal provisioning failed"})
        except Exception as e:  # noqa: BLE001 — never echo an unknown message
            log.error("baseline-principal provisioning failed: %s", type(e).__name__)
            return JSONResponse(status_code=500,
                                content={"error": "principal provisioning failed"})
        st["token_to_team"] = tokens
        st["fs"] = fs
        return {"ok": True, "deployment": len(deployment),
                "teams": len(st["token_to_team"])}

    @app.post("/flags/rotate")
    async def rotate(body: dict, x_facility_token: str | None = Header(default=None)):
        """Round-clock signal from GameMgr: plant fresh flags into prod over SSH
        and record FLAG_PLANTED. Idempotent per round (FlagServer.rotate).

        A rotation that could not cover every target raises out of `rotate` — the
        exception is deliberately NOT swallowed here, so it surfaces as a 500 and
        GameMgr's `_signal_flag_rotate` fails the round rather than publishing a
        half-rotated one. FLAG_PLANT_FAILED is already on the timeline by then."""
        if not _authed(x_facility_token):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        if st["fs"] is None:
            return JSONResponse(status_code=409, content={"error": "not configured"})
        round_id = int(body.get("round_id") or 0)
        # Plant over SSH in a worker thread, NOT on the event loop: a slow or
        # hung plant (e.g. a prod box mid-restart) must not stall concurrent
        # /game/flag/submit requests. This was the flag-service-wedge bug —
        # a blocking plant on the loop froze all submits + later rotations.
        await asyncio.to_thread(st["fs"].rotate, round_id)
        return {"ok": True, "round_id": round_id}

    @app.post("/flags/audit")
    async def audit(body: dict, x_facility_token: str | None = Header(default=None)):
        """Round-END signal from GameMgr: read back every planted flag and record
        one FLAG_PERSISTENCE event per (team, service, store) — did the defender's
        own flag survive the round? Read-only (repair only on AUTH_REJECTED);
        never fails the match (FlagServer.audit)."""
        if not _authed(x_facility_token):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        if st["fs"] is None:
            return JSONResponse(status_code=409, content={"error": "not configured"})
        round_id = int(body.get("round_id") or 0)
        # Off the event loop: the audit does an SSH read per (team, service,
        # store), so a slow/hung box can't stall concurrent /game/flag/submit —
        # same reason /flags/rotate runs off-loop.
        await asyncio.to_thread(st["fs"].audit, round_id)
        return {"ok": True, "round_id": round_id}

    @app.post("/game/flag/submit")
    async def submit(
        body: dict,
        x_team_token: str | None = Header(default=None),
        x_cyberarena_verification_attempt: str | None = Header(default=None),
    ):
        if st["fs"] is None:
            return {"accepted": False, "reason": "ERROR",
                    "detail": "flag service not configured yet"}
        team_id = st["token_to_team"].get(x_team_token) if x_team_token else None
        if team_id is None:
            return {"accepted": False, "reason": "BAD_TOKEN",
                    "detail": "X-Team-Token header missing or empty"}
        # `X-CyberArena-Verification-Attempt` (verification harnesses only) is
        # echoed into FLAG_SUBMITTED so a harness can correlate the capture with
        # its attempt. Absent for every normal submit (async/attack) → the field
        # is simply not stamped, so nothing else changes.
        # FlagServer.submit makes blocking HTTP calls (status + event record);
        # run it off the event loop so submits stay responsive and concurrent
        # (and aren't blocked behind an in-flight rotate's planting).
        result = await asyncio.to_thread(
            st["fs"].submit, team_id, body.get("flag", ""),
            x_cyberarena_verification_attempt,
        )
        return _submit_result_body(result)

    @app.post("/game/flag/claim")
    async def claim(
        body: dict,
        x_team_token: str | None = Header(default=None),
        x_cyberarena_verification_attempt: str | None = Header(default=None),
    ):
        """Player-facing: claim a WRITE capture on `{victim_team_id, service,
        store}`. The attacker submits no evidence — the service reads the victim's
        writable protected target itself (through the challenge's own `retrieve`)
        and credits only if the attacker's own mark is there. Same token identity
        and phase gate as /game/flag/submit."""
        if st["fs"] is None:
            return {"accepted": False, "reason": "ERROR",
                    "detail": "flag service not configured yet"}
        team_id = st["token_to_team"].get(x_team_token) if x_team_token else None
        if team_id is None:
            return {"accepted": False, "reason": "BAD_TOKEN",
                    "detail": "X-Team-Token header missing or empty"}
        # Off the event loop for the same reason as submit + rotate: the claim
        # does a privileged region read, and a slow victim must not stall
        # concurrent submissions.
        result = await asyncio.to_thread(
            st["fs"].claim, team_id,
            str(body.get("victim_team_id", "") or ""),
            str(body.get("service", "") or ""),
            str(body.get("store", "") or DEFAULT_STORE),
            x_cyberarena_verification_attempt,
        )
        return _submit_result_body(result)

    @app.post("/game/attack-info")
    async def attack_info(x_team_token: str | None = Header(default=None)):
        """Player-facing: the requesting team's current-round attack-info — the
        OTHER teams' public flag-ids (which record/identity holds each flag) plus
        the host to aim at, and for a WRITE store the requester's own mark. Same
        token identity as submit (X-Team-Token → team); never reveals the
        requester's own targets or any flag value.

        `principals` rides alongside as a sibling array: the match-long baseline
        identity + credentials THIS requester was issued on each opponent service
        (never another team's, never its own). It does not rotate with the round,
        and no credential ever appears in a `targets` row."""
        if st["fs"] is None:
            return {"targets": [], "principals": []}
        team_id = st["token_to_team"].get(x_team_token) if x_team_token else None
        if team_id is None:
            return {"error": "BAD_TOKEN",
                    "detail": "X-Team-Token header missing or empty"}

        def _info():
            return (st["fs"].attack_info(team_id),
                    st["fs"].principal_info(team_id))

        targets, principals = await asyncio.to_thread(_info)
        return {"targets": targets, "principals": principals}

    return app
