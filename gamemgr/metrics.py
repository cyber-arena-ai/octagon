"""Per-agent token-rate + tool-call-latency metrics.

Derived from a run's agent stream (`stream-<phase>.jsonl`) paired with its timing
sidecar (`stream-<phase>.jsonl.timing`, `<line_no>\\t<epoch_ts>` per line, written
by agents/transport_docker at capture time). Two signals, both on an ABSOLUTE
wall-clock axis so the two teams are directly comparable at the same instant:

  * token output speed — each assistant message / codex turn reports
    `output_tokens`; we attribute it to the interval it was generated in
    (`prev_line_ts .. this_line_ts`) → tokens/sec. While a tool runs the agent
    emits no tokens, so those intervals are simply absent (the gaps).
  * tool-call latency — every tool call (MCP tool, shell, file edit, …) shows up
    as a start/end pair; latency = end − start. Lets a human spot an unfair
    environment ("team1's call took 10s, team2's 30s → network").

Provider-agnostic: claude stream-json (tool_use↔tool_result by id, per-message
usage) and codex exec --json (item.started↔item.completed by id, turn usage).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

# codex `exec --json` top-level types (dotted); everything else is claude.
_CODEX_TYPES = {
    "thread.started", "turn.started", "turn.completed", "turn.failed",
    "item.started", "item.completed", "item.updated",
}
_CODEX_TOOL_ITEMS = {"mcp_tool_call", "command_execution", "file_change"}


def _load_timing(path: str | Path) -> dict[int, float]:
    """line_no -> arrival epoch seconds (missing/garbled lines are skipped)."""
    out: dict[int, float] = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        n, _, t = line.partition("\t")
        try:
            out[int(n)] = float(t)
        except ValueError:
            continue
    return out


def _percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = int(k)
    if f + 1 >= len(s):
        return s[f]
    return s[f] + (s[f + 1] - s[f]) * (k - f)


def _codex_tool_label(item: dict) -> str:
    itype = item.get("item_type") or item.get("type") or "tool"
    label = item.get("tool") or item.get("server") or item.get("command") or itype
    return str(label)[:80]


def _configured_models_from_run(run_dir: str | Path) -> dict[str, str]:
    """Best-effort team -> configured model from the run's saved config.yaml.

    Prefer the current `agents.<team>.model` shape, but also accept the older
    `teams[].agent.model` / `topology.teams[].agent.model` layouts so metrics
    can still surface the operator-requested model on mixed-era artifacts.
    """
    path = Path(run_dir) / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except OSError:
        return {}
    except yaml.YAMLError:
        return {}

    out: dict[str, str] = {}

    agents = raw.get("agents")
    if isinstance(agents, dict):
        for team_id, cfg in agents.items():
            if not isinstance(cfg, dict):
                continue
            model = cfg.get("model")
            if model:
                out[str(team_id)] = str(model)

    def _merge_team_list(items) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            team_id = item.get("id")
            agent = item.get("agent") or {}
            model = agent.get("model") if isinstance(agent, dict) else None
            if team_id and model and str(team_id) not in out:
                out[str(team_id)] = str(model)

    _merge_team_list(raw.get("teams"))
    topology = raw.get("topology") or {}
    if isinstance(topology, dict):
        _merge_team_list(topology.get("teams"))

    return out


def compute_agent_metrics(stream_path: str | Path, timing_path: str | Path) -> dict | None:
    """Per-agent TOOL-call timing from the (stream, timing) pair — every tool call
    (MCP / shell / file / …) as a start→end pair, for the claude and codex schemas.

    Token usage does NOT come from here: it is read uniformly from the api-router
    (`token_rate_from_usage`), the single provider-agnostic source, so adding a new
    agent framework needs no token parser. The stream is still the only source for
    TOOL calls (they never touch the router). None if the stream is unreadable/empty."""
    ts = _load_timing(timing_path)
    try:
        raw_lines = Path(stream_path).read_text().splitlines()
    except OSError:
        return None
    lines: list[tuple[int, dict]] = []
    for i, raw in enumerate(raw_lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # Skip non-dict JSON lines (e.g. a bare string emitted by the OpenHands SDK
        # driver) — the parsers below assume dicts (`o.get(...)`).
        if not isinstance(obj, dict):
            continue
        lines.append((i, obj))
    if not lines:
        return None

    provider = "codex" if any(o.get("type") in _CODEX_TYPES for _, o in lines[:8]) else "claude"
    tool_calls: list[dict] = []
    pending: dict[str, tuple[str, float | None]] = {}  # id -> (label, start_ts)

    for ln, o in lines:
        t = ts.get(ln)
        typ = o.get("type")
        if provider == "claude":
            if typ == "assistant":
                for b in (o.get("message", {}) or {}).get("content", []) or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        pending[b.get("id")] = (b.get("name") or "tool", t)
            elif typ == "user":
                content = (o.get("message", {}) or {}).get("content", [])
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            name, st = pending.pop(b.get("tool_use_id"), (None, None))
                            if name is not None and st is not None and t is not None:
                                tool_calls.append({"tool": name, "start": st, "end": t,
                                                   "latency_s": t - st})
        else:  # codex
            if typ == "item.started":
                it = o.get("item") or {}
                if (it.get("item_type") or it.get("type")) in _CODEX_TOOL_ITEMS:
                    pending[it.get("id")] = (_codex_tool_label(it), t)
            elif typ == "item.completed":
                it = o.get("item") or {}
                name, st = pending.pop(it.get("id"), (None, None))
                if name is not None and st is not None and t is not None:
                    tool_calls.append({"tool": name, "start": st, "end": t,
                                       "latency_s": t - st})

    lat = [c["latency_s"] for c in tool_calls]
    return {
        "provider": provider,
        "tool_calls": tool_calls,
        "summary": {
            "n_tool_calls": len(tool_calls),
            "tool_latency_p50": _percentile(lat, 0.5),
            "tool_latency_p90": _percentile(lat, 0.9),
        },
    }


def token_rate_from_usage(
    usage_jsonl_path: str | Path,
    *,
    configured_models: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Per-team token-rate time-series from the api-router per-call log
    (`artifacts/usage-router.jsonl`: one `{ts, team_id, attribution,
    output_tokens, input_tokens, latency_ms, model, provider_model,
    configured_model, cost_usd, cache_*}` per LLM call). This is the
    PROVIDER-SYMMETRIC token source — the only one that works for codex, whose
    stream never emits usable token usage during play. `ts` is the call's
    completion instant; the tokens were generated over `[ts - latency, ts]`.
    The per-team `summary` carries both the timeline stats and the non-timeline
    totals (cost, input/cache tokens, configured/requested/provider model
    names) — the latter subsume the former standalone `usage-router.json`
    aggregate."""
    by_team: dict[str, list] = {}
    agg: dict[str, dict] = {}  # team -> aggregate totals (subsumes usage-router.json)
    try:
        lines = Path(usage_jsonl_path).read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        team, ts, out = r.get("team_id"), r.get("ts"), int(r.get("output_tokens") or 0)
        if team is None or ts is None:
            continue
        lat = r.get("latency_ms")
        attr = r.get("attribution")
        configured_model = r.get("configured_model") or (configured_models or {}).get(team)
        sample = {
            "output_tokens": out,
            "tokens_per_s": None,
            "attribution": attr,
            "model": r.get("model"),
            "provider_model": r.get("provider_model"),
            "configured_model": configured_model,
        }
        if lat:
            dur = float(lat) / 1000.0
            s = {
                "start": float(ts) - dur,
                "end": float(ts),
                **sample,
                "tokens_per_s": (out / dur) if dur > 0 else None,
            }
        else:
            s = {"start": float(ts), "end": float(ts), **sample}
        by_team.setdefault(team, []).append(s)
        # Non-timeline totals (cost, input/cache tokens, configured/requested/
        # provider model names) — kept off the samples to keep the panel payload
        # lean, rolled into the summary below.
        a = agg.setdefault(team, {"total_input_tokens": 0, "total_cost_usd": 0.0,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 0, "models": [],
                                  "provider_models": [], "configured_model": None})
        a["total_input_tokens"] += int(r.get("input_tokens") or 0)
        a["total_cost_usd"] += float(r.get("cost_usd") or 0.0)
        a["cache_read_input_tokens"] += int(r.get("cache_read_input_tokens") or 0)
        a["cache_creation_input_tokens"] += int(r.get("cache_creation_input_tokens") or 0)
        model = r.get("model")
        provider_model = r.get("provider_model")
        if model and model not in a["models"]:
            a["models"].append(model)
        if provider_model and provider_model not in a["provider_models"]:
            a["provider_models"].append(provider_model)
        if configured_model and not a["configured_model"]:
            a["configured_model"] = configured_model
    result: dict[str, dict] = {}
    for team, samples in by_team.items():
        samples.sort(key=lambda x: x["end"])
        tot = sum(s["output_tokens"] for s in samples)
        span = (samples[-1]["end"] - samples[0]["start"]) if samples else 0.0
        # Which signal(s) attributed this team's calls. Anything other than
        # ["model-family"] on a run you expected to split by family is the smell
        # that attribution collapsed (shared token/IP) — see persist_router_usage.
        methods = sorted({s["attribution"] for s in samples if s.get("attribution")})
        a = agg[team]
        result[team] = {"token_samples": samples, "summary": {
            "n_calls": len(samples), "total_output_tokens": tot, "wall_s": span,
            "mean_tokens_per_s": (tot / span) if span > 0 else None,
            "attribution_methods": methods,
            "total_input_tokens": a["total_input_tokens"],
            "total_cost_usd": round(a["total_cost_usd"], 6),
            "cache_read_input_tokens": a["cache_read_input_tokens"],
            "cache_creation_input_tokens": a["cache_creation_input_tokens"],
            "configured_model": a["configured_model"],
            "models": sorted(a["models"]),
            "provider_models": sorted(a["provider_models"]),
        }}
    return result


