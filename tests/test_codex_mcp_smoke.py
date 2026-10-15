"""Live Codex CLI ↔ MCP server smoke test.

The Codex analog of test_m4_5_claude_mcp_smoke.py: the only test that
exercises the real chain "codex exec reads config.toml → speaks
streamable HTTP → hits our MCP server with X-Team-Token (via
env_http_headers) → fires report_ready". Python SDK client tests can't
catch config.toml schema / header-forwarding / Responses-API routing
failures that only surface through a real `codex` run.

Cost: one gpt-5.4 turn, a few cents. Env-gated: skipped unless a Codex
key (CODEX_API_KEY_TEAM1 / CODEX_API_KEY) is set AND `codex` is on PATH.

Strategy: run uvicorn in-process (no docker), render config.toml via the
real CodexAgentManager pointed at 127.0.0.1, run `codex exec` from the
host asking it to call the report_ready tool. If the timeline records
AGENT_READY, the whole chain works.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from agents.codex import CodexAgentManager
from gamemgr.api import build_app
from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.game_mgr import GameMgr
from gamemgr.mcp_server import MCP_PUBLIC_ENDPOINT
from models.agent import AgentConfig
from models.topology import Team, TopologyGraph

# Best-effort: populate CODEX_* from .env so a local run picks up the key.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:
    pass

HOST = "127.0.0.1"
PORT = 18901
TOKEN = "live-codex-smoke-token"
SMOKE_TIMEOUT_S = 120.0

CODEX_PROMPT = (
    "Call the report_ready MCP tool (cyberarena server) exactly once, "
    "with no arguments. Then stop. Do nothing else."
)


def _codex_key() -> str:
    return os.environ.get("CODEX_API_KEY_TEAM1") or os.environ.get("CODEX_API_KEY") or ""


def _codex_base() -> str:
    return (
        os.environ.get("CODEX_BASE_URL_TEAM1")
        or os.environ.get("CODEX_BASE_URL")
        or "https://app-us.ppapi.ai/v1"
    )


pytestmark = pytest.mark.skipif(
    not (_codex_key() and shutil.which("codex")),
    reason="set CODEX_API_KEY_TEAM1 in .env AND have `codex` on PATH to run the codex MCP smoke",
)


class _StubOrchestrator:
    network = None


def _make_game() -> GameMgr:
    teams = [Team(id="team1", dev="d", prod="p", token=TOKEN)]
    topology = TopologyGraph(teams=teams, nodes=[], networks=[])
    cfg = GameConfig(
        topology=topology,
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000, deploy_on=["team1"])],
        rounds=RoundConfig(),
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def test_codex_cli_can_reach_mcp_server_and_call_report_ready(tmp_path):
    """In-process uvicorn + a real config.toml; run codex CLI against it
    and assert the timeline records AGENT_READY within budget."""
    game = _make_game()
    app = build_app(game, asyncio.Event())
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True, name="codex-smoke")
    server_thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "uvicorn never started"

    try:
        codex_home = tmp_path / "codex_home"
        codex_home.mkdir()
        # Render the SHIPPED config.toml via the real manager, pointed at
        # the in-process server (X-Team-Token via env_http_headers).
        agent_cfg = AgentConfig(
            cli="codex exec", provider="codex", model="gpt-5.4",
            api_key=_codex_key(), base_url=_codex_base(),
            team_id="team1", team_token=TOKEN,
            mcp_endpoint=f"http://{HOST}:{PORT}{MCP_PUBLIC_ENDPOINT}",
        )
        (codex_home / "config.toml").write_text(
            CodexAgentManager(transport=object(), client=None)._build_config_toml(agent_cfg)
        )

        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        env["CODEX_API_KEY"] = _codex_key()          # config.toml env_key
        env["CYBERARENA_TEAM_TOKEN"] = TOKEN          # config.toml env_http_headers

        cmd = [
            "codex", "exec", "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m", "gpt-5.4",
            CODEX_PROMPT,
        ]
        print("\nrunning:", " ".join(cmd))
        proc = subprocess.run(
            cmd, env=env, cwd=str(tmp_path),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, timeout=SMOKE_TIMEOUT_S, text=True,
        )
        print("── codex exit:", proc.returncode)
        print("── stderr tail:", (proc.stderr or "")[-1500:])
        print("── stdout tail:", (proc.stdout or "")[-2000:])

        ready = [e for e in game.timeline.history() if e.type == "AGENT_READY"]
        assert ready, (
            "no AGENT_READY on the timeline — codex CLI did not reach the MCP "
            "server. Usual causes: config.toml mcp_servers schema, X-Team-Token "
            "via env_http_headers not sent, Responses-API routing, or the URL "
            f"trailing-slash.\n  codex exit={proc.returncode}\n  stderr={(proc.stderr or '')[-800:]!r}"
        )
        assert ready[-1].team_id == "team1"
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
