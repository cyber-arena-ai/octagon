"""compute_agent_metrics: derive tool-call latency + token rate from a
(stream, timing) pair, for both claude and codex schemas. Offline/synthetic."""

from __future__ import annotations

import json

from gamemgr.metrics import compute_agent_metrics, compute_run_metrics, token_rate_from_usage


def _write(tmp_path, name, stream_lines, timing):
    sp = tmp_path / name
    sp.write_text("".join(json.dumps(o) + "\n" for o in stream_lines))
    tp = tmp_path / (name + ".timing")
    tp.write_text("".join(f"{n}\t{t:.4f}\n" for n, t in timing))
    return sp, tp


def test_claude_tool_latency(tmp_path):
    """Tool timing only — tokens come from the api-router, not the stream."""
    stream = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
        {"type": "result"},
    ]
    timing = [(1, 1000.0), (2, 1002.0), (3, 1003.0), (4, 1013.0), (5, 1014.0)]
    sp, tp = _write(tmp_path, "stream-async-0.jsonl", stream, timing)

    m = compute_agent_metrics(sp, tp)
    assert m["provider"] == "claude"
    assert m["summary"]["n_tool_calls"] == 1
    assert m["tool_calls"][0]["tool"] == "Bash"
    assert m["tool_calls"][0]["latency_s"] == 10.0            # 1013 - 1003
    assert "token_samples" not in m                            # tokens are the router's job


def test_codex_tool_latency(tmp_path):
    stream = [
        {"type": "thread.started", "thread_id": "x"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "i1", "item_type": "command_execution",
                                          "command": "curl foo"}},
        {"type": "item.completed", "item": {"id": "i1"}},
        {"type": "turn.completed", "usage": {"output_tokens": 60}},
    ]
    timing = [(1, 2000.0), (2, 2000.5), (3, 2001.0), (4, 2031.0), (5, 2033.0)]
    sp, tp = _write(tmp_path, "stream-async-0.jsonl", stream, timing)

    m = compute_agent_metrics(sp, tp)
    assert m["provider"] == "codex"
    assert m["summary"]["n_tool_calls"] == 1
    assert m["tool_calls"][0]["tool"] == "curl foo"
    assert m["tool_calls"][0]["latency_s"] == 30.0            # 2031 - 2001


def test_the_10s_vs_30s_gap_is_visible(tmp_path):
    """The headline use case: same tool, team1 fast vs team2 slow → the p50
    latencies expose the environment gap."""
    claude = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "curl"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a"}]}},
    ]
    codex = [
        {"type": "item.started", "item": {"id": "a", "item_type": "command_execution", "command": "curl"}},
        {"type": "item.completed", "item": {"id": "a"}},
    ]
    sp1, tp1 = _write(tmp_path, "team1.jsonl", claude, [(1, 100.0), (2, 110.0)])   # 10s
    sp2, tp2 = _write(tmp_path, "team2.jsonl", codex, [(1, 100.0), (2, 130.0)])    # 30s
    m1 = compute_agent_metrics(sp1, tp1)
    m2 = compute_agent_metrics(sp2, tp2)
    assert m1["summary"]["tool_latency_p50"] == 10.0
    assert m2["summary"]["tool_latency_p50"] == 30.0


def test_missing_timing_is_graceful(tmp_path):
    stream = [{"type": "assistant", "message": {"content": [{"type": "text"}], "usage": {"output_tokens": 3}}}]
    sp = tmp_path / "s.jsonl"
    sp.write_text("".join(json.dumps(o) + "\n" for o in stream))
    m = compute_agent_metrics(sp, tmp_path / "does-not-exist.timing")
    assert m is not None and m["summary"]["n_tool_calls"] == 0  # no crash, no times


def test_token_rate_from_usage_is_provider_symmetric(tmp_path):
    """The api-router per-call log gives a token rate for BOTH providers — incl.
    the codex case where the stream yields nothing."""
    up = tmp_path / "usage-router.jsonl"
    rows = [
        {"ts": 100.0, "team_id": "team1", "output_tokens": 40, "latency_ms": 2000},  # 20/s
        {"ts": 105.0, "team_id": "team2", "output_tokens": 90, "latency_ms": 1000},  # 90/s
        {"ts": 110.0, "team_id": "team1", "output_tokens": 30, "latency_ms": 3000},  # 10/s
    ]
    up.write_text("".join(json.dumps(r) + "\n" for r in rows))
    m = token_rate_from_usage(up)
    assert round(m["team1"]["token_samples"][0]["tokens_per_s"], 1) == 20.0
    assert round(m["team2"]["token_samples"][0]["tokens_per_s"], 1) == 90.0
    assert m["team1"]["summary"]["total_output_tokens"] == 70
    assert m["team2"]["summary"]["n_calls"] == 1


