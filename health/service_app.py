"""Health-poller app — the containerized HealthPoller (v3: probes over SSH).

Runs the poll scheduler off the host AND does the probe itself: it runs each
challenge's functionality test inside prod over SSH (ExecPort -> SshExecPort),
records SERVICE_HEALTH_CHECK via the EventSink. GameMgr stays the clock and
drives phase transitions; the v2 host `/facility/health-check` verb is gone.

Endpoints (all infra — X-Facility-Token):
  POST /poller/configure {hosts, targets:[{team_id,service,interval_secs}]}
       — build the SSH-backed HealthChecker + HealthPoller (targets carry the
         challenge + a VulboxTarget bound to SshExecPort)
  POST /poller/attack-enter {round_id}   — start immediate + periodic probes
  POST /poller/attack-exit               — cancel
  POST /poller/trigger-now {team_id, service, trigger?}   — one-shot
         (trigger tags the probe: "restart" default, "manual" for agents)
  GET  /healthz

No player-facing surface.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

from types import SimpleNamespace

from challenges.interface import FunctionalityTest, VulboxTarget
from facility import FacilityContext
from facility.adapters_http import HttpEventSink, HttpStatusPort
from facility.adapters_ssh import SshExecPort
from facility.ports import ExecPort

from .checker import HealthChecker
from .poller import HealthPoller, PollTarget

log = logging.getLogger("health.service")


def _default_probe_loader(package: str):
    """Load just this challenge's functionality_test (no build_challenge /
    discovery). Lazy import; tests inject a fake."""
    from challenges.registry import load_probe
    return load_probe(package, "functionality_test", FunctionalityTest)


def _default_checker_loader(package: str):
    """Resolve the optional CHECKER forms (checker.py probe, checker.sh bytes)
    for this challenge. Lazy import; tests inject a fake."""
    from health.checkers import load_checkers
    return load_checkers(package)


def build_health_poller_app(
    *,
    gm_base_url: str,
    facility_token: str,
    key_path: str = "",
    client: httpx.Client | None = None,
    exec_port: ExecPort | None = None,
    probe_loader=_default_probe_loader,
    checker_loader=_default_checker_loader,
) -> FastAPI:
    status = HttpStatusPort(gm_base_url, client=client)
    events = HttpEventSink(gm_base_url, facility_token, client=client)
    st: dict = {"poller": None}

    app = FastAPI(title="health-poller")

    def _authed(token: str | None) -> bool:
        return bool(facility_token) and token == facility_token

    def _build_poller(hosts: dict, targets_cfg: list[dict]) -> HealthPoller:
        ex = exec_port or SshExecPort(key_path=key_path, host_by_team=hosts)
        ctx = FacilityContext(status=status, events=events, exec=ex)
        checker = HealthChecker(ctx)

        def make_exec(team_id: str):
            def exec_in(_container_name: str, cmd: str):
                r = ex.exec(team_id, "prod", cmd)
                return r.exit_code, r.stdout
            return exec_in

        def make_exec_stdin(team_id: str):
            # Runs `bash -s` in prod with `script` on stdin (checker.sh, shipped
            # from the poller so nothing tamperable lands on prod disk).
            def exec_stdin(script: bytes):
                r = ex.exec_stdin(team_id, "prod", "bash -s", script)
                return r.exit_code, r.stdout, r.stderr, r.timed_out
            return exec_stdin

        targets: list[PollTarget] = []
        for tc in targets_cfg:
            team_id, service = tc["team_id"], tc["service"]
            # Load the functionality_test probe + the optional CHECKER forms
            # (checker.py / checker.sh) once at configure; GameMgr passed the
            # package + named ports. The shim gives HealthChecker what it reads
            # — no Challenge instantiation in the facility.
            checker_py, checker_sh = checker_loader(tc["package"])
            challenge = SimpleNamespace(
                functionality_test=probe_loader(tc["package"]),
                checker=checker_py,
                checker_sh=checker_sh,
            )
            target = VulboxTarget(
                host=hosts.get(team_id, ""),
                ports=dict(tc.get("ports") or {}),
                meta={
                    "team_id": team_id,
                    "exec_in_container": make_exec(team_id),
                    "exec_stdin": make_exec_stdin(team_id),
                },
            )
            targets.append(PollTarget(
                team_id=team_id, service=service, challenge=challenge,
                target=target, interval_secs=float(tc["interval_secs"]),
            ))
        return HealthPoller(checker, targets)

    @app.get("/healthz")
    async def healthz():
        p = st["poller"]
        return {"status": "ok", "facility": "health",
                "configured": p is not None,
                "targets": len(p.targets) if p else 0}

    @app.post("/poller/configure")
    async def configure(body: dict, x_facility_token: str | None = Header(default=None)):
        if not _authed(x_facility_token):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        st["poller"] = _build_poller(dict(body.get("hosts") or {}),
                                     list(body.get("targets") or []))
        return {"ok": True, "targets": len(st["poller"].targets)}

    @app.post("/poller/attack-enter")
    async def attack_enter(body: dict, x_facility_token: str | None = Header(default=None)):
        if not _authed(x_facility_token):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        if st["poller"] is None:
            return JSONResponse(status_code=409, content={"error": "not configured"})
        st["poller"].on_attack_enter(int(body.get("round_id") or 0))
        return {"ok": True}

    @app.post("/poller/attack-exit")
    async def attack_exit(x_facility_token: str | None = Header(default=None)):
        if not _authed(x_facility_token):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        if st["poller"] is not None:
            st["poller"].on_attack_exit()
        return {"ok": True}

    @app.post("/poller/trigger-now")
    async def trigger_now(body: dict, x_facility_token: str | None = Header(default=None)):
        if not _authed(x_facility_token):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        if st["poller"] is None:
            return JSONResponse(status_code=409, content={"error": "not configured"})
        await st["poller"].trigger_now(
            str(body["team_id"]), str(body["service"]),
            trigger=str(body.get("trigger", "restart")),
        )
        return {"ok": True}

    return app
