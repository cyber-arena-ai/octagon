"""apirouter.gateway — operator debuggability: which team, which request, what error.

Drives the real app via httpx.MockTransport + fastapi TestClient (same style as
test_apirouter_gateway). For every audited failure path we assert BOTH that the
router logs it AND that it leaves a row in the sink, that the correlation id ties
the response header <-> the JSONL row <-> the log line, that a successful stream is
NOT buffered, and that no secret (the injected upstream key, the client token) ever
reaches either sink — including a key echoed back inside an upstream error body.
"""

import asyncio
import json
import logging
import time

import httpx
from fastapi.testclient import TestClient

from apirouter import config as cfgmod
from apirouter.gateway import build_app, _refusal_category_from_err
from apirouter.metering import MeteringSink

OPENAI_RESP = {
    "id": "cc-1", "object": "chat.completion", "model": "deepseek-v4-flash",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}

# A key-shaped upstream credential, so the shape-regex path is exercised too.
UPSTREAM_KEY = "sk-abcdefghijklmnopqrstuvwx"


def _make(tmp_path, responder, *, api_key=UPSTREAM_KEY, pricing=None, budgets=None, compat=None):
    raw = {"upstream": {"base_url": "https://up.example", "api_key": api_key},
           "pricing": pricing or {"deepseek-v4-flash": {"input": 0.27, "output": 1.10},
                                  "claude-sonnet-4": {"input": 3.0, "output": 15.0}},
           "principals": [{"id": "team1", "token": "cad-team1"},
                          {"id": "team2", "token": "cad-team2"}]}
    for p in raw["principals"]:
        if budgets and p["id"] in budgets:
            p["budget_usd"] = budgets[p["id"]]
    if compat:
        raw["compat"] = compat
    cfg = cfgmod.from_dict(raw)
    sink = MeteringSink(jsonl_path=tmp_path / "usage.jsonl", stdout=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    return build_app(cfg, sink=sink, client=client), tmp_path / "usage.jsonl"


def _build(tmp_path, responder, **kw):
    app, usage = _make(tmp_path, responder, **kw)
    return TestClient(app), usage


async def _drive_asgi(app, path, headers, body):
    """Drive the ASGI app directly and return [(monotonic_ts, message), …] for every
    `send`. Bypasses httpx's ASGITransport, which BUFFERS the whole response before
    yielding — so this is the only way to observe that the gateway emits SSE chunks
    incrementally (multiple `http.response.body` messages over time) rather than one
    buffered blob."""
    events = []
    state = {"sent": False}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Stay "connected": StreamingResponse races a disconnect-watcher on receive(),
        # so returning http.disconnect here would cancel the stream mid-flight. Block
        # instead — the watcher is cancelled once the response finishes on its own.
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(msg):
        events.append((time.monotonic(), msg))

    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "path": "/" + path, "raw_path": ("/" + path).encode(), "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("testclient", 123), "server": ("testserver", 80), "scheme": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
    }
    await app(scope, receive, send)
    return events


def _rows(usage):
    if not usage.exists():
        return []
    return [json.loads(x) for x in usage.read_text().splitlines() if x.strip()]


def _json_resp(payload, *, status=200, ct="application/json"):
    body = json.dumps(payload).encode()
    return lambda req: httpx.Response(status, headers={"content-type": ct}, content=body)


def _hdr(tok="cad-team1"):
    return {"x-api-key": tok}


# ── (1) correlation id: every row carries a well-formed request_id ────────────

