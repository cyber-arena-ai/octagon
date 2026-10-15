"""Tests for agent stream-json transcript parsing + the /game/agents
endpoint that feeds the panel's transcript section."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamemgr.api import build_app
from gamemgr.config import load as load_game_config
from gamemgr.game_mgr import GameMgr
from gamemgr.transcripts import (collect_transcripts, merge_grok_deltas,
                                 parse_stream_json, summary_stats, _unpack_line)
from orchestrator.orchestrator import Orchestrator

CONFIG = Path(__file__).parent.parent / "config" / "legacy" / "game.yaml"


# ── sample stream-json ─────────────────────────────────────────────────

_SYSTEM = {"type": "system", "subtype": "init", "model": "claude-x", "tools": ["Bash", "Read"]}
_ASSISTANT = {
    "type": "assistant",
    "message": {
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "content": [
            {"type": "thinking", "thinking": "let me look around"},
            {"type": "text", "text": "I'll list the files."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la", "description": "list dir"}},
        ],
    },
}
_ASSISTANT_CACHED = {
    "type": "assistant",
    "message": {
        "usage": {
            "input_tokens": 10, "output_tokens": 2,
            "cache_read_input_tokens": 500, "cache_creation_input_tokens": 50,
        },
        "content": [{"type": "text", "text": "hi"}],
    },
}
_TOOL_OK = {"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "file1\nfile2", "is_error": False},
]}}
_TOOL_ERR = {"type": "user", "message": {"content": [
    {"type": "tool_result", "content": [{"type": "text", "text": "boom"}], "is_error": True},
]}}

_DSH_ASSISTANT = {
    "type": "dsh", "ts": 1786000001.0, "role": "assistant",
    "thought": "inspect the service",
    "text": "I will inspect it.",
    "tool_calls": [{
        "id": "call_1",
        "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"},
    }],
}
_DSH_TOOL = {
    "type": "dsh", "ts": 1786000002.0, "role": "tool",
    "tool_call_id": "call_1", "name": "bash", "content": "ok\n",
    "is_error": False,
}
_DSH_USAGE = {
    "type": "dsh", "ts": 1786000003.0, "role": "usage",
    "input_tokens": 120, "output_tokens": 30, "cached_input_tokens": 90,
}


def _write_stream(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o) for o in lines) + "\n")


# ── parse_stream_json ──────────────────────────────────────────────────


def test_parse_unpacks_each_block_in_order(tmp_path):
    p = tmp_path / "stream-defense.jsonl"
    _write_stream(p, [_SYSTEM, _ASSISTANT, _TOOL_OK, _TOOL_ERR])
    events = parse_stream_json(p)

    kinds = [e.kind for e in events]
    assert kinds == ["system", "thinking", "text", "tool_use", "tool_result", "tool_result"]

    tool_use = events[3]
    assert tool_use.tool_name == "Bash"
    assert "list dir" in tool_use.title

    # Per-turn usage is attached to the turn's first event only (here the
    # thinking block), so summing across events can't multiply-count it.
    assert events[1].kind == "thinking"
    assert events[1].tokens_in == 100 and events[1].tokens_out == 20
    assert tool_use.tokens_in is None and tool_use.tokens_out is None

    assert events[4].is_error is False
    assert events[5].is_error is True
    assert events[5].title.startswith("ERR ")
    assert "boom" in events[5].body


def test_parse_skips_malformed_and_blank_lines(tmp_path):
    p = tmp_path / "stream-attack.jsonl"
    p.write_text("\n".join([json.dumps(_SYSTEM), "not json {", "", json.dumps(_TOOL_OK)]) + "\n")
    events = parse_stream_json(p)
    assert [e.kind for e in events] == ["system", "tool_result"]


def test_parse_missing_file_returns_empty(tmp_path):
    assert parse_stream_json(tmp_path / "nope.jsonl") == []


def test_parse_joins_timing_sidecar_ts_by_line(tmp_path):
    """Each event carries the arrival ts of its stream LINE from the `.timing`
    sidecar; the several events off one line share that line's ts, and a line
    with no sidecar entry gets ts=None (so the live feed has per-event time)."""
    p = tmp_path / "stream-async-0.jsonl"
    _write_stream(p, [_SYSTEM, _ASSISTANT, _TOOL_OK])   # physical lines 1, 2, 3
    # sidecar: "<line_no>\t<epoch>"; omit line 3 to prove missing -> None
    Path(str(p) + ".timing").write_text("1\t1000.5\n2\t1002.25\n")

    events = parse_stream_json(p)
    assert [e.kind for e in events] == [
        "system", "thinking", "text", "tool_use", "tool_result"]
    assert events[0].ts == 1000.5                       # line 1 (system)
    # line 2 unpacks to 3 events — thinking/text/tool_use all share line 2's ts
    assert [e.ts for e in events[1:4]] == [1002.25, 1002.25, 1002.25]
    assert events[4].ts is None                         # line 3: no sidecar entry


def test_parse_without_timing_sidecar_leaves_ts_none(tmp_path):
    p = tmp_path / "stream-async-0.jsonl"
    _write_stream(p, [_SYSTEM, _TOOL_OK])
    assert all(e.ts is None for e in parse_stream_json(p))


# ── Gemini CLI (`--output-format stream-json`): its own init/message/tool_* schema
def test_parse_gemini_cli_stream(tmp_path):
    """Real Gemini CLI shape (from a live run): init→system, assistant
    message→text, tool_use/tool_result map through; the `user` prompt and the
    final `result` usage summary are dropped."""
    p = tmp_path / "stream-async-0.jsonl"
    _write_stream(p, [
        {"type": "init", "timestamp": "t0", "session_id": "s", "model": "gemini-3-flash-preview"},
        {"type": "message", "timestamp": "t1", "role": "user", "content": "THE TASK …"},
        {"type": "tool_use", "timestamp": "t2", "tool_name": "run_bash", "tool_id": "c1",
         "parameters": {"command": "curl target"}},
        {"type": "tool_result", "timestamp": "t3", "tool_id": "c1", "status": "success",
         "output": "HTTP 200 body"},
        {"type": "message", "timestamp": "t4", "role": "assistant",
         "content": "I recovered the flag.", "delta": True},
        {"type": "result", "timestamp": "t5", "status": "success", "stats": {"total_tokens": 10}},
    ])
    events = parse_stream_json(p)
    assert [e.kind for e in events] == ["system", "tool_use", "tool_result", "text"]
    assert events[0].title.startswith("session init") and "gemini-3-flash-preview" in events[0].title
    assert events[1].tool_name == "run_bash" and "curl target" in events[1].title
    assert events[2].is_error is False and "HTTP 200" in events[2].body
    assert "recovered the flag" in events[3].body


def test_parse_gemini_tool_result_error_flags_is_error(tmp_path):
    p = tmp_path / "stream-async-0.jsonl"
    _write_stream(p, [
        {"type": "tool_result", "timestamp": "t", "tool_id": "c1", "status": "error",
         "output": "boom"},
    ])
    ev = parse_stream_json(p)[0]
    assert ev.kind == "tool_result" and ev.is_error is True and ev.title.startswith("ERR ")


# ── summary_stats ──────────────────────────────────────────────────────


def test_summary_stats_counts_tools_tokens_errors(tmp_path):
    p = tmp_path / "stream-defense.jsonl"
    _write_stream(p, [_SYSTEM, _ASSISTANT, _TOOL_OK, _TOOL_ERR])
    stats = summary_stats(parse_stream_json(p))
    assert stats["events"] == 6
    assert stats["tools"] == 1
    assert "Bash" in stats["tools_breakdown"]
    assert stats["errors"] == 1
    assert stats["tokens_in"] == 100 and stats["tokens_out"] == 20
    assert stats["tokens_cached"] == 0   # no cache fields in this sample


def test_summary_stats_sums_cached_tokens(tmp_path):
    """cache_read + cache_creation input tokens roll up into tokens_cached,
    separate from the uncached tokens_in."""
    p = tmp_path / "stream-defense.jsonl"
    _write_stream(p, [_ASSISTANT_CACHED])
    stats = summary_stats(parse_stream_json(p))
    assert stats["tokens_in"] == 10
    assert stats["tokens_out"] == 2
    assert stats["tokens_cached"] == 550   # 500 read + 50 creation


def test_parse_dsh_trajectory_preserves_assistant_tool_and_usage_cards(tmp_path):
    p = tmp_path / "stream-async-0.jsonl"
    _write_stream(p, [_DSH_ASSISTANT, _DSH_TOOL, _DSH_USAGE])

    events = parse_stream_json(p)
    assert [e.kind for e in events] == ["thinking", "text", "tool_use", "tool_result", "system"]
    assert events[0].body == "inspect the service"
    assert events[2].tool_name == "bash"
    assert '"command": "ls"' in events[2].body
    assert events[3].body == "ok\n" and events[3].is_error is False
    assert events[4].tokens_in == 120
    assert events[4].tokens_out == 30
    assert events[4].tokens_cached == 90


def test_non_dsh_schemas_continue_to_dispatch_as_before(tmp_path):
    p = tmp_path / "stream-defense.jsonl"
    _write_stream(p, [_SYSTEM, _ASSISTANT])
    events = parse_stream_json(p)
    assert [e.kind for e in events] == ["system", "thinking", "text", "tool_use"]


# ── codex `exec --json` schema ─────────────────────────────────────────

_CODEX_THREAD = {"type": "thread.started", "thread_id": "019eb8a2-dafe-7d70-8502-62ca5c29bd78"}
_CODEX_MSG = {"type": "item.completed", "item": {
    "id": "i0", "type": "agent_message", "text": "Patching the bounds check."}}
_CODEX_SHELL = {"type": "item.completed", "item": {
    "id": "i1", "type": "command_execution",
    "command": "ssh team1_prod 'ls'", "aggregated_output": "color.pas\n", "exit_code": 0}}
_CODEX_SHELL_ERR = {"type": "item.completed", "item": {
    "id": "i2", "type": "command_execution",
    "command": "false", "aggregated_output": "nope", "exit_code": 1}}
_CODEX_FILE = {"type": "item.completed", "item": {
    "id": "i3", "type": "file_change",
    "changes": [{"path": "/srv/challenge/ico/color.pas", "kind": "update"}], "status": "completed"}}
_CODEX_MCP = {"type": "item.completed", "item": {
    "id": "i4", "type": "mcp_tool_call", "server": "cyberarena", "tool": "restart_service",
    "arguments": {"service": "nautilus-ico"},
    "result": {"content": [{"type": "text", "text": "{\"job_id\": \"abc\"}"}]},
    "error": None, "status": "completed"}}
# item.started for the same id must NOT produce a duplicate card.
_CODEX_MCP_STARTED = {"type": "item.started", "item": {
    "id": "i4", "type": "mcp_tool_call", "server": "cyberarena", "tool": "restart_service",
    "status": "in_progress"}}
_CODEX_TURN = {"type": "turn.completed", "usage": {
    "input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 80}}


def test_parse_codex_stream_unpacks_items(tmp_path):
    p = tmp_path / "stream-defense.jsonl"
    _write_stream(p, [
        _CODEX_THREAD, _CODEX_MSG, _CODEX_SHELL, _CODEX_SHELL_ERR,
        _CODEX_FILE, _CODEX_MCP_STARTED, _CODEX_MCP, _CODEX_TURN,
    ])
    events = parse_stream_json(p)
    kinds = [e.kind for e in events]
    # thread.started→system, agent_message→text, 2× shell (tool_use+result),
    # file_change→tool_use, mcp_tool_call→tool_use+result, turn.completed→system.
    # item.started produces nothing (no duplicate of the mcp card).
    assert kinds == [
        "system", "text",
        "tool_use", "tool_result",     # shell ok
        "tool_use", "tool_result",     # shell err
        "tool_use",                    # file_change
        "tool_use", "tool_result",     # mcp_tool_call
        "system",                      # turn.completed
    ]
    # shell error surfaces as an error tool_result (events[5]; events[3] is
    # the OK shell's result)
    assert events[3].is_error is False
    assert events[5].is_error is True
    # mcp tool card is addressed server.tool
    mcp_card = next(e for e in events if e.tool_name == "cyberarena.restart_service")
    assert "nautilus-ico" in mcp_card.body
    # file_change card names the path
    file_card = next(e for e in events if e.tool_name == "file_change")
    assert "color.pas" in file_card.title


def test_codex_summary_stats(tmp_path):
    p = tmp_path / "stream-defense.jsonl"
    _write_stream(p, [_CODEX_THREAD, _CODEX_MSG, _CODEX_SHELL, _CODEX_MCP, _CODEX_TURN])
    stats = summary_stats(parse_stream_json(p))
    # tokens roll up from turn.completed (codex reports usage per-turn)
    assert stats["tokens_in"] == 1200
    assert stats["tokens_out"] == 80
    assert stats["tokens_cached"] == 1000
    assert "shell" in stats["tools_breakdown"]
    assert "cyberarena.restart_service" in stats["tools_breakdown"]


# ── collect_transcripts ────────────────────────────────────────────────


def test_collect_groups_and_sorts_by_team_round_phase(tmp_path):
    arts = tmp_path / "artifacts"
    _write_stream(arts / "team2" / "round-1" / "stream-attack.jsonl", [_SYSTEM, _ASSISTANT])
    _write_stream(arts / "team1" / "round-1" / "stream-attack.jsonl", [_TOOL_OK])
    _write_stream(arts / "team1" / "round-1" / "stream-defense.jsonl", [_SYSTEM])
    _write_stream(arts / "team1" / "round-2" / "stream-attack.jsonl", [_TOOL_OK])

    out = collect_transcripts(tmp_path)
    # Chronological within a round: defense before attack (NOT alphabetical,
    # which would put R1 attack ahead of R1 defense).
    assert [(t["team_id"], t["round_id"], t["phase"]) for t in out] == [
        ("team1", 1, "defense"),
        ("team1", 1, "attack"),
        ("team1", 2, "attack"),
        ("team2", 1, "attack"),
    ]
    assert out[0]["stats"]["events"] == 1
    assert isinstance(out[0]["events"], list)


def test_collect_skips_empty_transcripts(tmp_path):
    arts = tmp_path / "artifacts"
    (arts / "team1" / "round-1").mkdir(parents=True)
    (arts / "team1" / "round-1" / "stream-defense.jsonl").write_text("")
    assert collect_transcripts(tmp_path) == []


def test_collect_no_artifacts_dir_returns_empty(tmp_path):
    assert collect_transcripts(tmp_path) == []


def test_collect_transcripts_caches_then_invalidates_on_change(tmp_path):
    """A poll with no file change reuses the parsed result (same object);
    a new/changed stream file invalidates the cache and re-parses."""
    arts = tmp_path / "artifacts"
    _write_stream(arts / "team1" / "round-1" / "stream-defense.jsonl", [_SYSTEM])
    out1 = collect_transcripts(tmp_path)
    assert len(out1) == 1
    # Unchanged → cache hit returns the very same object (no re-parse).
    assert collect_transcripts(tmp_path) is out1
    # New file → signature changes → re-parse, now reflects both.
    _write_stream(arts / "team1" / "round-1" / "stream-attack.jsonl", [_TOOL_OK])
    out2 = collect_transcripts(tmp_path)
    assert len(out2) == 2
    assert out2 is not out1


# ── /game/agents endpoint ──────────────────────────────────────────────


_ADMIN = {"X-Admin-Token": "admintok"}   # /game/agents is admin/team gated now


@pytest.fixture
def app_client():
    cfg = load_game_config(CONFIG)
    orch = Orchestrator(graph=cfg.topology)
    game = GameMgr(config=cfg, orchestrator=orch)
    game._admin_token = "admintok"   # admin token = the all-team (panel) view
    shutdown = asyncio.Event()
    return game, TestClient(build_app(game, shutdown))


def test_agents_endpoint_empty_before_run(app_client):
    game, client = app_client
    assert game.run_dir is None
    r = client.get("/game/agents", headers=_ADMIN)
    assert r.status_code == 200
    assert r.json() == {"run": None, "transcripts": []}


def test_agents_endpoint_serves_parsed_transcripts(app_client, tmp_path):
    game, client = app_client
    _write_stream(
        tmp_path / "artifacts" / "team1" / "round-1" / "stream-defense.jsonl",
        [_SYSTEM, _ASSISTANT, _TOOL_OK],
    )
    game.run_dir = tmp_path  # simulate a started run

    # /game/agents now returns ONE merged trajectory per team (was one-per-chunk).
    body = client.get("/game/agents", headers=_ADMIN).json()
    assert body["run"] == tmp_path.name
    assert len(body["transcripts"]) == 1
    tr = body["transcripts"][0]
    assert (tr["team_id"], tr["phase"], tr["chunks"]) == ("team1", "merged", 1)
    assert tr["round_id"] is None
    assert tr["stats"]["tools"] == 1
    assert [e["kind"] for e in tr["events"]][:2] == ["system", "thinking"]

# ── opencode `run --format json` schema ────────────────────────────────

_OC_TEXT = {"type": "text", "sessionID": "ses_1", "part": {
    "type": "text", "messageID": "msg_1", "text": "Checking the game state first."}}
_OC_TOOL = {"type": "tool_use", "sessionID": "ses_1", "part": {
    "type": "tool", "tool": "cyberarena_get_game_state", "callID": "tc1",
    "state": {"status": "completed", "input": {}, "output": '{"phase": "LIVE"}'}}}
_OC_TOOL_SHELL = {"type": "tool_use", "sessionID": "ses_1", "part": {
    "type": "tool", "tool": "bash", "callID": "tc2",
    "state": {"status": "completed", "input": {"command": "ssh team1_prod ls"},
              "output": "color.pas\n"}}}
_OC_TOOL_ERR = {"type": "tool_use", "sessionID": "ses_1", "part": {
    "type": "tool", "tool": "bash", "callID": "tc3",
    "state": {"status": "error", "input": {"command": "false"}, "output": "boom"}}}
_OC_STEP_START = {"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}}
_OC_STEP_FINISH = {"type": "step_finish", "sessionID": "ses_1", "part": {
    "type": "step-finish", "reason": "tool-calls",
    "tokens": {"input": 1200, "output": 80, "reasoning": 0, "cache": {"read": 1000, "write": 0}}}}


def test_parse_opencode_stream_unpacks_parts(tmp_path):
    p = tmp_path / "stream-async-0.jsonl"
    _write_stream(p, [
        _OC_STEP_START, _OC_TEXT, _OC_TOOL, _OC_TOOL_SHELL, _OC_TOOL_ERR, _OC_STEP_FINISH,
    ])
    events = parse_stream_json(p)
    kinds = [e.kind for e in events]
    # step_start→nothing; text→text; each tool_use→tool_use+tool_result; step_finish→system
    assert kinds == [
        "text",
        "tool_use", "tool_result",      # mcp tool ok
        "tool_use", "tool_result",      # shell ok
        "tool_use", "tool_result",      # shell err
        "system",                       # step_finish
    ]
    # the errored tool surfaces as an error result; the OK ones don't
    errs = [e for e in events if e.is_error]
    assert len(errs) == 1 and "boom" in errs[0].body
    # tool cards are named by the opencode tool
    assert any(e.tool_name == "cyberarena_get_game_state" for e in events)
    assert any(e.tool_name == "bash" for e in events)


def test_opencode_summary_stats(tmp_path):
    p = tmp_path / "stream-async-0.jsonl"
    _write_stream(p, [_OC_TEXT, _OC_TOOL, _OC_STEP_FINISH])
    stats = summary_stats(parse_stream_json(p))
    assert stats["tokens_in"] == 1200 and stats["tokens_out"] == 80
    assert stats["tokens_cached"] == 1000   # cache read + write
    assert "cyberarena_get_game_state" in stats["tools_breakdown"]


# ── kimi-cli stream-json (OpenAI-chat shape: role/content/tool_calls) ────

_KIMI_ASSISTANT_TOOLCALL = {
    "role": "assistant", "content": [],
    "tool_calls": [{"type": "function", "id": "call_x",
                    "function": {"name": "Shell",
                                 "arguments": "{\"command\": \"echo hello\"}"}}],
}
_KIMI_TOOL_RESULT = {
    "role": "tool", "tool_call_id": "call_x",
    "content": [{"type": "text", "text": "<system>Command executed successfully.</system>"},
                {"type": "text", "text": "hello\n"}],
}
_KIMI_ASSISTANT_TEXT = {"role": "assistant", "content": "done."}


def test_kimi_stream_json_unpacks_to_events():
    """kimi's role/content/tool_calls lines (no top-level `type`, no `message`
    wrapper) must yield tool_use + tool_result + text events — not the blank
    panel that an unrecognized schema produced."""
    evs = (_unpack_line(_KIMI_ASSISTANT_TOOLCALL)
           + _unpack_line(_KIMI_TOOL_RESULT)
           + _unpack_line(_KIMI_ASSISTANT_TEXT))
    kinds = [e.kind for e in evs]
    assert "tool_use" in kinds and "tool_result" in kinds and "text" in kinds
    tu = next(e for e in evs if e.kind == "tool_use")
    assert tu.tool_name == "Shell" and "echo hello" in tu.title
    tr = next(e for e in evs if e.kind == "tool_result")
    assert "hello" in tr.body


def test_kimi_line_not_confused_with_claude_or_codex():
    """The kimi detector keys off (role, no type, no message); Claude/codex/
    opencode lines must not route into the kimi unpacker."""
    # A Claude assistant line has top-level type + message wrapper.
    claude = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}],
                                               "usage": {"input_tokens": 1, "output_tokens": 1}}}
    evs = _unpack_line(claude)
    assert [e.kind for e in evs] == ["text"]  # parsed by the Claude branch, unchanged


# ── Grok CLI (`--output-format streaming-json`): token-level thought/text deltas
def test_parse_grok_stream_coalesces_token_deltas(tmp_path):
    """Real Grok shape (wordpress game e072ff837c4d, 2026-09-18): one token per
    line with its leading space already in `data`. A run of `thought` deltas
    becomes ONE thinking event and a run of `text` deltas ONE text event, joined
    with "" (never " ", which would double every space); the auto-compact pair
    is a system card; the first piece's timing is kept."""
    p = tmp_path / "stream-async-0.jsonl"
    lines = ([{"type": "thought", "data": d} for d in ["The", " user", " wants", " me", " to", ":\n"]]
             + [{"type": "text", "data": d} for d in ["I'll", " start", " by", " reading", "."]]
             + [{"type": "auto_compact_started", "percentage": 89},
                {"type": "auto_compact_completed"}]
             + [{"type": "thought", "data": d} for d in ["Now", " attack", "."]])
    _write_stream(p, lines)
    (tmp_path / "stream-async-0.jsonl.timing").write_text(
        "".join(f"{i}\t{1000 + i}\n" for i in range(1, len(lines) + 1)))

    events = parse_stream_json(p)
    assert [e.kind for e in events] == ["thinking", "text", "system", "system", "thinking"]
    assert events[0].body == "The user wants me to:\n"
    assert events[1].body == "I'll start by reading."
    assert "  " not in events[0].body and "  " not in events[1].body
    assert events[0].ts == 1001 and events[1].ts == 1007      # first delta of each run
    assert "auto-compact started" in events[2].title and "89%" in events[2].title
    assert events[4].body == "Now attack."
    assert summary_stats(events)["events"] == 5