def compute_run_metrics(run_dir: str | Path) -> dict[str, dict]:
    """Per-team timeline metrics for a whole run, for the panel + trajectory zip.

    ONE source per signal, both provider-agnostic:
      * tool activity/latency — the agent stream + `.timing`;
      * token-rate — the api-router per-call log (`usage-router.jsonl`), the SAME
        source for every provider/framework. No per-CLI token parsing, no
        source-mixing. A team absent from the router log gets an empty token
        series and `token_source: "none"` — surfaced, never silently swapped for
        a different source."""
    root = Path(run_dir)
    art = root / "artifacts"
    configured_models = _configured_models_from_run(root)
    tokens = token_rate_from_usage(art / "usage-router.jsonl", configured_models=configured_models)
    result: dict[str, dict] = {}
    for team_dir in sorted(art.glob("team*")):
        team = team_dir.name
        tool_calls: list = []
        provider = None
        for sp in sorted(team_dir.glob("stream-*.jsonl")):
            m = compute_agent_metrics(sp, str(sp) + ".timing")
            if m:
                provider = m["provider"]
                tool_calls += m["tool_calls"]
        tk = tokens.get(team)
        lat = [c["latency_s"] for c in tool_calls]
        result[team] = {
            "provider": provider,
            "token_source": "api-router" if tk else "none",
            "tool_calls": tool_calls,
            "token_samples": tk["token_samples"] if tk else [],
            "summary": {
                "n_tool_calls": len(tool_calls),
                "tool_latency_p50": _percentile(lat, 0.5),
                "tool_latency_p90": _percentile(lat, 0.9),
                "total_output_tokens": tk["summary"]["total_output_tokens"] if tk else 0,
                "total_input_tokens": tk["summary"]["total_input_tokens"] if tk else 0,
                "total_cost_usd": tk["summary"]["total_cost_usd"] if tk else 0.0,
                "cache_read_input_tokens": tk["summary"]["cache_read_input_tokens"] if tk else 0,
                "cache_creation_input_tokens": tk["summary"]["cache_creation_input_tokens"] if tk else 0,
                "configured_model": (
                    tk["summary"]["configured_model"] if tk else configured_models.get(team)
                ),
                "models": tk["summary"]["models"] if tk else [],
                "provider_models": tk["summary"]["provider_models"] if tk else [],
                "mean_tokens_per_s": tk["summary"]["mean_tokens_per_s"] if tk else None,
                "attribution_methods": tk["summary"]["attribution_methods"] if tk else [],
            },
        }
    return result