def test_correlation_id_on_success_row(tmp_path):
    client, usage = _build(tmp_path, _json_resp(OPENAI_RESP))
    r = client.post("/v1/chat/completions", headers=_hdr(),
                    json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 200
    llm = [x for x in _rows(usage) if x.get("event") == "llm"]
    assert len(llm) == 1
    assert len(llm[0]["request_id"]) == 12   # 12 hex, minted by the router per request


# ── (2) headline case: Anthropic-format → OpenAI-only upstream 404 ────────────

def test_upstream_404_nonstream_logged_and_recorded(tmp_path, caplog):
    err_body = {"error": {"type": "not_found_error",
                          "message": "POST /v1/messages is not a supported endpoint"}}
    def responder(req):
        return httpx.Response(404, headers={"content-type": "application/json",
                                            "request-id": "up-req-123"},
                              content=json.dumps(err_body).encode())
    client, usage = _build(tmp_path, responder)
    with caplog.at_level(logging.WARNING, logger="apirouter.gateway"):
        r = client.post("/v1/messages", headers=_hdr(),
                        json={"model": "claude-sonnet-4", "messages": []})
    assert r.status_code == 404
    assert json.loads(r.content)["error"]["type"] == "not_found_error"  # verbatim, NOT translated
    errs = [x for x in _rows(usage) if x.get("event") == "llm_error"]
    assert len(errs) == 1
    e = errs[0]
    assert e["principal"] == "team1"              # which team
    assert e["status"] == 404 and e["reason"] == "upstream_error"   # what error
    assert e["model"] == "claude-sonnet-4" and e["path"] == "v1/messages"
    assert e["upstream_request_id"] == "up-req-123"
    assert "not a supported endpoint" in e["error"]
    assert not any(x.get("event") == "llm" for x in _rows(usage))    # no success row
    # which request: a WARNING log line carrying the SAME id as the row — that pairing
    # (log ↔ row) is the join, with the status + client-asked model visible.
    hits = [x for x in caplog.records if e["request_id"] in x.getMessage()]
    assert hits and all(x.levelno == logging.WARNING for x in hits)
    assert "404" in hits[0].getMessage() and "claude-sonnet-4" in hits[0].getMessage()


# ── 5xx (ERROR level) + an UNPARSEABLE (HTML) error body still recorded ───────

def test_upstream_5xx_html_body_logged_at_error(tmp_path, caplog):
    def responder(req):
        return httpx.Response(503, headers={"content-type": "text/html"},
                              content=b"<html><body>502 Bad Gateway (nginx)</body></html>")
    client, usage = _build(tmp_path, responder)
    with caplog.at_level(logging.WARNING, logger="apirouter.gateway"):
        r = client.post("/v1/chat/completions", headers=_hdr(),
                        json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 503
    e = [x for x in _rows(usage) if x.get("event") == "llm_error"][0]
    assert e["status"] == 503 and "nginx" in e["error"]     # unparseable body still captured
    hits = [x for x in caplog.records if e["request_id"] in x.getMessage()]
    assert hits and any(x.levelno == logging.ERROR for x in hits)   # 5xx -> ERROR


# ── non-2xx delivered WITH an SSE content-type: buffered, not streamed ────────

def test_error_with_sse_content_type_is_recorded(tmp_path):
    def responder(req):
        return httpx.Response(429, headers={"content-type": "text/event-stream"},
                              content=b'data: {"error":{"message":"rate limited"}}\n\n')
    client, usage = _build(tmp_path, responder)
    r = client.post("/v1/chat/completions", headers=_hdr(),
                    json={"model": "deepseek-v4-flash", "stream": True, "messages": []})
    assert r.status_code == 429
    errs = [x for x in _rows(usage) if x.get("event") == "llm_error"]
    assert len(errs) == 1 and errs[0]["status"] == 429 and errs[0]["stream"] is True
    assert len(errs[0]["request_id"]) == 12


# ── error arriving MID-STREAM (200 header, then an SSE error event) ───────────

def test_sse_error_event_midstream_visible_and_no_success_row(tmp_path):
    parts = [b'data: {"type":"message_start","message":{"usage":{"input_tokens":50,"output_tokens":1}}}\n\n',
             b'data: {"type":"content_block_delta","delta":{"text":"FLAG{leak_me_not}"}}\n\n',
             b'event: error\n',
             b'data: {"type":"error","error":{"type":"overloaded_error","message":"upstream overloaded"}}\n\n']
    def responder(req):
        async def stream():
            for p in parts:
                yield p
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())
    client, usage = _build(tmp_path, responder)
    r = client.post("/v1/messages", headers=_hdr(),
                    json={"model": "claude-sonnet-4", "stream": True, "messages": []})
    assert r.status_code == 200
    assert "FLAG{leak_me_not}" in r.text          # completion streamed through verbatim
    rows = _rows(usage)
    assert not any(x.get("event") == "llm" for x in rows)   # NOT a misleading success
    errs = [x for x in rows if x.get("event") == "llm_error"]
    assert len(errs) == 1 and errs[0]["reason"] == "sse_error_event"
    assert "overloaded" in errs[0]["error"]
    # the model's own output (a possible exfiltrated flag) is NEVER put in the sink
    assert "FLAG{leak_me_not}" not in usage.read_text()


# ── OpenAI cyber-policy refusal mid-stream → labeled as a refusal ─────────────

def test_sse_cyber_policy_refusal_is_labeled(tmp_path):
    # gpt-5.6-sol style: the stream starts fine, then ppapi/OpenAI aborts it with a
    # server-side cyber-policy refusal. codex only surfaces "response.failed"; the
    # router must capture code=cyber_policy AND tag the row refusal_category=cyber so
    # the harvest can count it as a refusal (not a generic transport error).
    parts = [b'data: {"type":"response.output_text.delta","delta":"analy"}\n\n',
             b'event: response.failed\n',
             b'data: {"type":"error","error":{"type":"invalid_request","code":"cyber_policy",'
             b'"message":"This content was flagged for possible cybersecurity risk. To get '
             b'authorized for security work, join the Trusted Access for Cyber program"}}\n\n']
    def responder(req):
        async def stream():
            for p in parts:
                yield p
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())
    client, usage = _build(tmp_path, responder)
    r = client.post("/v1/responses", headers=_hdr("cad-team2"),
                    json={"model": "gpt-5.6-sol", "stream": True, "input": "x"})
    assert r.status_code == 200                       # headers already went out
    errs = [x for x in _rows(usage) if x.get("event") == "llm_error"]
    assert len(errs) == 1
    e = errs[0]
    assert e["reason"] == "sse_error_event"
    assert e["refusal_category"] == "cyber"           # ← labeled as a refusal
    assert e["principal"] == "team2"                  # attributed to the codex team
    assert "cyber_policy" in e["error"]               # the machine code is captured too
    assert not any(x.get("event") == "llm" for x in _rows(usage))   # not a success row


