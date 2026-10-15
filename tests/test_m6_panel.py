"""M6 — panel smoke.

Three layers:
  1. file existence + required HTML hooks (no FastAPI, no browser)
  2. FastAPI TestClient: GET /panel returns the file, /game/events returns
     JSON the panel JS can consume
  3. Optional headless-chrome render: skipped if google-chrome isn't on PATH;
     otherwise renders /panel and asserts a non-trivial PNG comes out
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamemgr.api import PANEL_HTML_PATH, build_app
from gamemgr.config import load as load_game_config
from gamemgr.game_mgr import GameMgr
from orchestrator.orchestrator import Orchestrator

CONFIG = Path(__file__).parent.parent / "config" / "legacy" / "game.yaml"


# ── layer 1: file + required IDs ────────────────────────────


def test_panel_html_exists():
    assert PANEL_HTML_PATH.is_file(), f"panel missing: {PANEL_HTML_PATH}"
    content = PANEL_HTML_PATH.read_text()
    # Anchors the panel JS / report-screenshot pipeline depends on:
    for needle in (
        'id="round-num"', 'id="phase"',
        'id="timeline"', 'id="controls"', 'id="conn"',
        'fetch(\'/game/events\')',     # backfill path
        '/events/stream',              # WS path
        'Cyber Arena',                 # title text
    ):
        assert needle in content, f"panel.html missing hook: {needle!r}"


# ── layer 2: FastAPI surface ────────────────────────────────


@pytest.fixture
def app_client(monkeypatch):
    # GameMgr requires a topology config; just load the real game.yaml — no
    # docker actions taken until you call orch.setup(), which we don't.
    cfg = load_game_config(CONFIG)
    # We never start docker here; mocking an Orchestrator with the minimum
    # surface GameMgr needs at construction:
    orch = Orchestrator(graph=cfg.topology)
    game = GameMgr(config=cfg, orchestrator=orch)
    game._admin_token = "admintok"   # /panel is admin-gated now
    shutdown = asyncio.Event()
    return TestClient(build_app(game, shutdown))


def test_panel_route_serves_html(app_client):
    # /panel is admin-only (the deploy backend injects the token when it proxies
    # the authenticated human); a tokenless / player request is refused.
    assert app_client.get("/panel").status_code == 403
    r = app_client.get("/panel", headers={"X-Admin-Token": "admintok"})
    assert r.status_code == 200
    assert "Cyber Arena" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_events_route_is_json_array(app_client):
    # /game/events is admin-only now (the fixture set the admin token).
    assert app_client.get("/game/events").status_code == 403
    r = app_client.get("/game/events", headers={"X-Admin-Token": "admintok"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)   # empty pre-start, but valid JSON


def test_events_stream_closes_when_shutting_down():
    """The /events/stream WS must return (close) once shutdown is signalled,
    instead of blocking forever — otherwise it stalls uvicorn's graceful
    drain at end-of-match. Here shutdown is pre-set, so the handler should
    close promptly rather than hang on queue.get()."""
    from fastapi import WebSocketDisconnect

    cfg = load_game_config(CONFIG)
    game = GameMgr(config=cfg, orchestrator=Orchestrator(graph=cfg.topology))
    game._admin_token = "admintok"   # WS is admin-gated; connect as admin so we
    shutdown = asyncio.Event()       # exercise the shutdown-close path, not auth
    shutdown.set()
    client = TestClient(build_app(game, shutdown))

    with client.websocket_connect(
            "/events/stream", headers={"X-Admin-Token": "admintok"}) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# ── layer 3: real chrome render (skipped if chrome absent) ──


CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


@pytest.mark.skipif(CHROME is None, reason="no chrome on PATH")
def test_panel_renders_in_headless_chrome():
    """Bring up a real uvicorn, fetch the (admin-gated) panel HTML, render it in
    headless chrome, sanity-check that something non-trivial was drawn. /panel is
    admin-only now and chrome can't send an X-Admin-Token header, so we fetch the
    served HTML with the token (as the deploy proxy does) and render it from a
    local file — exercises the same panel.html + CSS the browser gets."""
    import urllib.request
    import uvicorn

    cfg = load_game_config(CONFIG)
    orch = Orchestrator(graph=cfg.topology)
    game = GameMgr(config=cfg, orchestrator=orch)
    game._admin_token = "admintok"
    shutdown = asyncio.Event()
    app = build_app(game, shutdown)
    port = 8765   # dedicated test port
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    # wait for bind
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "uvicorn never bound"

    try:
        with tempfile.TemporaryDirectory() as td:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/panel", headers={"X-Admin-Token": "admintok"})
            html = urllib.request.urlopen(req, timeout=10).read()
            page = os.path.join(td, "panel.html")
            with open(page, "wb") as fh:
                fh.write(html)
            out = os.path.join(td, "panel.png")
            rc = subprocess.run(
                [
                    CHROME, "--headless", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars",
                    "--window-size=1400,900",
                    f"--screenshot={out}",
                    f"--virtual-time-budget=1500",  # let JS run for 1.5s
                    f"file://{page}",
                ],
                capture_output=True, timeout=30,
            )
            assert rc.returncode == 0, f"chrome failed: {rc.stderr.decode(errors='replace')!r}"
            size = os.path.getsize(out)
            assert size > 5000, f"screenshot suspiciously small ({size} bytes)"
    finally:
        server.should_exit = True
        th.join(timeout=5)