def test_grok_line_is_not_mistaken_for_other_schemas():
    # a Gemini `message` line carries an ISO timestamp → still Gemini
    gem = _unpack_line({"type": "message", "timestamp": "2026-09-18T00:00:00Z",
                        "role": "assistant", "content": "hi"})
    assert [e.kind for e in gem] == ["text"]
    # a bare Grok delta is a delta, even when its text looks like a schema word
    assert [e.kind for e in _unpack_line({"type": "text", "data": "assistant"})] == ["grok_text"]
    assert [e.kind for e in _unpack_line({"type": "thought", "data": " x"})] == ["grok_thought"]
    # a `text` line WITHOUT `data` is not Grok (and no other schema) → dropped
    assert _unpack_line({"type": "text", "text": "x"}) == []
    # merge is a no-op on a stream with no deltas
    ev = _unpack_line(_ASSISTANT)
    assert merge_grok_deltas(ev) == ev


def test_grok_stream_folds_into_a_readable_transcript_via_collect(tmp_path):
    run = tmp_path / "run"
    team = run / "artifacts" / "team1"
    team.mkdir(parents=True)
    _write_stream(team / "stream-async-0.jsonl",
                  [{"type": "thought", "data": "Plan"}, {"type": "thought", "data": " it."},
                   {"type": "text", "data": "Done"}, {"type": "text", "data": "."}])
    trs = collect_transcripts(run)
    assert len(trs) == 1
    kinds = [e["kind"] for e in trs[0]["events"]]
    assert kinds == ["thinking", "text"]
    assert trs[0]["events"][0]["body"] == "Plan it."
