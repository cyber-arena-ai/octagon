"""Defensive request rewrites for flaky "OpenAI-compatible" upstreams.

Pure helpers (no I/O) so they're trivially unit-tested; the gateway wires them
in + owns the retry/fallback orchestration. Each works on the raw request body
bytes and returns (possibly rewritten) bytes — a non-JSON or unparseable body is
returned untouched.

Covers the known proxy quirks (see docs/api_router_plan_v1.md §Compat):
  #1 empty assistant `content:""` alongside `tool_calls` → drop the empty content
  #3 unsupported params (e.g. `reasoning_effort`) → strip configured keys
  #4 strict `response_format: json_schema` rejected → downgrade to `json_object`
  #11 client `max_tokens` exceeds the upstream model's hard cap → clamp per-model
"""

from __future__ import annotations

import json
from typing import Iterable, Mapping


def sanitize_request_body(
    body: bytes,
    *,
    drop_empty_content: bool = False,
    drop_params: Iterable[str] = (),
    clamp_max_tokens: Mapping[str, int] | None = None,
) -> bytes:
    """#1 + #3 + #11. Drop empty `content` on assistant messages that carry
    `tool_calls`, strip top-level params named in `drop_params`, and clamp
    `max_tokens` down to the configured ceiling for the request's `model`."""
    drop_params = tuple(drop_params)
    caps = dict(clamp_max_tokens or {})
    if not (drop_empty_content or drop_params or caps):
        return body
    try:
        obj = json.loads(body)
    except Exception:
        return body
    if not isinstance(obj, dict):
        return body

    changed = False
    if drop_empty_content:
        for m in obj.get("messages", []) or []:
            if isinstance(m, dict) and m.get("content") == "" and m.get("tool_calls"):
                m.pop("content", None)
                changed = True
    for p in drop_params:
        if p in obj:
            obj.pop(p, None)
            changed = True
    if caps:
        cap = caps.get(obj.get("model"))
        mt = obj.get("max_tokens")
        if cap is not None and isinstance(mt, int) and mt > cap:
            obj["max_tokens"] = cap
            changed = True

    return json.dumps(obj).encode() if changed else body


def is_json_schema_request(body: bytes) -> bool:
    """True iff the request asks for `response_format.type == json_schema`."""
    try:
        rf = json.loads(body).get("response_format")
    except Exception:
        return False
    return isinstance(rf, dict) and rf.get("type") == "json_schema"


def downgrade_to_json_object(body: bytes) -> bytes:
    """#4. Replace a `json_schema` response_format with plain `json_object`."""
    try:
        obj = json.loads(body)
    except Exception:
        return body
    if isinstance(obj, dict) and isinstance(obj.get("response_format"), dict):
        obj["response_format"] = {"type": "json_object"}
        return json.dumps(obj).encode()
    return body


def normalize_tool_schemas(body: bytes) -> tuple[bytes, list[str]]:
    """Make every `tools[].function.parameters` (Chat Completions) a schema a
    strictly validating upstream accepts: drop keywords whose value is `null`
    (no JSON-Schema keyword is validly null) and give an object schema that has
    no `required` an explicit `required: []`.

    hy4-preview's serving stack (2026-09-18) refused EVERY request carrying a
    no-argument tool with `Tool 2 function has invalid 'parameters' schema:
    None is not of type 'array'`: the gateway's `get_game_state` is the first
    tool sent without a `required` key, and that upstream's own model fills the
    missing key with None and then validates it — so an absent `required` is as
    fatal there as a null one, while permissive upstreams never noticed either.
    Returns (body, names of tools touched); a clean body comes back as the same
    bytes."""
    try:
        obj = json.loads(body)
    except Exception:
        return body, []
    if not isinstance(obj, dict) or not isinstance(obj.get("tools"), list):
        return body, []
    touched: list[str] = []
    for t in obj["tools"]:
        fn = t.get("function") if isinstance(t, dict) else None
        params = fn.get("parameters") if isinstance(fn, dict) else None
        if not isinstance(params, dict):
            continue
        nulls = [k for k, v in params.items() if v is None]
        for k in nulls:
            del params[k]
        add_required = params.get("type") == "object" and "required" not in params
        if add_required:
            params["required"] = []
        if nulls or add_required:
            touched.append(str(fn.get("name") or "?"))
    if not touched:
        return body, []
    return json.dumps(obj).encode(), touched
