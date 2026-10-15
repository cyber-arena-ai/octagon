"""Unit tests for agents/dsh_trajectory_emitter.py — the dsh session-event →
arena dsh-trajectory translator (Board #953 emitter; #954 parser contract in
Artifact #71 / gamemgr/transcripts.py::_unpack_dsh_line).

`iter_arena_lines` is a pure function over dsh session events (the
`{type,seq,time,data}` objects dsh's session-persistence-jsonl backend writes to
`session.jsonl`, one per line). These tests feed it a representative headless
run — the same log the committed sample is generated from — and pin the emitted
schema field-by-field, plus the edge cases (empty assistant turn, missing tool
name, error result, chunk/header lines skipped).
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

from agents.dsh_trajectory_emitter import _blocks_text, _newest_session_log, iter_arena_lines

# ── a representative dsh headless session log (source events) ─────────────────
# Header line, turn/step lifecycle, two tool round-trips (bash ok, read_file
# error), a final answer, with per-step usage on two of the assistant messages.
# Times are epoch MILLISECONDS (dsh's `time`); arena `ts` is /1000.

REPRESENTATIVE_SESSION_EVENTS: list[dict] = [
    {"type": "session", "version": 0, "id": "session-demo", "createdAt": 1786000000000, "delegationDepth": 0},
    {"type": "turn/start", "seq": 1, "time": 1786000000000, "data": {"turn": 1}},
    {"type": "step/start", "seq": 2, "time": 1786000000100, "data": {"turn": 1, "step": 1}},
    {"type": "assistant/message", "seq": 3, "time": 1786000001000, "data": {
        "turn": 1, "step": 1,
        "message": {"id": "m1", "role": "assistant", "content": [
            {"type": "reasoning", "text": "The app is under /srv — list it to find the entrypoint."},
            {"type": "text", "text": "Let me look at the server files."},
            {"type": "tool-call", "id": "call_1", "name": "bash", "arguments": "{\"command\":\"ls /srv\"}"},
        ]},
        "usage": {"inputTokens": 1200, "outputTokens": 340, "cacheReadTokens": 900},
    }},
    {"type": "tool/result", "seq": 4, "time": 1786000002000, "data": {
        "turn": 1, "step": 1,
        "message": {"id": "r1", "role": "user", "content": [
            {"type": "tool-result", "toolCallId": "call_1",
             "content": [{"type": "text", "text": "app.py\nflag.txt\n"}], "isError": False},
        ]},
    }},
    {"type": "step/end", "seq": 5, "time": 1786000002100, "data": {"turn": 1, "step": 1}},
    {"type": "step/start", "seq": 6, "time": 1786000002200, "data": {"turn": 1, "step": 2}},
    {"type": "assistant/message", "seq": 7, "time": 1786000003000, "data": {
        "turn": 1, "step": 2,
        "message": {"id": "m2", "role": "assistant", "content": [
            {"type": "tool-call", "id": "call_2", "name": "read_file", "arguments": "{\"path\":\"/srv/flag.txt\"}"},
        ]},
    }},
    {"type": "tool/result", "seq": 8, "time": 1786000004000, "data": {
        "turn": 1, "step": 2,
        "message": {"id": "r2", "role": "user", "content": [
            {"type": "tool-result", "toolCallId": "call_2",
             "content": [{"type": "text", "text": "Permission denied"}], "isError": True},
        ]},
        "error": {"name": "ToolError", "code": "EACCES"},
    }},
    {"type": "assistant/message", "seq": 9, "time": 1786000005000, "data": {
        "turn": 1, "step": 3,
        "message": {"id": "m3", "role": "assistant", "content": [
            {"type": "text", "text": "I could not read the flag; access to /srv/flag.txt is denied."},
        ]},
        "usage": {"inputTokens": 1500, "outputTokens": 60},
    }},
    {"type": "turn/end", "seq": 10, "time": 1786000005100, "data": {"turn": 1, "reason": {"kind": "completed"}}},
]


def _lines():
    return list(iter_arena_lines(REPRESENTATIVE_SESSION_EVENTS))


def test_representative_run_yields_expected_cards_in_order():
    lines = _lines()
    # assistant, usage, tool, assistant, tool, assistant, usage
    assert [(l["role"]) for l in lines] == [
        "assistant", "usage", "tool", "assistant", "tool", "assistant", "usage",
    ]
    # every line is discriminated and non-empty
    assert all(l["type"] == "dsh" for l in lines)
    assert all("ts" in l for l in lines)


def test_assistant_card_carries_thought_text_and_openai_nested_tool_calls():
    a = _lines()[0]
    assert a["ts"] == 1786000001.0                       # epoch ms /1000
    assert a["thought"].startswith("The app is under /srv")
    assert a["text"] == "Let me look at the server files."
    assert a["tool_calls"] == [{
        "id": "call_1",
        "function": {"name": "bash", "arguments": "{\"command\":\"ls /srv\"}"},
    }]
    # arguments stays a raw JSON string, exactly as the model produced it
    assert isinstance(a["tool_calls"][0]["function"]["arguments"], str)


def test_usage_card_maps_camelcase_to_snake_case_tokens():
    u = _lines()[1]
    assert u == {"type": "dsh", "ts": 1786000001.0, "role": "usage",
                 "input_tokens": 1200, "output_tokens": 340, "cached_input_tokens": 900}
    # a usage without cache read omits cached_input_tokens rather than zero-fill
    u2 = _lines()[6]
    assert "cached_input_tokens" not in u2
    assert u2["input_tokens"] == 1500 and u2["output_tokens"] == 60


def test_tool_card_named_from_call_id_with_content_and_error_flag():
    ok = _lines()[2]
    assert ok["role"] == "tool" and ok["tool_call_id"] == "call_1"
    assert ok["name"] == "bash"                           # resolved via callId→name
    assert ok["content"] == "app.py\nflag.txt\n"
    assert ok["is_error"] is False

    err = _lines()[4]
    assert err["tool_call_id"] == "call_2" and err["name"] == "read_file"
    assert err["content"] == "Permission denied"
    assert err["is_error"] is True                        # isError block flag + data.error


def test_tool_calls_are_not_double_emitted_from_separate_tool_call_events():
    """A `tool/call` event carries the same call the assistant message already
    holds — it must only feed the name map, never emit a second tool_calls card."""
    events = [
        {"type": "tool/call", "seq": 1, "time": 1000,
         "data": {"turn": 1, "step": 1, "callId": "c9", "name": "grep", "arguments": "{}"}},
        {"type": "assistant/message", "seq": 2, "time": 2000, "data": {
            "turn": 1, "step": 1, "message": {"role": "assistant", "content": [
                {"type": "tool-call", "id": "c9", "name": "grep", "arguments": "{}"},
            ]}}},
        {"type": "tool/result", "seq": 3, "time": 3000, "data": {
            "turn": 1, "step": 1, "message": {"role": "user", "content": [
                {"type": "tool-result", "toolCallId": "c9",
                 "content": [{"type": "text", "text": "match"}]}]}}},
    ]
    out = list(iter_arena_lines(events))
    assert [l["role"] for l in out] == ["assistant", "tool"]
    assert out[1]["name"] == "grep"                        # named even from tool/call map


def test_empty_assistant_turn_and_unknown_events_produce_no_card():
    events = [
        {"type": "session", "id": "s"},                                  # header
        {"type": "turn/start", "seq": 1, "time": 1, "data": {"turn": 1}},
        {"type": "assistant/chunk", "seq": 2, "time": 2, "data": {}},    # token delta
        {"type": "assistant/message", "seq": 3, "time": 3, "data": {     # empty turn
            "turn": 1, "step": 1, "message": {"role": "assistant", "content": []}}},
        {"type": "todo/write", "seq": 4, "time": 4, "data": {"todos": []}},
        "not-a-dict",
    ]
    assert list(iter_arena_lines(events)) == []


def test_blocks_text_flattens_text_and_reasoning_and_tolerates_string():
    assert _blocks_text("plain") == "plain"
    assert _blocks_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
    assert _blocks_text([{"type": "image", "attachment": {}}]) == ""
    assert _blocks_text(None) == ""


def test_every_emitted_line_is_json_serializable():
    for line in _lines():
        assert json.loads(json.dumps(line)) == line


def test_live_tailer_flushes_received_event_on_sigterm(tmp_path):
    """The real container-side emitter flushes complete session events before
    honoring the launcher SIGTERM used for normal/timeout cleanup."""
    root = tmp_path / "sessions"
    session = root / "project" / "session-1" / "session.jsonl"
    session.parent.mkdir(parents=True)
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parents[1] / "agents" / "dsh_trajectory_emitter.py"),
         "--root", str(root), "--poll-secs", "0.01", "--idle-exit-secs", "5"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        event = REPRESENTATIVE_SESSION_EVENTS[3]
        session.write_text(json.dumps(event) + "\n")
        ready, _, _ = select.select([proc.stdout], [], [], 3.0)
        assert ready, "emitter did not flush a received trajectory line"
        line = proc.stdout.readline()
        assert json.loads(line)["role"] == "assistant"

        final_event = REPRESENTATIVE_SESSION_EVENTS[9]
        with session.open("a") as fh:
            fh.write(json.dumps(final_event) + "\n")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=3) == 0
        remaining = proc.stdout.read().splitlines()
        assert any(json.loads(row).get("text", "").startswith("I could not read") for row in remaining)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_newest_session_log_ignores_previous_async_chunks(tmp_path):
    root = tmp_path / "sessions"
    old = root / "project" / "old" / "session.jsonl"
    new = root / "project" / "new" / "session.jsonl"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_text("old\n")
    os.utime(old, (1, 1))
    new.write_text("new\n")

    assert _newest_session_log(str(root), not_before=2.0) == str(new)