def test_refusal_category_from_err_pure():
    # code match, message-only fallback, and clean negatives.
    assert _refusal_category_from_err("cyber_policy", "anything") == "cyber"
    assert _refusal_category_from_err("", "This content was flagged for possible "
                                          "cybersecurity risk") == "cyber"
    assert _refusal_category_from_err("", "join the Trusted Access for Cyber program") == "cyber"
    assert _refusal_category_from_err("rate_limit_exceeded", "slow down") is None
    assert _refusal_category_from_err("", "upstream overloaded") is None


# ── stream aborts AFTER the 200 headers were already sent ─────────────────────

def test_stream_aborted_after_200_recorded(tmp_path, caplog):
    def responder(req):
        async def stream():
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            raise httpx.ReadError("connection reset mid-stream")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())
    client, usage = _build(tmp_path, responder)
    with caplog.at_level(logging.WARNING, logger="apirouter.gateway"):
        r = client.post("/v1/chat/completions", headers=_hdr(),
                        json={"model": "deepseek-v4-flash", "stream": True, "messages": []})
    assert r.status_code == 200               # headers already went out
    errs = [x for x in _rows(usage) if x.get("event") == "llm_error"]
    assert len(errs) == 1 and errs[0]["reason"] == "stream_aborted"
    assert not any(x.get("event") == "llm" for x in _rows(usage))   # no misleading partial-success
    hits = [x for x in caplog.records if errs[0]["request_id"] in x.getMessage()]
    assert hits and any(x.levelno == logging.ERROR for x in hits)   # transport-class -> ERROR


# ── transport failure before any response (DNS/connect/TLS/timeout) → 502 ─────