def test_attribution_method_is_surfaced_so_collapse_is_visible(tmp_path):
    """Each call's attribution signal flows into the per-team summary, so a
    same-family collapse (all calls via a fallback signal) is visible, not
    silently bucketed. See persist_router_usage."""
    up = tmp_path / "usage-router.jsonl"
    rows = [
        {"ts": 100.0, "team_id": "team1", "output_tokens": 40, "latency_ms": 1000,
         "attribution": "model-family"},
        {"ts": 101.0, "team_id": "team1", "output_tokens": 10, "latency_ms": 1000,
         "attribution": "model-family"},
        # team2 could only be named by the collapsed fallback — the smell.
        {"ts": 102.0, "team_id": "team2", "output_tokens": 20, "latency_ms": 1000,
         "attribution": "principal"},
    ]
    up.write_text("".join(json.dumps(r) + "\n" for r in rows))
    m = token_rate_from_usage(up)
    assert m["team1"]["summary"]["attribution_methods"] == ["model-family"]
    assert m["team2"]["summary"]["attribution_methods"] == ["principal"]
    assert m["team1"]["token_samples"][0]["attribution"] == "model-family"


def test_summary_rolls_up_cost_input_cache_models(tmp_path):
    """The per-team summary carries the non-timeline totals (cost, input/cache
    tokens, models) that used to live in the standalone usage-router.json —
    consolidated so there is one aggregate, not two."""
    up = tmp_path / "usage-router.jsonl"
    rows = [
        {"ts": 100.0, "team_id": "team1", "output_tokens": 40, "input_tokens": 100,
         "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5,
         "cost_usd": 0.02, "model": "claude-sonnet-4-6", "latency_ms": 1000},
        {"ts": 101.0, "team_id": "team1", "output_tokens": 60, "input_tokens": 200,
         "cache_read_input_tokens": 20, "cache_creation_input_tokens": 0,
         "cost_usd": 0.03, "model": "claude-sonnet-4-6", "latency_ms": 1000},
    ]
    up.write_text("".join(json.dumps(r) + "\n" for r in rows))
    s = token_rate_from_usage(up)["team1"]["summary"]
    assert s["total_output_tokens"] == 100
    assert s["total_input_tokens"] == 300
    assert s["cache_read_input_tokens"] == 30
    assert s["cache_creation_input_tokens"] == 5
    assert round(s["total_cost_usd"], 2) == 0.05
    assert s["configured_model"] is None
    assert s["models"] == ["claude-sonnet-4-6"]
    assert s["provider_models"] == []
    assert s["n_calls"] == 2


def test_summary_preserves_configured_and_provider_models(tmp_path):
    up = tmp_path / "usage-router.jsonl"
    rows = [
        {"ts": 100.0, "team_id": "team1", "output_tokens": 40, "input_tokens": 100,
         "cost_usd": 0.02, "model": "gemini-3.7-flash", "provider_model": "gemini-3.5-flash",
         "configured_model": "gemini-3.7-flash", "latency_ms": 1000},
    ]
    up.write_text("".join(json.dumps(r) + "\n" for r in rows))
    team = token_rate_from_usage(up)["team1"]
    s = team["summary"]
    sample = team["token_samples"][0]
    assert s["configured_model"] == "gemini-3.7-flash"
    assert s["models"] == ["gemini-3.7-flash"]
    assert s["provider_models"] == ["gemini-3.5-flash"]
    assert sample["model"] == "gemini-3.7-flash"
    assert sample["provider_model"] == "gemini-3.5-flash"
    assert sample["configured_model"] == "gemini-3.7-flash"


def test_compute_run_metrics_reads_configured_model_from_run_config(tmp_path):
    run_dir = tmp_path / "run"
    art = run_dir / "artifacts"
    team1 = art / "team1"
    team1.mkdir(parents=True)
    (run_dir / "config.yaml").write_text(
        "agents:\n"
        "  team1:\n"
        "    model: gemini-3.7-flash\n"
    )
    (art / "usage-router.jsonl").write_text(
        json.dumps({
            "ts": 100.0, "team_id": "team1", "output_tokens": 40, "input_tokens": 100,
            "latency_ms": 1000, "model": "gemini-3.5-flash",
            "provider_model": "gemini-3.5-flash",
        }) + "\n"
    )
    s = compute_run_metrics(run_dir)["team1"]["summary"]
    assert s["configured_model"] == "gemini-3.7-flash"
    assert s["models"] == ["gemini-3.5-flash"]
    assert s["provider_models"] == ["gemini-3.5-flash"]
