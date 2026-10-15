"""dsh trajectory emitter — translate dsh's session-event log into the arena's
per-tool-call trajectory stream, live, for headless runs.

Why this exists
---------------
`dsh --profile headless` (DeepSeek Harness) runs a task to quiescence and prints
ONLY the final assistant text on stdout at the very end (see
`@deepseek-ai/dsh-headless`'s runner: it `summarize()`s the session and writes
`outcome.text`). The arena tees that stdout to `stream-async-<n>.jsonl`, so in a
headless run the trajectory stream was a single end-of-phase blob — and when the
async supervisor SIGKILLs the long-lived phase at round/match end, that buffered
final write is truncated and the file lands 0 bytes (issue: dsh team1
stream-async-0.jsonl = 0 bytes). There is no per-tool-call structure at all.

The fix
-------
dsh's `session-persistence-jsonl` backend writes EVERY session event
(`{type,seq,time,data}`) to an append-only `session.jsonl` under
`$DSH_HOME/sessions/...`, incrementally, as the run proceeds (the arena overlay
forces `compression: none` so it is plaintext). That log is the real
per-tool-call trajectory, and it survives a SIGKILL because it is flushed as it
grows, not buffered to phase end.

This module tails that log during the phase and translates each event into the
arena's dsh trajectory schema (one JSON object per line, top-level
`"type":"dsh"`), printing to stdout — which the arena tees, live, into
`stream-async-<n>.jsonl`. Because lines are emitted as events occur, a
mid-phase SIGKILL keeps everything already streamed.

The emitted schema is the parser⇄emitter contract locked with the #954 parser
owner (Artifact #71, `gamemgr/transcripts.py::_unpack_dsh_line`):

  assistant : {"type":"dsh","ts":<epoch-sec float>,"role":"assistant",
               "thought"?:str, "text"?:str,
               "tool_calls"?:[{"id":str,"function":{"name":str,"arguments":str}}]}
  tool      : {"type":"dsh","ts":...,"role":"tool","tool_call_id":str,
               "name":str|None,"content":str,"is_error":bool}
  usage     : {"type":"dsh","ts":...,"role":"usage",
               "input_tokens":int,"output_tokens":int,"cached_input_tokens"?:int}

`ts` is dsh's event `time` (Unix epoch ms) divided by 1000. `tool_calls` use the
OpenAI-nested function shape (dsh's native `llm-deepseek` shape); `arguments` is
the raw JSON string exactly as the model produced it.

Stdlib-only, python3. Shipped into the agent container by
`DeepSeekAgentManager.install` and launched alongside `dsh` by `phase_command`;
also imported directly by the unit tests, which exercise `iter_arena_lines`.

Source event schema (dsh, read from @deepseek-ai/dsh-session + dsh-llm):
  event   = {"type": str, "seq": int, "time": int(ms), "data": {...}}
  content block types: text {type,text} · reasoning {type,text} ·
    tool-call {type,id,name,arguments} · tool-result {type,toolCallId,content[],isError?}
  assistant/message.data = {turn, step, message:{role, content:[block]}, usage?}
  tool/result.data       = {turn, step, message:{role,content:[tool-result]}, error?}
  tool/call.data         = {turn, step, callId, name, arguments}
  usage (TokenUsage)     = {inputTokens, outputTokens, cacheReadTokens?, ...}
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import sys
import time
from typing import Any, Iterable, Iterator

# The dsh session-event types this emitter turns into arena cards. Everything
# else in the log (turn/step lifecycle, user/message, assistant/chunk deltas and
# their packed chunk-rows, the `session` header line, request headers, todo
# writes, telemetry) carries no per-tool-call card and is skipped.
_ASSISTANT_MESSAGE = "assistant/message"
_TOOL_RESULT = "tool/result"
_TOOL_CALL = "tool/call"


def _blocks_text(blocks: Any) -> str:
    """Flatten a dsh content-block list to a plain string: concatenate every
    `text`/`reasoning` block's text (and bare strings). Non-text blocks (images)
    contribute nothing. Tolerates a block list that is actually a plain string."""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict):
            t = b.get("type")
            if t in ("text", "reasoning") and isinstance(b.get("text"), str):
                parts.append(b["text"])
    return "".join(parts)


def _epoch_seconds(event: dict) -> float:
    """dsh stamps each event `time` in Unix epoch MILLISECONDS; the arena schema
    carries `ts` in epoch SECONDS (float). Missing/garbage → 0.0."""
    t = event.get("time")
    if isinstance(t, (int, float)):
        return t / 1000.0
    return 0.0


def _assistant_lines(event: dict, ts: float, names: dict) -> Iterator[dict]:
    """Translate one `assistant/message` event into 0..2 arena lines: the
    assistant card (thought/text/tool_calls — whichever the step produced) and,
    when the adapter reported it, a `usage` card. Records callId→name so the
    matching `tool/result` can be named."""
    data = event.get("data") or {}
    message = data.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        content = []

    thoughts: list[str] = []
    texts: list[str] = []
    tool_calls: list[dict] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "reasoning" and isinstance(b.get("text"), str):
            thoughts.append(b["text"])
        elif btype == "text" and isinstance(b.get("text"), str):
            texts.append(b["text"])
        elif btype == "tool-call":
            call_id = b.get("id")
            name = b.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                names[call_id] = name
            tool_calls.append({
                "id": call_id,
                "function": {"name": name, "arguments": b.get("arguments", "")},
            })

    line: dict[str, Any] = {"type": "dsh", "ts": ts, "role": "assistant"}
    if thoughts:
        line["thought"] = "".join(thoughts)
    if texts:
        line["text"] = "".join(texts)
    if tool_calls:
        line["tool_calls"] = tool_calls
    # A bare assistant/message with no thought/text/tool_calls (e.g. an empty
    # provider turn) carries no card — skip it rather than emit an empty shell.
    if len(line) > 3:
        yield line

    usage = data.get("usage")
    if isinstance(usage, dict):
        u: dict[str, Any] = {"type": "dsh", "ts": ts, "role": "usage"}
        if isinstance(usage.get("inputTokens"), (int, float)):
            u["input_tokens"] = usage["inputTokens"]
        if isinstance(usage.get("outputTokens"), (int, float)):
            u["output_tokens"] = usage["outputTokens"]
        if isinstance(usage.get("cacheReadTokens"), (int, float)):
            u["cached_input_tokens"] = usage["cacheReadTokens"]
        if len(u) > 3:
            yield u


def _tool_result_line(event: dict, ts: float, names: dict) -> Iterator[dict]:
    """Translate one `tool/result` event into a single arena `tool` line. The
    tool name is not on the result event itself — it is carried by the matching
    `tool/call` / assistant tool_call block, keyed by callId."""
    data = event.get("data") or {}
    message = data.get("message") or {}
    content = message.get("content")
    block = None
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool-result":
                block = b
                break
    if block is None:
        return

    call_id = block.get("toolCallId")
    is_error = bool(block.get("isError")) or bool(data.get("error"))
    yield {
        "type": "dsh",
        "ts": ts,
        "role": "tool",
        "tool_call_id": call_id,
        "name": names.get(call_id) if isinstance(call_id, str) else None,
        "content": _blocks_text(block.get("content")),
        "is_error": is_error,
    }


def _translate(events: Iterable[dict], names: dict) -> Iterator[dict]:
    """Core translation, order-preserving, with the callId→name map threaded in
    so it can persist across live-tail batches. `tool/call` events only populate
    that map (the tool_calls card comes from the assistant message carrying the
    same blocks), so a tool call is never double-emitted; the `session` header
    line and any non-dict / unrelated event carry no card and are skipped."""
    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == _TOOL_CALL:
            data = event.get("data") or {}
            cid, nm = data.get("callId"), data.get("name")
            if isinstance(cid, str) and isinstance(nm, str):
                names[cid] = nm
            continue
        if etype not in (_ASSISTANT_MESSAGE, _TOOL_RESULT):
            continue
        ts = _epoch_seconds(event)
        if etype == _ASSISTANT_MESSAGE:
            yield from _assistant_lines(event, ts, names)
        else:
            yield from _tool_result_line(event, ts, names)


def iter_arena_lines(events: Iterable[dict]) -> Iterator[dict]:
    """Translate a stream of dsh session events into arena dsh-trajectory lines,
    starting from an empty callId→name map. The one-shot, whole-log entry point
    (the live tailer threads a persistent map through `_translate`)."""
    return _translate(events, {})


# ── live tail: read the growing session.jsonl and stream arena lines ─────────


def _newest_session_log(root: str, not_before: float = 0.0) -> str | None:
    """The most-recently-modified `session.jsonl` under `root`. dsh writes one
    per invocation under `<root>/<project>/<session-id>/session.jsonl`; a headless
    phase is a single fresh session, so the newest is this phase's. `not_before`
    prevents a later async chunk from attaching to an older session."""
    matches = glob.glob(os.path.join(root, "**", "session.jsonl"), recursive=True)
    if not matches:
        return None
    fresh = []
    for path in matches:
        try:
            if os.path.getmtime(path) >= not_before:
                fresh.append(path)
        except OSError:
            continue
    return max(fresh, key=os.path.getmtime) if fresh else None


class _Tailer:
    """Incremental line reader for one growing file: hands back only complete
    (newline-terminated) lines, buffering any partial trailing line for later."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._pos = 0
        self._buf = ""

    def read_lines(self) -> list[str]:
        try:
            with open(self._path, "r", errors="replace") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
                self._pos = fh.tell()
        except OSError:
            return []
        if not chunk:
            return []
        self._buf += chunk
        *complete, self._buf = self._buf.split("\n")
        return complete


