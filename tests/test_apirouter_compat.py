"""apirouter.compat — defensive request rewrites (pure functions)."""

import json

from apirouter.compat import (
    downgrade_to_json_object,
    is_json_schema_request,
    sanitize_request_body,
)


def test_drop_empty_content_with_tool_calls():
    body = json.dumps({"messages": [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "user", "content": "hi"},
    ]}).encode()
    out = json.loads(sanitize_request_body(body, drop_empty_content=True))
    assert "content" not in out["messages"][0]      # dropped (empty + tool_calls)
    assert out["messages"][1]["content"] == "hi"     # untouched


def test_keep_empty_content_without_tool_calls():
    body = json.dumps({"messages": [{"role": "assistant", "content": ""}]}).encode()
    out = json.loads(sanitize_request_body(body, drop_empty_content=True))
    assert out["messages"][0]["content"] == ""       # no tool_calls → leave it


def test_drop_params():
    body = json.dumps({"model": "m", "reasoning_effort": "high", "temperature": 0.5}).encode()
    out = json.loads(sanitize_request_body(body, drop_params=["reasoning_effort"]))
    assert "reasoning_effort" not in out and out["temperature"] == 0.5


def test_clamp_max_tokens():
    # Over the cap -> clamped.
    body = json.dumps({"model": "gpt-4o-mini", "max_tokens": 64000}).encode()
    out = json.loads(sanitize_request_body(body, clamp_max_tokens={"gpt-4o-mini": 16384}))
    assert out["max_tokens"] == 16384
    # Under the cap -> untouched.
    body = json.dumps({"model": "gpt-4o-mini", "max_tokens": 1024}).encode()
    out = json.loads(sanitize_request_body(body, clamp_max_tokens={"gpt-4o-mini": 16384}))
    assert out["max_tokens"] == 1024
    # Model not in the cap table -> untouched.
    body = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 64000}).encode()
    out = sanitize_request_body(body, clamp_max_tokens={"gpt-4o-mini": 16384})
    assert json.loads(out)["max_tokens"] == 64000


def test_sanitize_noop_and_non_json():
    body = b'{"x":1}'
    assert sanitize_request_body(body) is body                       # nothing enabled
    assert sanitize_request_body(b"not json", drop_empty_content=True) == b"not json"


def test_is_json_schema_request():
    assert is_json_schema_request(json.dumps({"response_format": {"type": "json_schema"}}).encode())
    assert not is_json_schema_request(json.dumps({"response_format": {"type": "json_object"}}).encode())
    assert not is_json_schema_request(b"{}")


def test_downgrade_to_json_object():
    body = json.dumps({"response_format": {"type": "json_schema", "json_schema": {"name": "x"}}}).encode()
    out = json.loads(downgrade_to_json_object(body))
    assert out["response_format"] == {"type": "json_object"}


def test_normalize_tool_schemas_drops_null_required_only_where_present():
    """opencode sends `required: null` for a no-argument MCP tool; a strictly
    validating upstream refuses the whole request over it. The router drops
    null-valued schema keywords and leaves everything else byte-identical."""
    from apirouter.compat import normalize_tool_schemas

    body = json.dumps({
        "model": "hy4-preview",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "bash", "parameters": {
                "type": "object", "properties": {"command": {"type": "string"}},
                "required": ["command"]}}},
            {"type": "function", "function": {"name": "get_game_state", "parameters": {
                "type": "object", "properties": {}, "required": None}}},
            {"type": "function", "function": {"name": "report_ready", "parameters": {
                "type": "object", "properties": None, "required": None}}},
        ],
    }).encode()
    out, touched = normalize_tool_schemas(body)
    assert touched == ["get_game_state", "report_ready"]
    tools = json.loads(out)["tools"]
    assert tools[0]["function"]["parameters"]["required"] == ["command"]
    assert tools[1]["function"]["parameters"] == {"type": "object", "properties": {},
                                                  "required": []}
    assert tools[2]["function"]["parameters"] == {"type": "object", "required": []}
    # idempotent, and a clean body comes back as the SAME bytes
    again, touched2 = normalize_tool_schemas(out)
    assert again is out and touched2 == []


def test_normalize_tool_schemas_adds_required_to_a_no_arg_tool():
    """The gateway's no-argument tools carry no `required` key at all — the
    shape hy4-preview's upstream refused — so an object schema without one
    gets an explicit empty list; a non-object schema is left alone."""
    from apirouter.compat import normalize_tool_schemas

    body = json.dumps({"model": "m", "messages": [], "tools": [
        {"type": "function", "function": {"name": "get_game_state", "parameters": {
            "type": "object", "properties": {}, "title": "get_game_stateArguments"}}},
        {"type": "function", "function": {"name": "odd", "parameters": {"type": "string"}}},
    ]}).encode()
    out, touched = normalize_tool_schemas(body)
    assert touched == ["get_game_state"]
    tools = json.loads(out)["tools"]
    assert tools[0]["function"]["parameters"]["required"] == []
    assert tools[0]["function"]["parameters"]["title"] == "get_game_stateArguments"
    assert tools[1]["function"]["parameters"] == {"type": "string"}


def test_normalize_tool_schemas_leaves_non_chat_bodies_alone():
    from apirouter.compat import normalize_tool_schemas

    for raw in (b"not json", b"[1,2]", json.dumps({"model": "m", "input": "x"}).encode(),
                json.dumps({"tools": [{"type": "web_search"}]}).encode()):
        out, touched = normalize_tool_schemas(raw)
        assert out is raw and touched == []
