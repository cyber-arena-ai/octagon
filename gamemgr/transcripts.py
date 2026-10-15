"""Parse agent stream-json transcripts from a run's artifact dir.

Each per-phase agent invocation writes `stream-{phase}.jsonl` (the raw
CLI stream-json) under `runs/<ts>/artifacts/<team>/round-<N>/`. This
module turns those files into structured cards the panel renders below
the timeline:

  ⚙ system · 🧠 thinking · 💬 text · 🔧 tool_use · ↩ tool_result

Adapted from the offline `vis/logging.py` transcript renderer, minus the
shared-timeline offset interpolation — these cards render in file order
as a per-(team, round, phase) stack, so wall-clock alignment isn't
needed. Stdlib-only; consumed by `gamemgr/api.py::/game/agents`.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class AgentEvent:
    """One flattened event from a stream-json line."""

    kind: str  # system | thinking | text | tool_use | tool_result
    title: str  # one-line summary (the card header)
    body: str  # full content (collapsible in the panel)
    tool_name: str | None  # set on tool_use
    is_error: bool  # tool_result with is_error
    tokens_in: int | None  # usage.input_tokens (new, uncached input)
    tokens_out: int | None  # usage.output_tokens (generated)
    tokens_cached: int | None = None  # cache_read + cache_creation input
    # Arrival wall-clock (epoch secs) of the stream LINE this event came from,
    # joined from the parallel `.timing` sidecar in parse_stream_json. Carried
    # on /game/agents so the LIVE panel has per-event time (the finished path
    # reads the same sidecar midend-side). None when the sidecar lacks the line
    # (absent file, or a just-arrived line mid-stream).
    ts: float | None = None


def _truncate(s: str, n: int = 140) -> str:
    s = s.strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _flatten_msg_content(item: dict) -> list[dict]:
    """A line's `message.content` can carry several typed blocks (a
    thinking block then a tool_use, etc.); walk them out."""
    msg = item.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            return [c for c in content if isinstance(c, dict)]
    return []


# Codex `exec --json` emits a different schema than Claude's stream-json:
# thread.started / turn.started / turn.completed / item.{started,completed}
# (item.type ∈ agent_message | mcp_tool_call | command_execution | reasoning
# | …). Detected by the dotted top-level `type`; routed to _unpack_codex_line.
def _is_codex_line(top) -> bool:
    return isinstance(top, str) and (
        top.startswith("thread.") or top.startswith("turn.") or top.startswith("item.")
    )


def _unpack_codex_item(item: dict) -> list[AgentEvent]:
    """Translate one completed Codex `item` into 0..N flat AgentEvents.

    agent_message → text; reasoning → thinking; mcp_tool_call and
    command_execution → a tool_use card plus a tool_result card for the
    output. Unknown item types fall back to a system card so nothing in
    the transcript is silently dropped."""
    out: list[AgentEvent] = []
    itype = item.get("type")

    if itype == "agent_message":
        text = item.get("text", "")
        out.append(AgentEvent("text", _truncate(text), text, None, False, None, None))
    elif itype in ("reasoning", "agent_reasoning"):
        text = item.get("text") or item.get("summary") or ""
        out.append(AgentEvent("thinking", _truncate(str(text)), str(text), None, False, None, None))
    elif itype == "mcp_tool_call":
        name = f"{item.get('server', '?')}.{item.get('tool', '?')}"
        args = item.get("arguments")
        body = json.dumps(args, indent=2) if args is not None else ""
        title = f"{name}: {_truncate(body, 120)}" if args else name
        out.append(AgentEvent("tool_use", title, body, name, False, None, None))
        err, res = item.get("error"), item.get("result")
        is_err = bool(err) or item.get("status") == "failed"
        if err is not None or res is not None:
            rbody = json.dumps(err if err is not None else res, indent=2)
            out.append(AgentEvent(
                "tool_result", ("ERR " if is_err else "") + _truncate(rbody, 120),
                rbody, None, is_err, None, None,
            ))
    elif itype == "command_execution":
        cmd = item.get("command", "")
        out.append(AgentEvent("tool_use", f"shell: {_truncate(str(cmd), 120)}", str(cmd), "shell", False, None, None))
        otext = item.get("aggregated_output") or item.get("output") or ""
        exit_code = item.get("exit_code")
        is_err = isinstance(exit_code, int) and exit_code != 0
        if otext or exit_code is not None:
            out.append(AgentEvent(
                "tool_result", ("ERR " if is_err else "") + _truncate(str(otext), 120),
                str(otext), None, is_err, None, None,
            ))
    elif itype == "file_change":
        changes = item.get("changes") or []
        paths = ", ".join(
            f"{c.get('kind', 'edit')} {c.get('path', '?')}"
            for c in changes if isinstance(c, dict)
        )
        out.append(AgentEvent(
            "tool_use", f"file_change: {_truncate(paths, 120)}",
            json.dumps(changes, indent=2), "file_change", False, None, None,
        ))
    else:
        text = item.get("text") or json.dumps(item)
        out.append(AgentEvent(
            "system", f"{itype}: {_truncate(str(text), 100)}",
            json.dumps(item, indent=2), None, False, None, None,
        ))
    return out


def _unpack_codex_line(obj: dict) -> list[AgentEvent]:
    """Translate one Codex `exec --json` line into 0..N flat AgentEvents."""
    out: list[AgentEvent] = []
    top = obj.get("type")

    if top == "thread.started":
        tid = str(obj.get("thread_id", "?"))[:8]
        out.append(AgentEvent(
            "system", f"session start · thread={tid}",
            json.dumps(obj, indent=2), None, False, None, None,
        ))
    elif top == "turn.completed":
        usage = obj.get("usage") or {}
        ev = AgentEvent(
            "system", "turn complete", json.dumps(usage, indent=2),
            None, False, usage.get("input_tokens"), usage.get("output_tokens"),
        )
        ev.tokens_cached = usage.get("cached_input_tokens")
        out.append(ev)
    elif top == "turn.failed":
        err = obj.get("error") or {}
        out.append(AgentEvent(
            "tool_result", "ERR turn failed: " + _truncate(str(err), 110),
            json.dumps(obj, indent=2), None, True, None, None,
        ))
    elif top == "item.completed":
        # Render only the terminal item state — item.started/.updated would
        # double-count (same id) and carry no final result yet.
        out.extend(_unpack_codex_item(obj.get("item") or {}))
    # thread.started handled above; turn.started / item.started / item.updated
    # carry no panel-worthy delta → no event.
    return out


# opencode `run --format json` emits one event per line, each wrapping a `part`
# object with a top-level `sessionID`. type ∈ text | tool_use | step_start |
# step_finish | reasoning; part.type ∈ text | tool | step-start | step-finish.
# Detected by the wrapped `part` dict (Claude/codex lines have none).
def _is_opencode_line(obj: dict) -> bool:
    return isinstance(obj.get("part"), dict) and "sessionID" in obj


def _unpack_opencode_line(obj: dict) -> list[AgentEvent]:
    """Translate one opencode `run --format json` line into 0..N AgentEvents.

    text → text; reasoning → thinking; tool_use → a tool_use card + (from the
    same terminal event's `state.output`) a tool_result card; step_finish →
    a system card carrying that step's token usage."""
    out: list[AgentEvent] = []
    typ = obj.get("type")
    part = obj.get("part") or {}

    if typ == "text":
        text = part.get("text", "")
        if text.strip():
            out.append(AgentEvent("text", _truncate(text), text, None, False, None, None))
    elif typ in ("reasoning", "thinking"):
        text = part.get("text", "")
        if text.strip():
            out.append(AgentEvent("thinking", _truncate(text), text, None, False, None, None))
    elif typ == "tool_use":
        name = part.get("tool", "?")
        state = part.get("state") or {}
        inp = state.get("input")
        body = json.dumps(inp, indent=2) if inp else ""
        title = f"{name}: {_truncate(body, 120)}" if body else name
        out.append(AgentEvent("tool_use", title, body, name, False, None, None))
        output, is_err = state.get("output"), state.get("status") == "error"
        if output is not None or is_err:
            rbody = output if isinstance(output, str) else json.dumps(output, indent=2)
            out.append(AgentEvent(
                "tool_result", ("ERR " if is_err else "") + _truncate(str(rbody), 120),
                str(rbody), None, is_err, None, None,
            ))
    elif typ == "step_finish":
        tokens = part.get("tokens") or {}
        cache = tokens.get("cache") or {}
        ev = AgentEvent("system", "step complete", json.dumps(tokens, indent=2),
                        None, False, tokens.get("input"), tokens.get("output"))
        ev.tokens_cached = (cache.get("read") or 0) + (cache.get("write") or 0)
        out.append(ev)
    # step_start carries no panel-worthy delta → no event.
    return out


# kimi-cli `--print --output-format stream-json` emits an OpenAI-chat-style message
# stream: one JSON object per line with a top-level `role` (assistant | tool | user),
# a flat `content` (a string, or a list of {"type":"text","text":..} blocks), and —
# for assistant turns that call tools — `tool_calls` (OpenAI shape: each has
# function.{name, arguments(a JSON string)}); a tool turn carries `tool_call_id`.
# Unlike Claude stream-json it has NO top-level `type` and NO `message` wrapper, and
# unlike codex/opencode it has neither a dotted `type` nor a `part`. Detected by:
# a top-level `role` with no `type` and no `message`. (kimi --print emits no usage in
# the stream, so per-turn tokens come from the api-router metering, not from here.)
def _is_kimi_line(obj: dict) -> bool:
    return (
        obj.get("type") is None
        and "message" not in obj
        and obj.get("role") in ("assistant", "tool", "user")
    )


def _kimi_text(content) -> str:
    """kimi `content` is a string or a list of {"type":"text","text":..} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            blk.get("text", "") if isinstance(blk, dict) else str(blk)
            for blk in content
        )
    return ""


def _unpack_kimi_line(obj: dict) -> list[AgentEvent]:
    """Translate one kimi stream-json line into flat AgentEvents (text / tool_use /
    tool_result), mirroring the Claude/codex/opencode unpackers."""
    out: list[AgentEvent] = []
    role = obj.get("role")
    text = _kimi_text(obj.get("content"))

    if role == "assistant":
        if text.strip():
            out.append(AgentEvent("text", _truncate(text), text, None, False, None, None))
        for tc in obj.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = fn.get("name", "?")
            raw_args = fn.get("arguments", "")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                args = raw_args
            summary = ""
            if isinstance(args, dict):
                summary = (args.get("command") or args.get("description")
                           or args.get("file_path") or args.get("path")
                           or args.get("pattern") or "")
            title = f"{name}: {_truncate(str(summary), 120)}" if summary else name
            body = json.dumps(args, indent=2) if isinstance(args, (dict, list)) else str(raw_args)
            out.append(AgentEvent("tool_use", title, body, name, False, None, None))
    elif role == "tool":
        low = text[:80].lower()
        is_err = ("error" in low or "failed" in low or "non-zero" in low) and "success" not in low
        out.append(AgentEvent(
            "tool_result", ("ERR " if is_err else "") + _truncate(text, 120),
            text, None, is_err, None, None,
        ))
    elif role == "user" and text.strip():
        out.append(AgentEvent("text", _truncate(text), text, None, False, None, None))
    return out


# dsh headless trajectory rows are explicitly tagged so they cannot collide
# with Claude, Codex, OpenCode, or Kimi stream schemas.
def _is_dsh_line(obj: dict) -> bool:
    return obj.get("type") == "dsh"


def _dsh_text(content) -> str:
    """Coerce dsh text/content blocks into the parser's string body."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            blk.get("text", "") if isinstance(blk, dict) else str(blk)
            for blk in content
        )
    return "" if content is None else str(content)


