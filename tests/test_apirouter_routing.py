"""apirouter multi-upstream routing — model globs pick the upstream AND its key.

The point of the feature: one router can front several providers on separate
credentials (Claude on the official Anthropic key, everything else via a
reseller), chosen per request from the model the client sent. So the assertions
that matter are (a) which host the request went to and (b) which key it carried —
sending a claude-* call to the right host with the reseller's key would defeat it.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from apirouter import config as cfgmod
from apirouter.gateway import build_app
from apirouter.metering import MeteringSink

ROUTED = {
    "upstreams": {
        "anthropic": {"base_url": "https://api.anthropic.com", "api_key": "SK-OFFICIAL"},
        "ppapi": {"base_url": "https://app-us.ppapi.ai", "api_key": "SK-RESELLER"},
    },
    "routes": {
        "default": "ppapi",
        "by_model": {"claude-*": "anthropic"},
    },
}

RESP = {
    "id": "cc-1", "model": "x", "choices": [],
    "usage": {"prompt_tokens": 3, "completion_tokens": 5},
}


def _build(tmp_path, raw):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps(RESP).encode())

    cfg = cfgmod.from_dict(raw)
    sink = MeteringSink(jsonl_path=tmp_path / "usage.jsonl", stdout=False)
    app = build_app(cfg, sink=sink,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return TestClient(app), seen, tmp_path / "usage.jsonl"


# --- routing -------------------------------------------------------------

@pytest.mark.parametrize("model,host,key", [
    ("claude-opus-4-8", "api.anthropic.com", "SK-OFFICIAL"),   # by_model glob
    ("gpt-4o", "app-us.ppapi.ai", "SK-RESELLER"),              # unmatched -> default
])
def test_model_picks_upstream_and_its_key(tmp_path, model, host, key):
    client, seen, _ = _build(tmp_path, ROUTED)
    client.post("/v1/messages", json={"model": model, "messages": []})
    assert seen[0].url.host == host
    assert seen[0].headers["x-api-key"] == key
    assert seen[0].headers["authorization"] == f"Bearer {key}"


def test_client_token_never_reaches_upstream(tmp_path):
    """The agent's principal token must be swapped for the upstream's real key —
    on EVERY route, not just the default one."""
    client, seen, _ = _build(tmp_path, ROUTED)
    client.post("/v1/messages", json={"model": "claude-opus-4-8", "messages": []},
                headers={"x-api-key": "cad-team1", "authorization": "Bearer cad-team1"})
    assert "cad-team1" not in json.dumps(dict(seen[0].headers))


def test_bodyless_request_goes_to_default(tmp_path):
    client, seen, _ = _build(tmp_path, ROUTED)
    client.get("/v1/models")
    assert seen[0].url.host == "app-us.ppapi.ai"


def test_first_matching_glob_wins(tmp_path):
    """by_model is ordered, so an earlier broad rule shadows a later narrow one —
    the reverse of most-specific-wins. Pin it so the docs stay honest."""
    raw = {**ROUTED, "routes": {"default": "ppapi", "by_model": {
        "claude-*": "anthropic", "claude-haiku-*": "ppapi"}}}
    client, seen, _ = _build(tmp_path, raw)
    client.post("/v1/messages", json={"model": "claude-haiku-4-5", "messages": []})
    assert seen[0].url.host == "api.anthropic.com"


def test_metering_row_records_upstream(tmp_path):
    client, _, usage = _build(tmp_path, ROUTED)
    client.post("/v1/messages", json={"model": "claude-opus-4-8", "messages": []})
    client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": []})
    rows = [json.loads(line) for line in usage.read_text().splitlines()]
    assert [r["upstream"] for r in rows] == ["anthropic", "ppapi"]


# --- config: the two spellings are mutually exclusive ---------------------

def test_single_upstream_desugars_to_default_route():
    cfg = cfgmod.from_dict({"upstream": {"base_url": "https://up.example", "api_key": "K"}})
    assert cfg.routes.default == "default"
    assert cfg.upstream.base_url == "https://up.example"   # back-compat accessor
    assert cfg.upstream_for("anything")[1].api_key == "K"


@pytest.mark.parametrize("extra", [
    {"upstreams": {"a": {"base_url": "https://a.example", "api_key": "K"}}},
    {"routes": {"default": "ppapi"}},
])
def test_upstream_plus_routing_table_is_rejected(extra):
    """The question this feature invites: 'if I have both, which wins?' Neither —
    it fails at load, so nobody has to learn a precedence rule."""
    with pytest.raises(ValueError, match="mutually exclusive|cannot be combined"):
        cfgmod.from_dict({"upstream": {"base_url": "https://up.example", "api_key": "K"}, **extra})


def test_default_required_when_several_upstreams():
    with pytest.raises(ValueError, match="routes.default is required"):
        cfgmod.from_dict({"upstreams": {
            "a": {"base_url": "https://a.example", "api_key": "K"},
            "b": {"base_url": "https://b.example", "api_key": "K"}}})


def test_lone_upstream_needs_no_explicit_default():
    cfg = cfgmod.from_dict({"upstreams": {"solo": {"base_url": "https://a.example", "api_key": "K"}}})
    assert cfg.routes.default == "solo"


def test_route_to_undeclared_upstream_is_rejected():
    with pytest.raises(ValueError, match="unknown upstream 'typo'"):
        cfgmod.from_dict({
            "upstreams": {"ppapi": {"base_url": "https://a.example", "api_key": "K"}},
            "routes": {"default": "ppapi", "by_model": {"claude-*": "typo"}},
        })
