"""M4.5 — live Claude CLI ↔ MCP server smoke test.

Important: this is the **only** test that actually exercises the
end-to-end "Claude CLI parses mcp.json → speaks Streamable HTTP → hits
our server → fires the tool". Python SDK client tests pass even when
Claude CLI itself would fail (mcp.json format mismatch, header
forwarding, URL trailing-slash, TLS, etc.) — those failure modes live
*outside* the SDK's own client and only show up via a real CLI run.

Cost: one sonnet turn-loop, ~1-3 cents per run. Env-gated by
API_ROUTER_KEY just like the M4 hello-world / M5.6 attack tests; runs
nothing if the key is the .env placeholder.

Strategy: run uvicorn in-process (no docker), write mcp.json to a host
tmpdir pointing at 127.0.0.1, run `claude -p --mcp-config <file>` from
the host with a one-shot prompt asking it to call
mcp__cyberarena__report_ready. If the timeline sees AGENT_READY, the whole
chain works.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from gamemgr.api import build_app
from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.game_mgr import GameMgr
from gamemgr.mcp_server import MCP_PUBLIC_ENDPOINT
from models.topology import Team, TopologyGraph


HOST = "127.0.0.1"
PORT = 18900
TOKEN = "live-smoke-token"

CLAUDE_PROMPT = (
    "Use the mcp__cyberarena__report_ready tool exactly once, "
    "with no arguments. Report only the tool's response and then exit."
)
SMOKE_TIMEOUT_S = 60.0


def _have_real_router_key() -> bool:
    key = os.environ.get("API_ROUTER_KEY", "")
    return bool(key) and "placeholder" not in key


def _have_claude_cli() -> bool:
    return shutil.which("claude") is not None


pytestmark = pytest.mark.skipif(
    not (_have_real_router_key() and _have_claude_cli()),
    reason=(
        "set API_ROUTER_KEY in .env AND have `claude` on PATH to run the "
        "MCP smoke test"
    ),
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


def test_claude_cli_can_reach_mcp_server_and_call_report_ready():
    """Spin up in-process uvicorn, write a real mcp.json, run claude CLI
    against it, assert the timeline sees AGENT_READY within budget."""
    game = _make_game()
    app = build_app(game, asyncio.Event())
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True, name="m4_5-smoke")
    server_thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "uvicorn never started"

    try:
        with tempfile.TemporaryDirectory(prefix="cyberarena-mcp-smoke-") as td:
            mcp_json_path = Path(td) / "mcp.json"
            mcp_json_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "cyberarena": {
                                "type": "http",
                                "url": f"http://{HOST}:{PORT}{MCP_PUBLIC_ENDPOINT}",
                                "headers": {"X-Team-Token": TOKEN},
                            }
                        }
                    },
                    indent=2,
                )
                + "\n"
            )
            # chmod 600 — same hardening agent.env uses in-container.
            os.chmod(mcp_json_path, stat.S_IRUSR | stat.S_IWUSR)

            env = os.environ.copy()
            env["ANTHROPIC_API_KEY"] = env.get("API_ROUTER_KEY", "")
            base_url = env.get("API_ROUTER_BASE")
            if base_url:
                env["ANTHROPIC_BASE_URL"] = base_url
            env.setdefault("IS_SANDBOX", "1")

            cmd = [
                "claude",
                "-p",
                "--dangerously-skip-permissions",
                "--mcp-config", str(mcp_json_path),
                "--output-format", "stream-json",
                "--verbose",
                CLAUDE_PROMPT,
            ]
            print("\nrunning:", " ".join(cmd))
            proc = subprocess.run(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=SMOKE_TIMEOUT_S,
                text=True,
            )
            stdout_tail = proc.stdout[-2000:] if proc.stdout else ""
            stderr_tail = proc.stderr[-1500:] if proc.stderr else ""
            print("── claude exit:", proc.returncode)
            print("── stderr tail:", stderr_tail)
            print("── stdout tail:", stdout_tail)

            ready_events = [e for e in game.timeline.history() if e.type == "AGENT_READY"]
            assert ready_events, (
                "no AGENT_READY on the timeline — Claude CLI did not successfully "
                "reach the MCP server. This usually means: mcp.json format, "
                "URL trailing-slash, X-Team-Token header not being sent, or "
                "the SDK's internal route is no longer at our MCP_PUBLIC_ENDPOINT."
                f"\n  claude exit={proc.returncode}"
                f"\n  stderr={stderr_tail!r}"
            )
            assert ready_events[-1].team_id == "team1"
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