def test_transport_failure_502_recorded_at_error(tmp_path, caplog):
    def responder(req):
        raise httpx.ConnectError("connection refused")
    client, usage = _build(tmp_path, responder)
    with caplog.at_level(logging.WARNING, logger="apirouter.gateway"):
        r = client.post("/v1/chat/completions", headers=_hdr(),
                        json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 502
    e = [x for x in _rows(usage) if x.get("event") == "llm_error"][0]
    assert e["reason"] == "transport_error" and e["status"] == 502
    assert e["principal"] == "team1"
    hits = [x for x in caplog.records if e["request_id"] in x.getMessage()]   # log ↔ row join
    assert hits and any(x.levelno == logging.ERROR for x in hits)


# ── oversize body (413): record the SIZE, never the prompt ────────────────────

def test_oversize_413_records_size_not_body(tmp_path):
    client, usage = _build(tmp_path, _json_resp(OPENAI_RESP), compat={"max_request_bytes": 60})
    r = client.post("/v1/chat/completions", headers=_hdr(),
                    json={"model": "deepseek-v4-flash",
                          "messages": [{"role": "user", "content": "NEEDLE_PROMPT" + "x" * 500}]})
    assert r.status_code == 413
    e = [x for x in _rows(usage) if x.get("event") == "llm_error"][0]
    assert e["reason"] == "oversize_request" and e["status"] == 413
    # router-generated error: the id in the JSON body the caller gets matches the row
    assert e["principal"] == "team1"
    assert e["request_id"] == json.loads(r.content)["error"]["request_id"]
    assert "NEEDLE_PROMPT" not in usage.read_text()   # the prompt is never written


# ── unknown token (401): fingerprint, never the token; not billed ─────────────

def test_unknown_token_401_records_fingerprint_not_token(tmp_path, caplog):
    client, usage = _build(tmp_path, _json_resp(OPENAI_RESP))
    with caplog.at_level(logging.WARNING, logger="apirouter.gateway"):
        r = client.post("/v1/chat/completions", headers={"authorization": "Bearer stale-cad-token-999"},
                        json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 401
    assert json.loads(r.content)["error"]["request_id"]     # id reaches the caller via the body
    rows = _rows(usage)
    assert not any(x.get("event") == "llm" for x in rows)   # not billed
    rej = [x for x in rows if x.get("event") == "auth_reject"]
    assert len(rej) == 1 and rej[0]["reason"] == "unknown" and rej[0]["token_fp"].startswith("sha256:")
    assert rej[0]["request_id"] == json.loads(r.content)["error"]["request_id"]  # body ↔ row
    assert "stale-cad-token-999" not in usage.read_text()   # the token is never written
    assert "stale-cad-token-999" not in caplog.text         # …nor logged


# ── budget block (402): now carries a correlation id ──────────────────────────

def test_budget_block_carries_request_id(tmp_path, caplog):
    pricing = {"deepseek-v4-flash": {"input": 1_000_000, "output": 1_000_000}}  # $18/call
    client, usage = _build(tmp_path, _json_resp(OPENAI_RESP), pricing=pricing, budgets={"team1": 10})
    body = {"model": "deepseek-v4-flash", "messages": []}
    assert client.post("/v1/chat/completions", headers=_hdr(), json=body).status_code == 200
    with caplog.at_level(logging.WARNING, logger="apirouter.gateway"):
        r2 = client.post("/v1/chat/completions", headers=_hdr(), json=body)
    assert r2.status_code == 402
    blk = [x for x in _rows(usage) if x.get("event") == "budget_block"]
    assert len(blk) == 1 and blk[0]["principal"] == "team1"
    rid = blk[0]["request_id"]
    assert rid == json.loads(r2.content)["error"]["request_id"]        # body ↔ row
    assert any(rid in x.getMessage() for x in caplog.records)          # log ↔ row


# ── secret safety: an upstream 401 that echoes our injected key ───────────────

def test_upstream_key_echo_is_redacted_in_row_and_log(tmp_path, caplog):
    def responder(req):
        # provider auth-error bodies routinely echo (a prefix of) the presented key
        return httpx.Response(401, headers={"content-type": "application/json"},
                              content=json.dumps({"error": {
                                  "message": f"Incorrect API key provided: {UPSTREAM_KEY}"}}).encode())
    client, usage = _build(tmp_path, responder, api_key=UPSTREAM_KEY)
    with caplog.at_level(logging.WARNING, logger="apirouter.gateway"):
        r = client.post("/v1/chat/completions", headers=_hdr(),
                        json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 401
    e = [x for x in _rows(usage) if x.get("event") == "llm_error"][0]
    assert e["status"] == 401
    assert UPSTREAM_KEY not in usage.read_text()   # never in the artifact-bound sink
    assert UPSTREAM_KEY not in caplog.text          # never in the logs
    assert "<redacted>" in e["error"]               # …but the fact of the error survives


# ── streaming success MUST NOT be buffered (else every call pays the latency) ─

def test_successful_stream_is_not_buffered(tmp_path):
    upstream_done = {"t": None}

    async def stream():
        yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
        await asyncio.sleep(0.5)   # upstream stalls mid-stream
        upstream_done["t"] = time.monotonic()
        yield b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
        yield b'data: [DONE]\n\n'

    def responder(req):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())

    app, usage = _make(tmp_path, responder)
    body = json.dumps({"model": "deepseek-v4-flash", "stream": True, "messages": []}).encode()
    events = asyncio.run(_drive_asgi(app, "v1/chat/completions", _hdr(), body))

    body_chunks = [(t, m["body"]) for t, m in events
                   if m["type"] == "http.response.body" and m.get("body")]
    # The FIRST body chunk must reach the client BEFORE the upstream finished its
    # 0.5s stall — proof the gateway teed it through instead of buffering the whole
    # response (a buffering gateway sends nothing until end-of-stream).
    assert body_chunks and b"first" in body_chunks[0][1]
    assert body_chunks[0][0] < upstream_done["t"]
    assert len(body_chunks) >= 2   # multiple incremental chunks, not one blob
    # …and it still meters the stream at end-of-stream.
    rec = [x for x in _rows(usage) if x.get("event") == "llm"][0]
    assert rec["stream"] is True and rec["input_tokens"] == 5 and rec["output_tokens"] == 2


# ── retries are visible: logged, and counted on the final row ─────────────────

def test_retry_is_logged_and_counted_on_final_row(tmp_path, caplog):
    calls = {"n": 0}
    def responder(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, content=b"busy")
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps(OPENAI_RESP).encode())
    client, usage = _build(tmp_path, responder, compat={"retry_on_5xx": 3, "retry_backoff_ms": 1})
    with caplog.at_level(logging.WARNING, logger="apirouter.gateway"):
        r = client.post("/v1/chat/completions", headers=_hdr(),
                        json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 200 and calls["n"] == 3
    rec = [x for x in _rows(usage) if x.get("event") == "llm"][0]
    assert rec["attempts"] == 3                 # 2 retries then success, on the final row
    assert any("retry" in x.getMessage() and rec["request_id"] in x.getMessage()
               for x in caplog.records)


# ── json_schema downgrade is a request mutation → it must be logged ───────────

def test_json_schema_downgrade_is_logged(tmp_path, caplog):
    def responder(req):
        req.read()
        if json.loads(req.content).get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, content=b'{"error":"json_schema not supported"}')
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps(OPENAI_RESP).encode())
    client, usage = _build(tmp_path, responder, compat={"json_schema_fallback": True})
    with caplog.at_level(logging.WARNING, logger="apirouter.gateway"):
        r = client.post("/v1/chat/completions", headers=_hdr(),
                        json={"model": "deepseek-v4-flash", "messages": [],
                              "response_format": {"type": "json_schema", "json_schema": {"name": "x"}}})
    assert r.status_code == 200   # recovered by the downgrade
    assert any("downgrad" in x.getMessage().lower() for x in caplog.records)