def _unpack_dsh_line(obj: dict) -> list[AgentEvent]:
    """Translate one dsh trajectory line into flat panel events."""
    out: list[AgentEvent] = []
    role = obj.get("role", "assistant")

    if role == "usage":
        ev = AgentEvent(
            "system", "usage", json.dumps(obj, indent=2), None, False,
            obj.get("input_tokens"), obj.get("output_tokens"),
        )
        ev.tokens_cached = obj.get("cached_input_tokens") or obj.get("cache_read_input_tokens")
        out.append(ev)
        return out

    if role == "tool":
        text = _dsh_text(obj.get("content") if obj.get("content") is not None else obj.get("output"))
        if "is_error" in obj:
            is_err = bool(obj.get("is_error"))
        else:
            low = text[:80].lower()
            is_err = ("error" in low or "failed" in low or "non-zero" in low) and "success" not in low
        out.append(AgentEvent(
            "tool_result", ("ERR " if is_err else "") + _truncate(text, 120),
            text, None, is_err, None, None,
        ))
        return out

    thought = _dsh_text(obj.get("thought") or obj.get("reasoning"))
    if thought.strip():
        out.append(AgentEvent("thinking", _truncate(thought), thought, None, False, None, None))
    text = _dsh_text(obj.get("text") if obj.get("text") is not None else obj.get("content"))
    if text.strip():
        out.append(AgentEvent("text", _truncate(text), text, None, False, None, None))
    for tc in obj.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
        name = fn.get("name", "?")
        raw_args = fn.get("arguments", "")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            args = raw_args
        summary = ""
        if isinstance(args, dict):
            summary = (args.get("command") or args.get("description")
                       or args.get("file_path") or args.get("path")
                       or args.get("pattern") or "")
        title = f"{name}: {_truncate(str(summary), 120)}" if summary else name
        body = json.dumps(args, indent=2) if isinstance(args, (dict, list)) else str(raw_args)
        out.append(AgentEvent("tool_use", title, body, name, False, None, None))
    return out


