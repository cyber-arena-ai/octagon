"""apirouter.relay — narrow verbatim reverse proxy to one upstream + config.

Drives the relay app with FastAPI's TestClient and a stubbed httpx upstream
(MockTransport), asserting it forwards method/path/query/headers/body verbatim to
`base_url + <path>` and nowhere else.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from apirouter.config import from_dict
from apirouter.relay import build_relay_app

BASE = "http://gameserver:8000"


def _app_with_capture():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("x-team-token")
        seen["accept_encoding"] = request.headers.get("accept-encoding")
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(build_relay_app(BASE, client=client)), seen


def test_relay_forwards_verbatim():
    tc, seen = _app_with_capture()
    r = tc.post("/mcp/", headers={"X-Team-Token": "tok-team1"}, content=b'{"x":1}')
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert seen["url"] == f"{BASE}/mcp/"          # base + path, single upstream
    assert seen["method"] == "POST"
    assert seen["token"] == "tok-team1"            # player token passed through
    assert seen["body"] == b'{"x":1}'
    assert seen["accept_encoding"] == "identity"   # forces uncompressed passthrough


def test_relay_preserves_path_and_query():
    tc, seen = _app_with_capture()
    tc.get("/game/status", params={"since": "5"})
    assert seen["url"] == f"{BASE}/game/status?since=5"


def test_relay_is_single_upstream():
    """Whatever path the client asks for, it only ever hits base_url + path —
    never an arbitrary host. (Path can't escape the fixed base.)"""
    tc, seen = _app_with_capture()
    tc.get("/game/events")
    assert seen["url"].startswith(BASE + "/")


def test_relay_healthz():
    tc, _ = _app_with_capture()
    r = tc.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


# ── config ──────────────────────────────────────────────────────────────


def _cfg(relay_block: dict | None):
    raw = {"upstream": {"base_url": "https://up.example", "api_key": "K"}}
    if relay_block is not None:
        raw["relay"] = relay_block
    return from_dict(raw)


def test_relay_disabled_by_default():
    assert _cfg(None).relay.enabled is False


def test_relay_enabled_when_base_url_set():
    c = _cfg({"listen": "0.0.0.0:8000", "base_url": "http://host.docker.internal:8000"})
    assert c.relay.enabled is True
    assert c.relay.host == "0.0.0.0" and c.relay.port == 8000
    assert c.relay.base_url == "http://host.docker.internal:8000"
