"""apirouter.gateway — verbatim reverse passthrough + metering.

The upstream is a httpx.MockTransport, so we assert both directions: what the
proxy forwards upstream (verbatim) and what it returns to the client (markdown
repaired), plus the metering rows.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from apirouter import config as cfgmod
from apirouter.gateway import build_app
from apirouter.metering import MeteringSink

OPENAI_RESP = {
    "id": "cc-1", "object": "chat.completion", "model": "deepseek-v4-flash",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
}


def _build(tmp_path, responder, *, api_key="REAL-KEY", seen=None):
    raw = {"upstream": {"base_url": "https://up.example", "api_key": api_key},
           "pricing": {"deepseek-v4-flash": {"input": 0.27, "output": 1.10}}}
    cfg = cfgmod.from_dict(raw)
    sink = MeteringSink(jsonl_path=tmp_path / "usage.jsonl", stdout=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            request.read()
            seen.append(request)
        return responder(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = build_app(cfg, sink=sink, client=client)
    return TestClient(app), tmp_path / "usage.jsonl"


def _json_resp(payload, *, markdown=False, ct="application/json"):
    body = json.dumps(payload)
    if markdown:
        body = "```json\n" + body + "\n```"
    return lambda req: httpx.Response(200, headers={"content-type": ct}, content=body.encode())


def test_healthz(tmp_path):
    client, _ = _build(tmp_path, _json_resp(OPENAI_RESP))
    assert client.get("/healthz").json() == {"status": "ok"}


def test_passthrough_and_metered(tmp_path):
    client, usage = _build(tmp_path, _json_resp(OPENAI_RESP))
    r = client.post("/v1/chat/completions",
                    json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi"  # body passed through
    rec = json.loads(usage.read_text().strip())
    assert rec["model"] == "deepseek-v4-flash" and rec["input_tokens"] == 11 and rec["output_tokens"] == 7
    assert rec["cost_usd"] == (11 * 0.27 + 7 * 1.10) / 1_000_000
    assert rec["client_ip"] and rec["path"] == "v1/chat/completions"


def test_markdown_wrapped_response_repaired(tmp_path):
    client, usage = _build(tmp_path, _json_resp(OPENAI_RESP, markdown=True))
    r = client.post("/v1/chat/completions", json={"model": "deepseek-v4-flash", "messages": []})
    # client receives clean JSON despite upstream wrapping it in a fence
    assert r.json()["id"] == "cc-1"
    assert json.loads(usage.read_text().strip())["output_tokens"] == 7


def test_gemini_native_json_is_metered_and_records_provider_model(tmp_path):
    payload = {
        "modelVersion": "gemini-3.7-flash-002",
        "usageMetadata": {
            "promptTokenCount": 100,
            "cachedContentTokenCount": 40,
            "candidatesTokenCount": 7,
            "totalTokenCount": 107,
        },
    }
    client, usage = _build(tmp_path, _json_resp(payload))
    r = client.post("/v1beta/models/gemini-3.7-flash:generateContent",
                    json={"model": "deepseek-v4-flash", "contents": []})
    assert r.status_code == 200
    rec = json.loads(usage.read_text().strip())
    assert rec["input_tokens"] == 60
    assert rec["output_tokens"] == 7
    assert rec["cache_read_input_tokens"] == 40
    assert rec["provider_model"] == "gemini-3.7-flash-002"


def test_forwards_verbatim(tmp_path):
    seen = []
    client, _ = _build(tmp_path, _json_resp(OPENAI_RESP), seen=seen)
    client.post("/v1/chat/completions",
                headers={"x-custom": "keep-me"},
                json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "yo"}]})
    fwd = seen[0]
    assert str(fwd.url) == "https://up.example/v1/chat/completions"
    assert fwd.method == "POST"
    assert json.loads(fwd.content)["messages"][0]["content"] == "yo"
    assert fwd.headers.get("x-custom") == "keep-me"  # arbitrary headers pass through


def test_api_key_injection(tmp_path):
    seen = []
    client, _ = _build(tmp_path, _json_resp(OPENAI_RESP), api_key="REAL-KEY", seen=seen)
    client.post("/v1/chat/completions", headers={"authorization": "Bearer client-sent"},
                json={"model": "deepseek-v4-flash", "messages": []})
    fwd = seen[0]
    assert fwd.headers["authorization"] == "Bearer REAL-KEY"  # client's key dropped
    assert fwd.headers["x-api-key"] == "REAL-KEY"
    assert fwd.headers["x-goog-api-key"] == "REAL-KEY"


def test_gemini_native_api_key_injection_strips_client_header(tmp_path):
    seen = []
    client, _ = _build(tmp_path, _json_resp(OPENAI_RESP), api_key="REAL-KEY", seen=seen)
    client.post("/v1beta/models/gemini-3.7-flash:generateContent",
                headers={"x-goog-api-key": "client-principal-token"},
                json={"model": "deepseek-v4-flash", "contents": []})
    fwd = seen[0]
    assert fwd.headers["authorization"] == "Bearer REAL-KEY"
    assert fwd.headers["x-api-key"] == "REAL-KEY"
    assert fwd.headers["x-goog-api-key"] == "REAL-KEY"


def test_cache_tokens_are_billed(tmp_path):
    """End-to-end: a model priced with cache_read/cache_write must show up in
    cost_usd for cache_read_input_tokens/cache_creation_input_tokens the upstream
    reports — this is the ~80%-of-spend traffic that cost_usd silently dropped
    before this fix."""
    raw = {"upstream": {"base_url": "https://up.example", "api_key": "REAL-KEY"},
           "pricing": {"claude-opus-4-8": {
               "input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25}}}
    cfg = cfgmod.from_dict(raw)
    sink = MeteringSink(jsonl_path=tmp_path / "usage.jsonl", stdout=False)
    resp = {
        "id": "msg-1", "model": "claude-opus-4-8",
        "usage": {"input_tokens": 100, "output_tokens": 50,
                  "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200},
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(_json_resp(resp)))
    app = build_app(cfg, sink=sink, client=client)
    tc = TestClient(app)
    r = tc.post("/v1/messages", json={"model": "claude-opus-4-8", "messages": []})
    assert r.status_code == 200
    rec = json.loads(usage.read_text().strip()) if (usage := tmp_path / "usage.jsonl").exists() else None
    assert rec is not None
    expected = (100 * 5.0 + 50 * 25.0 + 1000 * 0.50 + 200 * 6.25) / 1_000_000.0
    assert rec["cost_usd"] == pytest.approx(expected)
    assert rec["cache_read_input_tokens"] == 1000
    assert rec["cache_creation_input_tokens"] == 200


def test_non_api_passthrough_not_metered(tmp_path):
    client, usage = _build(tmp_path, _json_resp({"hello": "world"}))  # no usage field
    r = client.get("/some/health")
    assert r.status_code == 200 and r.json() == {"hello": "world"}
    assert not usage.exists() or usage.read_text().strip() == ""


def _build_principals(tmp_path, responder):
    raw = {"upstream": {"base_url": "https://up.example", "api_key": "REAL"},
           "pricing": {"deepseek-v4-flash": {"input": 0.27, "output": 1.10}},
           "principals": [{"id": "alice", "token": "alice-tok"}, {"id": "bob", "token": "bob-tok"}]}
    cfg = cfgmod.from_dict(raw)
    sink = MeteringSink(jsonl_path=tmp_path / "usage.jsonl", stdout=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    return TestClient(build_app(cfg, sink=sink, client=client)), tmp_path / "usage.jsonl"


def test_principal_attribution(tmp_path):
    client, usage = _build_principals(tmp_path, _json_resp(OPENAI_RESP))
    r = client.post("/v1/chat/completions", headers={"authorization": "Bearer alice-tok"},
                    json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 200
    assert json.loads(usage.read_text().strip())["principal"] == "alice"


def test_principal_via_x_api_key(tmp_path):
    client, usage = _build_principals(tmp_path, _json_resp(OPENAI_RESP))
    r = client.post("/v1/chat/completions", headers={"x-api-key": "bob-tok"},
                    json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 200
    assert json.loads(usage.read_text().strip())["principal"] == "bob"


def test_principal_via_x_goog_api_key(tmp_path):
    client, usage = _build_principals(tmp_path, _json_resp(OPENAI_RESP))
    r = client.post("/v1beta/models/gemini-3.7-flash:generateContent",
                    headers={"x-goog-api-key": "bob-tok"},
                    json={"model": "deepseek-v4-flash", "contents": []})
    assert r.status_code == 200
    assert json.loads(usage.read_text().strip())["principal"] == "bob"


def test_unknown_token_401_not_metered(tmp_path):
    client, usage = _build_principals(tmp_path, _json_resp(OPENAI_RESP))
    r = client.post("/v1/chat/completions", headers={"authorization": "Bearer nope-secret-token-xyz"},
                    json={"model": "deepseek-v4-flash", "messages": []})
    assert r.status_code == 401
    rows = [json.loads(x) for x in usage.read_text().splitlines() if x.strip()]
    # Not METERED (no billable `llm` row) — the anti-oracle rule is "log, don't bill".
    assert not any(r.get("event") == "llm" for r in rows)
    # But it DOES leave an auth_reject trace so a 100%-failing agent isn't invisible…
    assert any(r.get("event") == "auth_reject" and r.get("reason") == "unknown" for r in rows)
    # …and the rejected token itself is NEVER written (a fingerprint stands in for it).
    assert "nope-secret-token-xyz" not in usage.read_text()


def _build_budget(tmp_path, responder):
    # pricing $1/token → OPENAI_RESP (11 in + 7 out) costs $18 per call.
    raw = {"upstream": {"base_url": "https://up.example", "api_key": "REAL"},
           "pricing": {"deepseek-v4-flash": {"input": 1_000_000, "output": 1_000_000}},
           "principals": [{"id": "alice", "token": "a", "budget_usd": 10},
                          {"id": "bob", "token": "b"}]}  # bob: no cap
    cfg = cfgmod.from_dict(raw)
    sink = MeteringSink(jsonl_path=tmp_path / "usage.jsonl", stdout=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    return TestClient(build_app(cfg, sink=sink, client=client)), tmp_path / "usage.jsonl"


def test_budget_blocks_after_cap(tmp_path):
    client, usage = _build_budget(tmp_path, _json_resp(OPENAI_RESP))
    body = {"model": "deepseek-v4-flash", "messages": []}
    r1 = client.post("/v1/chat/completions", headers={"authorization": "Bearer a"}, json=body)
    assert r1.status_code == 200          # pre-check spent 0 < 10 → allowed (now spent 18)
    r2 = client.post("/v1/chat/completions", headers={"authorization": "Bearer a"}, json=body)
    assert r2.status_code == 402          # pre-check spent 18 >= 10 → blocked
    assert r2.json()["error"]["type"] == "budget_exceeded"
    rows = [json.loads(x) for x in usage.read_text().splitlines()]
    assert any(r.get("event") == "budget_block" and r["principal"] == "alice" for r in rows)


def test_no_budget_is_unlimited(tmp_path):
    client, _ = _build_budget(tmp_path, _json_resp(OPENAI_RESP))
    for _ in range(3):
        r = client.post("/v1/chat/completions", headers={"authorization": "Bearer b"},
                        json={"model": "deepseek-v4-flash", "messages": []})
        assert r.status_code == 200


def _compat_client(tmp_path, handler, compat):
    cfg = cfgmod.from_dict({"upstream": {"base_url": "https://up.example", "api_key": "K"},
                            "pricing": {"m": {"input": 0, "output": 0}}, "compat": compat})
    sink = MeteringSink(jsonl_path=tmp_path / "usage.jsonl", stdout=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(build_app(cfg, sink=sink, client=client))


def test_compat_drop_empty_content_and_params(tmp_path):
    seen = []

    def handler(req):
        req.read(); seen.append(json.loads(req.content))
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps(OPENAI_RESP).encode())

    tc = _compat_client(tmp_path, handler,
                        {"drop_empty_content": True, "drop_params": ["reasoning_effort"]})
    tc.post("/v1/chat/completions", json={"model": "m", "reasoning_effort": "high",
            "messages": [{"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}]})
    fwd = seen[0]
    assert "reasoning_effort" not in fwd                 # #3 stripped
    assert "content" not in fwd["messages"][0]           # #1 dropped


def test_compat_request_too_large(tmp_path):
    tc = _compat_client(tmp_path, lambda r: httpx.Response(200), {"max_request_bytes": 50})
    r = tc.post("/v1/chat/completions",
                json={"model": "m", "messages": [{"role": "user", "content": "x" * 500}]})
    assert r.status_code == 413 and r.json()["error"]["type"] == "request_too_large"


def test_compat_retry_on_503(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, content=b"busy")
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps(OPENAI_RESP).encode())

    tc = _compat_client(tmp_path, handler, {"retry_on_5xx": 3, "retry_backoff_ms": 1})
    r = tc.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r.status_code == 200 and calls["n"] == 3      # #10: 2 retries then success


def test_compat_json_schema_downgrade(tmp_path):
    seen = []

    def handler(req):
        req.read(); body = json.loads(req.content); seen.append(body)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, content=b'{"error":"does not support json_schema"}')
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps(OPENAI_RESP).encode())

    tc = _compat_client(tmp_path, handler, {"json_schema_fallback": True})
    r = tc.post("/v1/chat/completions", json={"model": "m", "messages": [],
                "response_format": {"type": "json_schema", "json_schema": {"name": "x"}}})
    assert r.status_code == 200                          # #4: recovered
    assert seen[0]["response_format"]["type"] == "json_schema"   # first attempt
    assert seen[1]["response_format"]["type"] == "json_object"   # downgraded retry


def test_usage_endpoint(tmp_path):
    client, _ = _build_budget(tmp_path, _json_resp(OPENAI_RESP))
    client.post("/v1/chat/completions", headers={"authorization": "Bearer b"},
                json={"model": "deepseek-v4-flash", "messages": []})  # bob: 1 call
    rep = client.get("/usage").json()
    by = {r["principal"]: r for r in rep["principals"]}
    assert by["bob"]["calls"] == 1
    assert by["bob"]["input_tokens"] == 11 and by["bob"]["output_tokens"] == 7
    assert by["alice"]["calls"] == 0           # capped principal, listed with 0 usage
    assert by["alice"]["budget_usd"] == 10.0
    assert rep["total_cost_usd"] == by["bob"]["cost_usd"]


def test_streaming_passthrough_and_metered(tmp_path):
    parts = [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
             b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
             b'data: [DONE]\n\n']

    def responder(req):
        async def stream():
            for p in parts:
                yield p
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())

    client, usage = _build(tmp_path, responder)
    r = client.post("/v1/chat/completions", json={"model": "deepseek-v4-flash", "stream": True, "messages": []})
    assert r.status_code == 200
    assert "delta" in r.text  # stream passed through verbatim
    rec = json.loads(usage.read_text().strip())
    assert rec["stream"] is True and rec["input_tokens"] == 5 and rec["output_tokens"] == 2


def test_streaming_responses_api_metered(tmp_path):
    """Codex hits the Responses API: usage rides the terminal `response.completed`
    event under `response.usage` (not top-level `usage`). The router must meter it
    — this is what makes codex token usage land in the JSONL at all."""
    parts = [b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n',
             b'data: {"type":"response.completed","response":{"usage":'
             b'{"input_tokens":13,"output_tokens":4,'
             b'"input_tokens_details":{"cached_tokens":8}}}}\n\n']

    def responder(req):
        async def stream():
            for p in parts:
                yield p
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())

    client, usage = _build(tmp_path, responder)
    r = client.post("/v1/responses", json={"model": "deepseek-v4-flash", "stream": True, "input": "hi"})
    assert r.status_code == 200
    rec = json.loads(usage.read_text().strip())
    assert rec["stream"] is True
    # Responses `input_tokens` (13) INCLUDES the 8 cached; it's normalized to the 5
    # plain uncached, with the 8 reported as cache_read — so billing input + cache_read
    # counts 13 distinct tokens once each, not the cached 8 twice.
    assert rec["input_tokens"] == 5 and rec["output_tokens"] == 4
    assert rec["cache_read_input_tokens"] == 8


def test_streaming_gemini_native_metered_and_records_provider_model(tmp_path):
    parts = [
        b'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}],"modelVersion":"gemini-3.7-flash-002"}\n\n',
        b'data: {"usageMetadata":{"promptTokenCount":12,"cachedContentTokenCount":5,"candidatesTokenCount":3,"totalTokenCount":15},"modelVersion":"gemini-3.7-flash-002"}\n\n',
        b'data: [DONE]\n\n',
    ]

    def responder(req):
        async def stream():
            for p in parts:
                yield p
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream())

    client, usage = _build(tmp_path, responder)
    r = client.post("/v1beta/models/gemini-3.7-flash:streamGenerateContent",
                    json={"model": "deepseek-v4-flash", "stream": True, "contents": []})
    assert r.status_code == 200
    rec = json.loads(usage.read_text().strip())
    assert rec["stream"] is True
    assert rec["input_tokens"] == 7
    assert rec["output_tokens"] == 3
    assert rec["cache_read_input_tokens"] == 5
    assert rec["provider_model"] == "gemini-3.7-flash-002"