# The official Gemini CLI (`gemini -p --output-format stream-json`) emits its OWN
# line schema: one JSON object per line with a top-level `type` in
# {init, message, tool_use, tool_result, result} and an ISO `timestamp` — none of
# the Claude / codex / opencode / kimi / dsh shapes. `message` carries a top-level
# `role` + string `content`; `tool_use` a top-level `tool_name` + `parameters`.
_GEMINI_TYPES = frozenset({"init", "message", "tool_use", "tool_result", "result"})


def _is_gemini_line(obj: dict) -> bool:
    # The ISO `timestamp` disambiguates from any other schema (dsh uses a numeric
    # `ts`), so the generic `message`/`result` type names can't collide.
    return obj.get("type") in _GEMINI_TYPES and isinstance(obj.get("timestamp"), str)


def _unpack_gemini_line(obj: dict) -> list[AgentEvent]:
    """Translate one Gemini CLI stream-json line into flat AgentEvents.

    init → system; message/assistant → text (a `user` message is the injected
    prompt, skipped); tool_use → tool_use; tool_result → tool_result. `result` is
    the final usage summary, not a transcript card."""
    t = obj.get("type")
    if t == "init":
        model = obj.get("model", "?")
        return [AgentEvent(
            "system", f"session init · model={model}",
            json.dumps(obj, indent=2), None, False, None, None,
        )]
    if t == "message":
        if obj.get("role") == "assistant":
            text = str(obj.get("content") or "")
            if text.strip():
                return [AgentEvent("text", _truncate(text), text, None, False, None, None)]
        return []
    if t == "tool_use":
        name = obj.get("tool_name", "?")
        params = obj.get("parameters")
        summary = ""
        if isinstance(params, dict):
            summary = (params.get("command") or params.get("file_path")
                       or params.get("path") or params.get("pattern")
                       or params.get("strategic_intent") or params.get("title") or "")
        title = f"{name}: {_truncate(str(summary), 120)}" if summary else name
        body = json.dumps(params, indent=2) if isinstance(params, (dict, list)) else str(params)
        return [AgentEvent("tool_use", title, body, name, False, None, None)]
    if t == "tool_result":
        out = str(obj.get("output") or "")
        is_err = obj.get("status") not in (None, "success")
        return [AgentEvent(
            "tool_result", ("ERR " if is_err else "") + _truncate(out, 120),
            out, None, is_err, None, None,
        )]
    return []  # result: final usage summary, not a card