# ── a non-matching json_schema 400 (reconstructed eager response) is recorded ─

def test_json_schema_nonmatching_400_is_recorded(tmp_path):
    # 400 whose body does NOT mention json_schema → _forward reconstructs an eager
    # Response and returns it; it must still be recorded (and not crash).
    def responder(req):
        return httpx.Response(400, headers={"content-type": "application/json"},
                              content=b'{"error":{"message":"messages: field required"}}')
    client, usage = _build(tmp_path, responder, compat={"json_schema_fallback": True})
    r = client.post("/v1/chat/completions", headers=_hdr(),
                    json={"model": "deepseek-v4-flash", "messages": [],
                          "response_format": {"type": "json_schema", "json_schema": {"name": "x"}}})
    assert r.status_code == 400
    e = [x for x in _rows(usage) if x.get("event") == "llm_error"][0]
    assert e["status"] == 400 and "field required" in e["error"]


# ── a weird-but-successful body (non-dict `usage`) must not crash into a 500 ──

def test_malformed_usage_does_not_500(tmp_path, caplog):
    weird = {"id": "x", "model": "deepseek-v4-flash", "usage": 0}  # usage is not a dict
    client, usage = _build(tmp_path, _json_resp(weird))
    with caplog.at_level(logging.WARNING, logger="apirouter.gateway"):
        r = client.post("/v1/chat/completions", headers=_hdr(),
                        json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 200                       # returned verbatim, no router crash
    assert not any(x.get("event") == "llm" for x in _rows(usage))   # not metered
    assert any("non-dict usage" in x.getMessage() for x in caplog.records)
