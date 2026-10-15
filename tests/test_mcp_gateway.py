"""v3 step 2: the mcp-gateway. HttpBackend forwards each tool to the right
backend (submit -> flag-service directly; rest -> GameMgr REST), forwarding the
agent's X-Team-Token and mapping 401/400/BAD_TOKEN to the shared MCP errors.
Host + flag-service stubbed with httpx.MockTransport."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gamemgr.mcp_server import InvalidParams, Unauthorized
from mcpgateway.app import build_gateway_app
from mcpgateway.backend import HttpBackend

GM = "http://gm"
FS = "http://fs"


def _backend(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpBackend(GM, FS, client=client)


def _run(coro):
    return asyncio.run(coro)


# ── submit: direct to flag-service, BAD_TOKEN -> Unauthorized ─────────────────


def test_submit_forwards_to_flag_service():
    seen = {}

    def handler(req):
        assert req.url.host == "fs" and req.url.path == "/game/flag/submit"
        seen["token"] = req.headers.get("x-team-token")
        seen["flag"] = json.loads(req.content)["flag"]
        return httpx.Response(200, json={"accepted": True, "reason": "ACCEPTED",
                                         "detail": "submission accepted"})

    out = _run(_backend(handler).submit_flag("tok1", "FLAG{x}"))
    assert seen == {"token": "tok1", "flag": "FLAG{x}"}
    assert out == {"accepted": True, "reason": "ACCEPTED", "detail": "submission accepted"}


def test_claim_forwards_to_flag_service():
    seen = {}

    def handler(req):
        assert req.url.host == "fs" and req.url.path == "/game/flag/claim"
        seen["token"] = req.headers.get("x-team-token")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"accepted": True, "reason": "ACCEPTED",
                                         "detail": "submission accepted"})

    out = _run(_backend(handler).claim_capability("tok1", "team2", "notes", "ledger"))
    assert seen["token"] == "tok1"
    assert seen["body"] == {"victim_team_id": "team2", "service": "notes",
                            "store": "ledger"}
    assert out["accepted"] is True


def test_claim_bad_token_maps_to_unauthorized():
    def handler(req):
        return httpx.Response(200, json={"accepted": False, "reason": "BAD_TOKEN",
                                         "detail": "..."})
    with pytest.raises(Unauthorized):
        _run(_backend(handler).claim_capability("", "team2", "notes", "ledger"))


def test_claim_service_error_maps_to_retryable_observe_error():
    """A claim that could not REACH the service must read as "retry", never as
    "your write didn't land" — and in the CLAIM vocabulary: OBSERVE_ERROR is the
    reason that path defines for "could not tell", so the agent (and the prompt
    that names it) sees a verdict it recognizes."""
    from flag.store import REASON_DETAIL

    def handler(req):
        raise httpx.ReadTimeout("timed out", request=req)

    out = _run(_backend(handler).claim_capability("tok1", "team2", "notes", "ledger"))
    assert out["accepted"] is False and out["reason"] == "OBSERVE_ERROR"
    assert "retry" in out["detail"].lower()
    assert out["reason"] in REASON_DETAIL          # a defined claim outcome

    def five_oh_three(req):
        return httpx.Response(503, text="unavailable")

    out = _run(_backend(five_oh_three).claim_capability(
        "tok1", "team2", "notes", "ledger"))
    assert out["accepted"] is False and out["reason"] == "OBSERVE_ERROR"


def test_submit_bad_token_maps_to_unauthorized():
    def handler(req):
        return httpx.Response(200, json={"accepted": False, "reason": "BAD_TOKEN",
                                         "detail": "..."})
    with pytest.raises(Unauthorized):
        _run(_backend(handler).submit_flag("", "FLAG{x}"))


def test_submit_flag_service_timeout_maps_to_retryable_error():
    """If the flag-service is slow/unreachable (e.g. busy planting at a
    rotation), submit_flag must NOT raise an opaque MCP error — it returns a
    clear, retryable {accepted:false, reason:ERROR} so the agent retries."""
    def handler(req):
        raise httpx.ReadTimeout("timed out", request=req)
    out = _run(_backend(handler).submit_flag("tok1", "FLAG{x}"))
    assert out["accepted"] is False and out["reason"] == "ERROR"
    assert "retry" in out["detail"].lower()


def test_submit_flag_service_5xx_maps_to_retryable_error():
    def handler(req):
        return httpx.Response(503, text="unavailable")
    out = _run(_backend(handler).submit_flag("tok1", "FLAG{x}"))
    assert out == {"accepted": False, "reason": "ERROR",
                   "detail": "flag service temporarily unavailable — retry this submission"}


# ── get_attack_targets: direct to flag-service, BAD_TOKEN -> Unauthorized ─────


def test_get_attack_targets_forwards_to_flag_service():
    seen = {}

    def handler(req):
        assert req.url.host == "fs" and req.url.path == "/game/attack-info"
        seen["token"] = req.headers.get("x-team-token")
        return httpx.Response(200, json={"targets": [
            {"service": "chat_app", "host": "team2_prod", "store": "default",
             "flag_id": "user-team2", "round_id": 3}]})

    out = _run(_backend(handler).get_attack_targets("tok1"))
    assert seen["token"] == "tok1"
    assert out["targets"][0]["flag_id"] == "user-team2"
    # a flag-service that publishes no principals still answers both arrays
    assert out["principals"] == []


def test_get_attack_targets_forwards_baseline_principals():
    """The sibling `principals` array (this team's match-long logins) rides
    through the gateway verbatim, alongside the unchanged `targets`."""
    def handler(req):
        return httpx.Response(200, json={
            "targets": [{"service": "chat_app", "host": "team2_prod",
                         "store": "default", "flag_id": "user-team2",
                         "round_id": 3}],
            "principals": [{"team_id": "team2", "service": "chat_app",
                            "host": "team2_prod", "principal_id": "u-91ab",
                            "credentials": {"username": "u-91ab",
                                            "password": "pw-91ab"}}],
        })

    out = _run(_backend(handler).get_attack_targets("tok1"))
    assert out["targets"][0]["flag_id"] == "user-team2"
    assert out["principals"][0]["credentials"]["password"] == "pw-91ab"


def test_get_attack_targets_bad_token_maps_to_unauthorized():
    def handler(req):
        return httpx.Response(200, json={"error": "BAD_TOKEN", "detail": "..."})
    with pytest.raises(Unauthorized):
        _run(_backend(handler).get_attack_targets(""))


def test_get_attack_targets_flag_service_down_maps_to_empty():
    def handler(req):
        raise httpx.ReadTimeout("timed out", request=req)
    assert _run(_backend(handler).get_attack_targets("tok1")) == {
        "targets": [], "principals": []}


# ── GameMgr-coupled tools forward to gm with the token ───────────────────────


def test_state_attacks_report_restart_route_to_gm():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, req.headers.get("x-team-token")))
        if req.url.path == "/game/state":
            return httpx.Response(200, json={"phase": "ATTACK", "round": 1})
        if req.url.path == "/game/attacks":
            return httpx.Response(200, json={"team_id": "team1", "total": 0})
        if req.url.path == "/game/restart":
            return httpx.Response(200, json={"job_id": "abc123"})
        return httpx.Response(200, json={"ok": True})

    b = _backend(handler)
    assert _run(b.get_game_state("tok1"))["phase"] == "ATTACK"
    assert _run(b.get_attacks_received("tok1"))["team_id"] == "team1"
    assert _run(b.report_ready("tok1")) == {"ok": True}
    assert _run(b.report_deploy_complete("tok1", "notes")) == {"ok": True}
    assert _run(b.restart_service("tok1", "notes")) == {"job_id": "abc123"}

    paths = {(m, p) for m, p, _ in calls}
    assert ("GET", "/game/state") in paths      # get_game_state -> token-scoped /game/state
    assert ("GET", "/game/attacks") in paths
    assert ("POST", "/game/restart") in paths
    # token forwarded on ALL gateway->GM calls now (get_game_state included)
    assert all(tok == "tok1" for m, p, tok in calls)


def test_health_tools_route_to_gm():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, req.url.params.get("limit")))
        if req.url.path == "/game/health/trigger":
            return httpx.Response(200, json={"triggered": ["nautilus-ico"]})
        if req.url.path == "/game/health/recent":
            return httpx.Response(200, json={"team_id": "team1", "count": 0, "checks": []})
        return httpx.Response(200, json={"ok": True})

    b = _backend(handler)
    assert _run(b.trigger_health_check("tok1", None)) == {"triggered": ["nautilus-ico"]}
    assert _run(b.get_health_checks("tok1", 5))["count"] == 0

    assert ("POST", "/game/health/trigger", None) in calls
    # limit is forwarded as a query param.
    assert ("GET", "/game/health/recent", "5") in calls


def test_get_flag_checks_routes_to_gm():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, req.headers.get("x-team-token")))
        return httpx.Response(200, json={"team_id": "team1", "count": 0, "flags": []})

    b = _backend(handler)
    assert _run(b.get_flag_checks("tok1"))["count"] == 0
    assert ("GET", "/game/flags/checks", "tok1") in calls


def test_401_maps_to_unauthorized():
    def handler(req): return httpx.Response(401, json={"detail": "Unauthorized"})
    with pytest.raises(Unauthorized):
        _run(_backend(handler).get_attacks_received("bad"))


def test_400_maps_to_invalid_params():
    def handler(req): return httpx.Response(400, json={"detail": "unknown service"})
    with pytest.raises(InvalidParams):
        _run(_backend(handler).restart_service("tok1", "ghost"))


# ── app builds + mounts MCP + healthz ─────────────────────────────────────────


def test_gateway_app_healthz_and_mount():
    app = build_gateway_app(gm_base_url=GM, flag_service_url=FS,
                            client=httpx.AsyncClient(transport=httpx.MockTransport(
                                lambda r: httpx.Response(200, json={}))))
    with TestClient(app) as tc:  # context => lifespan runs the MCP session manager
        h = tc.get("/healthz").json()
        assert h["status"] == "ok" and h["endpoint"] == "/mcp/"
        # MCP is mounted at /mcp/ (a bare GET is not a valid MCP call, but the
        # route exists — not a 404).
        assert tc.get("/mcp/").status_code != 404