# ── Grok CLI (`grok -p … --output-format streaming-json`) ─────────────────
# xAI's own NDJSON: one TOKEN-LEVEL delta per line, `{"type":"thought","data":"The"}`
# then `{"type":"thought","data":" user"}` …, `{"type":"text","data":"I'll"}` …, plus
# `auto_compact_started` ({"percentage": 89}) / `auto_compact_completed`. The
# leading space is already inside `data`, so a run of deltas reconstructs with
# "".join — never " ".join. Nothing structured for tool calls exists in this
# format; a Grok transcript is reasoning + assistant text only.
_GROK_DELTA_TYPES = {"thought", "text"}
_GROK_COMPACT_TYPES = {"auto_compact_started", "auto_compact_completed"}


def _is_grok_line(obj: dict) -> bool:
    t = obj.get("type")
    if t in _GROK_DELTA_TYPES:
        # Gemini's `message` lines carry an ISO `timestamp`; a Grok delta is just
        # {type, data}.
        return "data" in obj and "timestamp" not in obj
    return t in _GROK_COMPACT_TYPES


def _unpack_grok_line(obj: dict) -> list[AgentEvent]:
    """One Grok delta → one lightweight `grok_thought` / `grok_text` event
    carrying the raw piece in `body`; `merge_grok_deltas` (applied to the whole
    file) coalesces each run into a single thinking / text event. The
    auto-compact pair becomes a system card."""
    t = obj.get("type")
    if t in _GROK_DELTA_TYPES:
        piece = obj.get("data")
        if not isinstance(piece, str):
            return []
        return [AgentEvent(f"grok_{t}", "", piece, None, False, None, None)]
    if t == "auto_compact_started":
        pct = obj.get("percentage")
        title = "context auto-compact started" + (f" ({pct}% of window)" if pct is not None else "")
        return [AgentEvent("system", title, json.dumps(obj), None, False, None, None)]
    return [AgentEvent("system", "context auto-compact completed", json.dumps(obj),
                       None, False, None, None)]


