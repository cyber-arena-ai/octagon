"""`dockerfiles/agent/codex/codex-security-mcp` — the local stdio MCP server that advertises the
security scanner to the model as a tool.

Driven here as a real subprocess over the stdio transport (newline-delimited JSON-RPC), so
what is pinned is the wire behaviour codex will actually see: the handshake, the advertised
tool list, and that a bad call is reported as a tool error rather than killing the session.
No scan is started — that needs `codex-security` plus a routed model — so the tests stay to the paths
that fail before any spend.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).parent.parent / "dockerfiles" / "agent" / "codex" / "codex-security-mcp"


class _Client:
    """Minimal MCP stdio client: write one JSON line, read one JSON line."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

    def call(self, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"server closed the stream on {method}"
        return json.loads(line)

    def notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


@pytest.fixture
def client():
    c = _Client()
    yield c
    c.close()


def test_handshake_echoes_the_client_protocol_version(client):
    """A newer or older codex must negotiate cleanly rather than fail the handshake."""
    r = client.call("initialize", {"protocolVersion": "2024-11-05",
                                   "capabilities": {}, "clientInfo": {"name": "t"}})
    assert r["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in r["result"]["capabilities"]
    assert r["result"]["serverInfo"]["name"] == "arena-scanner"


def test_advertises_the_trigger_and_poll_pair(client):
    """Trigger+poll, mirroring the arena's own trigger_health_check / get_health_checks:
    a scan runs for minutes, and an MCP call must not block on it."""
    client.call("initialize", {"protocolVersion": "2025-06-18"})
    client.notify("notifications/initialized")
    tools = client.call("tools/list", msg_id=2)["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"trigger_security_scan", "get_security_scan"}
    trigger = next(t for t in tools if t["name"] == "trigger_security_scan")
    assert trigger["inputSchema"]["required"] == ["path"]
    # The description has to tell the model the shape of the interaction, since nothing
    # else will: it is asynchronous, local-only, and read-only.
    assert "poll" in trigger["description"].lower()


def test_bad_path_is_a_tool_error_not_a_crash(client):
    client.call("initialize", {"protocolVersion": "2025-06-18"})
    r = client.call("tools/call",
                    {"name": "trigger_security_scan",
                     "arguments": {"path": "/definitely/not/here"}}, msg_id=3)
    assert r["result"]["isError"] is True
    assert "not a directory" in json.loads(r["result"]["content"][0]["text"])["error"]
    # the session must survive it
    assert client.call("tools/list", msg_id=4)["result"]["tools"]


def test_unknown_scan_id_and_unknown_tool_are_handled(client):
    client.call("initialize", {"protocolVersion": "2025-06-18"})
    r = client.call("tools/call",
                    {"name": "get_security_scan", "arguments": {"scan_id": "nope"}}, msg_id=5)
    assert "unknown scan_id" in json.loads(r["result"]["content"][0]["text"])["error"]

    r = client.call("tools/call", {"name": "no_such_tool", "arguments": {}}, msg_id=6)
    assert r["error"]["code"] == -32602


def test_garbage_line_does_not_kill_the_session(client):
    """codex writes framed JSON, but a half-line from a crash must not end the server."""
    client.proc.stdin.write("this is not json\n")
    client.proc.stdin.flush()
    r = client.call("initialize", {"protocolVersion": "2025-06-18"}, msg_id=7)
    assert r["result"]["serverInfo"]["name"] == "arena-scanner"