def _emit(lines: list[str], names_seen: dict, out) -> None:
    """Parse raw jsonl lines, translate, and write arena lines flushed so the
    arena's stdout tee lands them live. Malformed lines are skipped."""
    events = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    for arena in _translate(events, names_seen):
        out.write(json.dumps(arena, ensure_ascii=False) + "\n")
        out.flush()


def main(argv: list[str] | None = None) -> int:
    """Tail the phase's session log and stream arena trajectory lines to stdout
    until dsh finishes (SIGTERM from the launcher, or the parent bash dies and we
    are reparented) — then do one final drain so the last events are not lost."""
    ap = argparse.ArgumentParser(description="dsh trajectory emitter (live tail)")
    ap.add_argument("--root", required=True, help="dsh sessions root ($DSH_HOME/sessions)")
    ap.add_argument("--poll-secs", type=float, default=0.25)
    ap.add_argument(
        "--not-before", type=float, default=0.0,
        help="ignore session logs older than this epoch timestamp",
    )
    ap.add_argument("--idle-exit-secs", type=float, default=120.0,
                    help="give up if no session log ever appears within this window")
    args = ap.parse_args(argv)

    stopping = {"flag": False}

    def _on_term(_signum, _frame):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    out = sys.stdout
    names: dict[str, str] = {}
    tailer: _Tailer | None = None
    started = time.monotonic()

    while True:
        if tailer is None:
            path = _newest_session_log(args.root, args.not_before)
            if path is not None:
                tailer = _Tailer(path)
        if tailer is not None:
            _emit(tailer.read_lines(), names, out)

        # Stop when the launcher signals dsh is done, or the parent process has
        # died (reparented to init → ppid 1). Drain once more before leaving so
        # the final assistant message / tool result is emitted.
        reparented = os.getppid() == 1
        if stopping["flag"] or reparented:
            if tailer is not None:
                _emit(tailer.read_lines(), names, out)
            return 0
        if tailer is None and (time.monotonic() - started) > args.idle_exit_secs:
            return 0
        time.sleep(args.poll_secs)


if __name__ == "__main__":
    raise SystemExit(main())