def merge_grok_deltas(events: list[AgentEvent]) -> list[AgentEvent]:
    """Collapse every maximal run of `grok_thought` into one `thinking` event and
    every run of `grok_text` into one `text` event, body = "".join(pieces),
    keeping the FIRST piece's `ts`. Events of any other kind pass through in
    place, so this is a no-op on a stream with no Grok deltas and safe to apply
    to every parsed file."""
    out: list[AgentEvent] = []
    run_kind: str | None = None
    pieces: list[str] = []
    run_ts: float | None = None

    def flush():
        nonlocal run_kind, pieces, run_ts
        if run_kind is not None:
            body = "".join(pieces)
            if body.strip():
                kind = "thinking" if run_kind == "grok_thought" else "text"
                out.append(AgentEvent(kind, _truncate(body), body, None, False,
                                      None, None, ts=run_ts))
        run_kind, pieces, run_ts = None, [], None

    for ev in events:
        if ev.kind in ("grok_thought", "grok_text"):
            if ev.kind != run_kind:
                flush()
                run_kind, run_ts = ev.kind, ev.ts
            pieces.append(ev.body)
        else:
            flush()
            out.append(ev)
    flush()
    return out


def _unpack_line(obj: dict) -> list[AgentEvent]:
    """Translate one stream-json line into 0..N flat AgentEvents.

    Dispatches by schema: Codex `exec --json` (dotted top-level `type`),
    opencode `run --format json` (wrapped `part`), dsh (`type == "dsh"`), kimi
    (`role`, no `type`), Gemini CLI (`type` in init/message/tool_* + ISO ts),
    Grok CLI (`type` thought/text + `data`, no timestamp — token deltas; callers
    coalesce them with `merge_grok_deltas`), or Claude stream-json (system /
    assistant / user)."""
    out: list[AgentEvent] = []
    top = obj.get("type")

    if _is_dsh_line(obj):
        return _unpack_dsh_line(obj)

    if _is_codex_line(top):
        return _unpack_codex_line(obj)

    if _is_opencode_line(obj):
        return _unpack_opencode_line(obj)

    if _is_kimi_line(obj):
        return _unpack_kimi_line(obj)

    if _is_gemini_line(obj):
        return _unpack_gemini_line(obj)

    if _is_grok_line(obj):
        return _unpack_grok_line(obj)

    if top == "system" and obj.get("subtype") == "init":
        model = obj.get("model", "?")
        tools = obj.get("tools") or []
        out.append(AgentEvent(
            kind="system",
            title=f"session init · model={model} · {len(tools)} tools",
            body=json.dumps(obj, indent=2),
            tool_name=None, is_error=False, tokens_in=None, tokens_out=None,
        ))
        return out

    if top == "assistant":
        for c in _flatten_msg_content(obj):
            ctype = c.get("type")
            if ctype == "thinking":
                text = c.get("thinking", "")
                out.append(AgentEvent(
                    "thinking", _truncate(text), text, None, False, None, None,
                ))
            elif ctype == "text":
                text = c.get("text", "")
                out.append(AgentEvent(
                    "text", _truncate(text), text, None, False, None, None,
                ))
            elif ctype == "tool_use":
                name = c.get("name", "?")
                ti = c.get("input") or {}
                summary = (
                    ti.get("description") or ti.get("command")
                    or ti.get("file_path") or ti.get("pattern") or ""
                )
                title = f"{name}: {_truncate(str(summary), 120)}" if summary else name
                out.append(AgentEvent(
                    "tool_use", title, json.dumps(ti, indent=2), name, False, None, None,
                ))
        # `usage` is per-turn, not per-block — attach it to just the first
        # event so summing tokens across events doesn't multiply-count one
        # turn's usage by its block count.
        if out:
            usage = (obj.get("message") or {}).get("usage") or {}
            out[0].tokens_in = usage.get("input_tokens")
            out[0].tokens_out = usage.get("output_tokens")
            # Prompt-cache input is reported separately from input_tokens;
            # most of an agent's context is cache reads (Claude --continue),
            # so surface it as its own figure rather than hiding it.
            out[0].tokens_cached = (
                (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
            )
        return out

    if top == "user":
        for c in _flatten_msg_content(obj):
            if c.get("type") != "tool_result":
                continue
            content = c.get("content", "")
            # tool_result.content is a list-of-blocks or a plain string
            # depending on the tool; coerce to a string body.
            if isinstance(content, list):
                content = "\n".join(
                    blk.get("text", "") if isinstance(blk, dict) else str(blk)
                    for blk in content
                )
            content_str = str(content)
            is_err = bool(c.get("is_error"))
            out.append(AgentEvent(
                "tool_result",
                ("ERR " if is_err else "") + _truncate(content_str, 120),
                content_str, None, is_err, None, None,
            ))
        return out

    return out


def _load_line_timing(stream_path: Path) -> dict[int, float]:
    """Read the parallel `.timing` sidecar (`<line_no>\t<epoch_ts>` per line,
    written live by agents/transport_docker as each stream line drains) into a
    `{line_no: ts}` map. Absent/partial file → missing entries; a line with no
    entry simply carries no ts (e.g. a just-arrived line mid-stream)."""
    tp = stream_path.with_suffix(stream_path.suffix + ".timing")
    out: dict[int, float] = {}
    try:
        raw = tp.read_text(errors="replace")
    except OSError:
        return out
    for ln in raw.splitlines():
        parts = ln.split("\t")
        if len(parts) == 2:
            try:
                out[int(parts[0])] = float(parts[1])
            except ValueError:
                continue
    return out


def parse_stream_json(path: Path) -> list[AgentEvent]:
    """Parse one `stream-*.jsonl` file into flat AgentEvents, in file
    order. Malformed lines are skipped (the CLI occasionally writes a
    partial line on shutdown).

    Each event is stamped with `ts` — the arrival wall-clock (epoch secs) of its
    stream line, joined by physical line number from the parallel `.timing`
    sidecar — so the LIVE /game/agents feed carries per-event time (None when the
    sidecar lacks that line)."""
    events: list[AgentEvent] = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return events
    ts_by_line = _load_line_timing(path)
    # Enumerate ALL physical lines (blank/malformed included) so line_no stays
    # aligned with the sidecar, which is keyed by the raw drain's newline count.
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # A valid JSON line need not be an object — some CLIs (e.g. the OpenHands
        # SDK driver) emit a bare JSON string/number per line. _unpack_line assumes
        # a dict (`obj.get(...)`), so skip non-dicts rather than crash the whole
        # snapshot. Keeps "unknown stream schema degrades to empty" actually true.
        if not isinstance(obj, dict):
            continue
        line_ts = ts_by_line.get(line_no)
        for ev in _unpack_line(obj):
            ev.ts = line_ts
            events.append(ev)
    # Grok's per-token deltas become whole thinking/text events here; no-op
    # for every other harness.
    return merge_grok_deltas(events)


def summary_stats(events: list[AgentEvent]) -> dict:
    """Per-transcript headline shown in the panel's `<summary>`."""
    tool_counts: dict[str, int] = {}
    tin = tout = tcached = 0
    n_err = 0
    for ev in events:
        if ev.kind == "tool_use" and ev.tool_name:
            tool_counts[ev.tool_name] = tool_counts.get(ev.tool_name, 0) + 1
        if ev.is_error:
            n_err += 1
        if ev.tokens_in:
            tin += ev.tokens_in
        if ev.tokens_out:
            tout += ev.tokens_out
        if ev.tokens_cached:
            tcached += ev.tokens_cached
    return {
        "events": len(events),
        "tools": sum(tool_counts.values()),
        "tools_breakdown": ", ".join(
            f"{n}×{name}" for name, n in sorted(tool_counts.items(), key=lambda kv: -kv[1])
        ) or "(none)",
        "errors": n_err,
        "tokens_in": tin,
        "tokens_cached": tcached,
        "tokens_out": tout,
    }


_STREAM_RE = re.compile(r"^stream-(?P<phase>.+)\.jsonl$")
_ROUND_RE = re.compile(r"^round-(?P<round>\d+)$")
# Phases run defense → attack within a round; a plain string sort would
# put "attack" before "defense". Rank so transcripts list chronologically.
_PHASE_RANK = {"defense": 0, "attack": 1}
# Async mode writes `stream-async-<n>.jsonl` chunks (no defense/attack split).
_ASYNC_RE = re.compile(r"^async-(?P<n>\d+)$")


def _phase_sort_key(phase: str) -> tuple[int, int]:
    """Order transcripts within a (team, round): sync phases defense→attack,
    then async chunks by their numeric index. Unknown phases sort last."""
    if phase in _PHASE_RANK:
        return (_PHASE_RANK[phase], 0)
    m = _ASYNC_RE.match(phase)
    if m:
        return (50, int(m.group("n")))
    return (99, 0)

# Parsed-result cache keyed by run dir → ((path, mtime_ns, size) signature,
# transcripts). The panel polls collect_transcripts every few seconds and
# transcripts are append-once at phase end, so most polls hit the cache and
# skip re-reading/parsing thousands of JSONL lines.
_CACHE: dict[str, tuple[tuple, list[dict]]] = {}


def collect_transcripts(run_dir: Path | str) -> list[dict]:
    """Return one transcript dict per non-empty stream file. Two layouts:

      sync:  `artifacts/<team>/round-<N>/stream-<phase>.jsonl`  → round_id=N
      async: `artifacts/<team>/stream-async-<n>.jsonl`          → round_id=None
             (the agent runs continuously across the whole game — no per-round
             split — so async chunks live directly under the team dir)

    Sorted by (team_id, round_id, phase). Each dict is JSON-ready for
    /game/agents.
    """
    root = Path(run_dir) / "artifacts"
    if not root.is_dir():
        return []
    # sync (round subdir) + async (flat under team). Async files are excluded
    # from the sync glob's `*/round-*/` shape, so the two sets don't overlap.
    paths = sorted(root.glob("*/round-*/stream-*.jsonl")) + sorted(
        root.glob("*/stream-async-*.jsonl")
    )

    # Cheap change-signature: stat (not read) each file. Re-parse only when
    # a file appears or changes; otherwise return the cached result.
    sig_list = []
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        sig_list.append((str(p), st.st_mtime_ns, st.st_size))
    sig = tuple(sig_list)
    cached = _CACHE.get(str(run_dir))
    if cached is not None and cached[0] == sig:
        return cached[1]

    out: list[dict] = []
    for stream_path in paths:
        m = _STREAM_RE.match(stream_path.name)
        if not m:
            continue
        rm = _ROUND_RE.match(stream_path.parent.name)
        if rm is not None:                       # sync: <team>/round-<N>/...
            team_id = stream_path.parent.parent.name
            round_id = int(rm.group("round"))
        elif _ASYNC_RE.match(m.group("phase")):  # async: <team>/stream-async-<n>
            team_id = stream_path.parent.name
            round_id = None
        else:
            continue
        events = parse_stream_json(stream_path)
        if not events:
            continue
        out.append({
            "team_id": team_id,
            "round_id": round_id,
            "phase": m.group("phase"),
            "stats": summary_stats(events),
            "events": [asdict(e) for e in events],
        })
    # round_id is None for async; coerce to -1 only for ordering (a run is
    # all-sync or all-async, so this never interleaves the two).
    out.sort(key=lambda t: (
        t["team_id"], t["round_id"] if t["round_id"] is not None else -1,
        _phase_sort_key(t["phase"]),
    ))
    _CACHE[str(run_dir)] = (sig, out)
    return out
