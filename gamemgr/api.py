"""FastAPI surface for GameMgr — REST endpoints plus a WebSocket event stream."""

from __future__ import annotations

import asyncio
import base64
import functools
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .events import Event, EventType
from .game_mgr import GameMgr
from . import trajectory
from .transcripts import collect_transcripts

PANEL_HTML_PATH = Path(__file__).parent.parent / "panel" / "panel.html"

log = logging.getLogger(__name__)


def build_app(game: GameMgr, shutdown_event: asyncio.Event) -> FastAPI:
    # v3: MCP is served by the on-net mcp-gateway container, not the host app.
    # The host app is GameMgr's REST/control + facility-ingest + panel/WS surface.
    app = FastAPI(title="cyber-arena")

    # ── read-surface access gates ───────────────────────────────────────────
    # The panel/spectator read surface is NOT open. /panel + /topology are the
    # whole-game operator view (admin token only). /game/agents + /game/metrics
    # are per-team: a valid ADMIN token sees all teams (what the panel needs),
    # a team's own X-Team-Token sees only its own, anything else is refused —
    # this is what stops a player from reading the opponent's transcript (and
    # scraping its flag) straight off /game/agents. /panel, /topology,
    # /game/events and the /events/stream WS are admin-only too — the timeline
    # carries both teams' scoring + activity. Only /game/status + /game/rounds
    # stay open (global phase/round, no secrets). Fail closed: admin_token_ok is
    # False when CYBERARENA_ADMIN_TOKEN is unset, so the admin surfaces 403 until
    # an operator opts in.
    def _require_admin(x_admin_token: str | None) -> None:
        if not game.admin_token_ok(x_admin_token):
            raise HTTPException(status_code=403, detail="forbidden")

    def _view_scope(x_admin_token: str | None,
                    x_team_token: str | None) -> str | None:
        """None = full (all-team) view for a valid admin token; else the caller's
        team_id for a valid X-Team-Token; else 401."""
        if game.admin_token_ok(x_admin_token):
            return None
        team_id = game._token_to_team.get(x_team_token) if x_team_token else None
        if team_id is not None:
            return team_id
        raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/panel")
    async def panel_page(x_admin_token: str | None = Header(default=None)):
        """Serve panel/panel.html. Admin-token only — the whole-game operator
        view; the deploy backend injects the token when it proxies the
        authenticated human, so agents (no admin token) get 403. Same-origin
        with /game/* + /events/stream so the browser can WS without CORS /
        mixed-content fuss."""
        _require_admin(x_admin_token)
        return FileResponse(PANEL_HTML_PATH, media_type="text/html")

    @app.get("/game/status")
    async def status():
        return game.status()

    @app.get("/game/rounds")
    async def rounds():
        st = game.status()
        return {"current_round": st["round"], "total_rounds": st["total_rounds"]}

    @app.get("/game/agents")
    async def agents(
        x_admin_token: str | None = Header(default=None),
        x_team_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """One continuous agent trajectory PER TEAM for the current run, read
        live from the artifact dir (reflects the growing current chunk — poll
        for real-time updates). The agent writes one `stream-*.jsonl` per chunk
        (async restart / sync phase); here they're stitched into ONE merged
        trajectory per team (was one-per-chunk), with a `chunk_boundary` marker
        between chunks so restarts stay visible. The raw per-chunk files stay on
        disk (in the run artifacts) for deep-dives.

        Returns the NORMALIZED transcript cards the panel renders. Carries full
        agent output — tool commands, reasoning, flag strings — so access is
        gated: an ADMIN token gets every team (the panel), a team's X-Team-Token
        gets only its own transcript, anything else is 401. Without this a player
        could read the opponent's transcript here and scrape its flag."""
        scope = _view_scope(x_admin_token, x_team_token)  # None = all teams
        if game.run_dir is None:
            return {"run": None, "transcripts": []}
        transcripts = trajectory.merge_by_team(collect_transcripts(game.run_dir))
        if scope is not None:
            transcripts = [t for t in transcripts if t.get("team_id") == scope]
        return {"run": game.run_dir.name, "transcripts": transcripts}

    @app.get("/game/metrics")
    async def metrics_endpoint(
        x_admin_token: str | None = Header(default=None),
        x_team_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Per-team timeline metrics — tool-call latency + token rate on ONE
        absolute clock — for the panel. Refreshes live from the api-router's
        per-call usage (pulled from the router) plus the agent streams, then
        returns the roll-up. Empty until the first LLM calls land.

        Gated like /game/agents (it reveals a team's model + tool-call sequence +
        token throughput — an activity side-channel): ADMIN token → all teams;
        a team's X-Team-Token → only its own; else 401."""
        import asyncio as _aio
        import json as _json
        scope = _view_scope(x_admin_token, x_team_token)  # None = all teams
        if game.run_dir is None:
            return {"run": None, "teams": {}}
        try:  # pull the router log + recompute (docker exec — off the event loop)
            await _aio.get_running_loop().run_in_executor(None, game.persist_router_usage)
        except Exception:  # noqa: BLE001 — live metrics are best-effort
            pass
        mp = game.run_dir / "artifacts" / "metrics.json"
        try:
            teams = _json.loads(mp.read_text()) if mp.exists() else {}
        except Exception:  # noqa: BLE001
            teams = {}
        if scope is not None:
            teams = {scope: teams[scope]} if scope in teams else {}
        return {"run": game.run_dir.name, "teams": teams}

    @app.get("/game/events")
    async def events(since: float | None = None,
                     x_admin_token: str | None = Header(default=None)) -> list[dict[str, Any]]:
        """The whole-match timeline (both teams' scoring + activity events).
        Admin-token only — the deploy backend injects it when proxying the
        authenticated human to the panel; agents use the mcp-gateway, not this."""
        _require_admin(x_admin_token)
        hist = game.timeline.history()
        if since is not None:
            hist = [e for e in hist if e.ts >= since]
        return [asdict(e) for e in hist]

    @app.get("/topology")
    async def topology(x_admin_token: str | None = Header(default=None)) -> Any:
        """Live network topology (config intent ⊕ live docker) for THIS run, on
        the same port as the panel — lets the platform proxy a per-job topology
        view cross-host. Built in a thread (docker calls block).

        Admin-token only (whole-game view; no per-team variant). Passes this
        run's namespace so the overlay is exact even under single-host
        concurrency (docker names are prefixed `{ns}-`; build_topology strips it).
        """
        _require_admin(x_admin_token)
        if game.run_dir is None:
            return JSONResponse(status_code=409, content={"error": "run not started"})
        from vis.topology import build_topology   # lazy: pulls the docker SDK

        cfg = str(game.run_dir / "config.yaml")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, functools.partial(build_topology, cfg, ns=game.ns))

    @app.post("/facility/events")
    async def facility_events(body: dict, x_facility_token: str | None = Header(default=None)):
        """Infra-only: a containerized facility POSTs telemetry back to the
        single central timeline. Facility-token auth (NOT the player token);
        idempotent on `event_id`. Fail closed: 403 unless the token matches."""
        if not game.facility_token_ok(x_facility_token):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        event_id = body.get("event_id")
        if not event_id:
            return JSONResponse(status_code=400, content={"error": "event_id required"})
        recorded = game.ingest_event(
            event_id=str(event_id),
            type_=str(body.get("type", "")),
            team_id=body.get("team_id"),
            round_id=int(body.get("round_id") or 0),
            phase=str(body.get("phase", "")),
            payload=body.get("payload") or {},
        )
        return {"ok": True, "recorded": recorded}

    @app.post("/facility/state/read")
    async def facility_state_read(
        body: dict,
        x_facility_token: str | None = Header(default=None),
    ):
        """Infra-only read of one challenge-declared authoritative region.

        The request carries only identities and an opaque region name. GameMgr
        resolves the fixed path from trusted challenge metadata and reads it via
        the host Docker API; no command or caller-provided path enters prod.
        """
        if not game.facility_token_ok(x_facility_token):
            return JSONResponse(status_code=403, content={"error": "forbidden"})
        result = await asyncio.to_thread(
            game.read_authoritative_state,
            str(body.get("team_id") or ""),
            str(body.get("service") or ""),
            str(body.get("region") or ""),
        )
        return {
            "status": result.status.value,
            "files": {
                name: base64.b64encode(content).decode("ascii")
                for name, content in result.files.items()
            },
            "detail": result.detail,
        }

    # ── control plane (admin-token) ─────────────────────────────────────────
    # start/stop mutate the whole match, so they require the admin credential
    # (X-Admin-Token, checked by _require_admin above) — distinct from the
    # player and facility tokens.
    @app.post("/game/start")
    async def start(x_admin_token: str | None = Header(default=None)):
        _require_admin(x_admin_token)
        return {"ok": True, "note": "game loop already running (started at boot)"}

    @app.post("/game/stop")
    async def stop(x_admin_token: str | None = Header(default=None)):
        _require_admin(x_admin_token)
        await game.stop()
        shutdown_event.set()
        return {"ok": True}

    @app.post("/game/flag/submit")
    async def submit_flag(body: dict, x_team_token: str | None = Header(default=None)):
        # Header is the only authoritative identity. body['team_id'] (if
        # present, from old M5 clients) is ignored. BAD_TOKEN does not
        # emit a FLAG_SUBMITTED event — we don't want the event stream to
        # become a token-probing oracle.
        if not x_team_token:
            return {"accepted": False, "reason": "BAD_TOKEN",
                    "detail": "X-Team-Token header missing or empty"}
        team_id = game._token_to_team.get(x_team_token)
        if team_id is None:
            return {"accepted": False, "reason": "BAD_TOKEN",
                    "detail": "X-Team-Token header missing or empty"}
        result = await game.submit_flag(team_id, body.get("flag", ""))
        return {
            "accepted": result.accepted,
            "reason": result.reason,
            "detail": result.detail,
        }

    # ── player-token REST mirrors of the GameMgr-coupled MCP tools ──────────
    # The containerized mcp-gateway (v3) forwards these here with the agent's
    # X-Team-Token; GameMgr resolves identity authoritatively and executes the
    # privileged op (e.g. docker restart stays host-side). 401 on bad token, so
    # the gateway can map it to the MCP Unauthorized error.

    def _require_team(x_team_token: str | None) -> str:
        team_id = game._token_to_team.get(x_team_token) if x_team_token else None
        if team_id is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return team_id

    @app.post("/verify/attempt")
    async def verify_attempt(
        body: dict, x_team_token: str | None = Header(default=None)
    ):
        """Solo-DEFENSE verifier report relay target (the mcp-gateway forwards the
        verifier's POST here with its X-Team-Token). GameMgr server-derives all
        attribution, records the Layer-2 event, and — on the report that latches
        `verified_fixed` — ends the match successfully and returns stop:true in
        THIS response. 409 outside solo-defense; 403 for a non-verifier token."""
        try:
            return game.record_verifier_attempt(x_team_token, body or {})
        except PermissionError:
            raise HTTPException(status_code=403, detail="not the verifier team")
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @app.get("/game/attacks")
    async def game_attacks(x_team_token: str | None = Header(default=None)):
        """Own-team attacks-received view (token-scoped). Mirrors the
        get_attacks_received MCP tool. Records MCP_ATTACKS_QUERY so the live
        panel shows the agent polling its breach count."""
        team_id = _require_team(x_team_token)
        result = game.attacks_received(team_id)
        game.timeline.record(
            EventType.MCP_ATTACKS_QUERY,
            team_id=team_id,
            round_id=result.get("current_round", 0),
            payload={
                "team_id": team_id,
                "total": result.get("total"),
                "this_round": result.get("this_round"),
                "by_service": result.get("by_service"),
            },
        )
        return result

    @app.get("/game/state")
    async def game_state(x_team_token: str | None = Header(default=None)):
        """Agent get_game_state (token-scoped) — separate from the tokenless
        /game/status that facilities poll, so MCP_STATE_QUERY is recorded for
        the agent's polls only (not facility status reads)."""
        team_id = _require_team(x_team_token)
        st = game.status()
        game.timeline.record(
            EventType.MCP_STATE_QUERY,
            team_id=team_id,
            round_id=st.get("round", 0),
            phase=st.get("phase", ""),
            payload={"team_id": team_id, "phase": st.get("phase"),
                     "round": st.get("round")},
        )
        return st

    @app.post("/game/report/ready")
    async def game_report_ready(x_team_token: str | None = Header(default=None)):
        team_id = _require_team(x_team_token)
        log.info(
            "agent readiness received team=%s gate_present=%s",
            team_id,
            team_id in game._ready_events,
        )
        await game.record_agent_ready(team_id)
        return {"ok": True}

    @app.post("/game/report/deploy-complete")
    async def game_report_deploy_complete(
        body: dict, x_team_token: str | None = Header(default=None)
    ):
        team_id = _require_team(x_team_token)
        service = str(body.get("service", ""))
        if not game.is_known_service(service):
            raise HTTPException(status_code=400, detail="unknown service")
        await game.record_deploy_complete(team_id, service)
        return {"ok": True}

    @app.post("/game/restart")
    async def game_restart(body: dict, x_team_token: str | None = Header(default=None)):
        """Agent restart of its own service. GameMgr executes the docker
        restart (host-side); returns {job_id}. 400 on the ValueError cases
        (unknown service / no restart_cmd / in-flight / wrong phase)."""
        team_id = _require_team(x_team_token)
        try:
            job_id = await game.restart_service(team_id, str(body.get("service", "")))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"job_id": job_id}

    @app.post("/game/health/trigger")
    async def game_health_trigger(
        body: dict, x_team_token: str | None = Header(default=None)
    ):
        """Agent-triggered functionality test on its own service(s). Mirrors
        the trigger_health_check MCP tool. Fire-and-forget: returns the
        services kicked off; results land as SERVICE_HEALTH_CHECK events. 400
        on unknown/non-defendable service (the ValueError cases)."""
        team_id = _require_team(x_team_token)
        service = body.get("service")
        service = str(service) if service else None
        try:
            return await game.trigger_health_check(team_id, service)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/game/health/recent")
    async def game_health_recent(
        limit: int | None = None, x_team_token: str | None = Header(default=None)
    ):
        """Own-team recent health-check runs (token-scoped). Mirrors the
        get_health_checks MCP tool. Records MCP_HEALTH_QUERY so the live panel
        shows the agent polling its health results."""
        team_id = _require_team(x_team_token)
        result = game.get_health_checks(team_id, limit)
        game.timeline.record(
            EventType.MCP_HEALTH_QUERY,
            team_id=team_id,
            payload={"team_id": team_id, "count": result.get("count")},
        )
        return result

    @app.get("/game/flags/checks")
    async def game_flag_checks(x_team_token: str | None = Header(default=None)):
        """Own-team flag-persistence status (token-scoped): did each planted flag
        survive the last completed round's audit. Mirrors the get_flag_checks MCP
        tool. Records MCP_FLAG_CHECK_QUERY so the live panel shows the poll."""
        team_id = _require_team(x_team_token)
        result = game.get_flag_checks(team_id)
        game.timeline.record(
            EventType.MCP_FLAG_CHECK_QUERY,
            team_id=team_id,
            payload={"team_id": team_id, "count": result.get("count")},
        )
        return result

    @app.websocket("/events/stream")
    async def events_stream(websocket: WebSocket):
        # Same admin gate as /game/events (it streams the same timeline). Checked
        # on the handshake headers before accept; the deploy WS relay forwards
        # X-Admin-Token. Close 1008 (policy violation) if it's missing/wrong.
        if not game.admin_token_ok(websocket.headers.get("x-admin-token")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        # attach_stream gives this connection its own queue; record() fans
        # out via put_nowait so a slow client cannot back-pressure the game.
        queue = game.timeline.attach_stream()
        # Race each receive against shutdown_event so this long-lived stream
        # CLOSES promptly when the server is winding down. Without it the
        # handler blocks on queue.get() forever, stalling uvicorn's graceful
        # drain → force_exit → a CancelledError logged from the cancelled
        # lifespan/MCP task on an otherwise-clean end-of-match.
        stopper = asyncio.ensure_future(shutdown_event.wait())
        try:
            for past in game.timeline.history():
                await websocket.send_json(asdict(past))
            while not shutdown_event.is_set():
                getter = asyncio.ensure_future(queue.get())
                done, _pending = await asyncio.wait(
                    {getter, stopper}, return_when=asyncio.FIRST_COMPLETED
                )
                if stopper in done:
                    getter.cancel()
                    break
                await websocket.send_json(asdict(getter.result()))
        except (WebSocketDisconnect, asyncio.CancelledError):
            # Client closed, or the server force-cancelled this stream —
            # both are clean exits, not errors.
            pass
        finally:
            stopper.cancel()
            game.timeline.detach_stream(queue)
            try:
                await websocket.close()
            except Exception:
                pass

    return app
