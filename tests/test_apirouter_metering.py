"""apirouter.metering — usage extraction, cost calc, JSONL sink."""

import json

import pytest

from apirouter.metering import MeteringSink, cost_from_pricing, extract_usage

PRICING = {"deepseek-v4-flash": {"input": 0.27, "output": 1.10}}


def test_extract_usage_openai_shape():
    u = extract_usage({"prompt_tokens": 11, "completion_tokens": 7})
    assert u["input_tokens"] == 11 and u["output_tokens"] == 7


def test_extract_usage_anthropic_shape():
    u = extract_usage({"input_tokens": 11, "output_tokens": 7})
    assert u["input_tokens"] == 11 and u["output_tokens"] == 7


def test_extract_usage_gemini_native_shape():
    u = extract_usage({
        "promptTokenCount": 11,
        "candidatesTokenCount": 7,
        "totalTokenCount": 18,
    })
    assert u["input_tokens"] == 11 and u["output_tokens"] == 7


def test_extract_usage_dual_shape_zero_alias():
    """ppapi (and similar OpenAI-shape proxies fronting Claude/Gemini) include
    BOTH alias sets, with input_tokens/output_tokens stuck at 0 while
    prompt_tokens/completion_tokens carry the real counts. The normalizer must
    pick the real number, not lock onto the 0."""
    # Real ppapi shape for a streamed claude-haiku-4-5 reply.
    u = extract_usage({
        "prompt_tokens": 9, "completion_tokens": 8,
        "input_tokens": 9, "output_tokens": 0,
    })
    assert u["input_tokens"] == 9 and u["output_tokens"] == 8
    # And for gemini, where BOTH input and output Anthropic-aliases are 0.
    u = extract_usage({
        "prompt_tokens": 2, "completion_tokens": 1,
        "input_tokens": 0, "output_tokens": 0,
    })
    assert u["input_tokens"] == 2 and u["output_tokens"] == 1


def test_extract_usage_cached_tokens():
    u = extract_usage({"prompt_tokens": 100, "completion_tokens": 5,
                       "prompt_tokens_details": {"cached_tokens": 20}})
    assert u["cache_read_input_tokens"] == 20
    # prompt_tokens INCLUDES the 20 cached; input_tokens is normalized to the plain
    # uncached 80 so billing cache_read on top doesn't double-count.
    assert u["input_tokens"] == 80


def test_extract_usage_anthropic_native_cache_not_stripped():
    """Anthropic reports input_tokens already EXCLUDING the cache buckets (which
    arrive as top-level siblings, no details object). input_tokens must pass through
    untouched — subtracting cache_read here would undercount."""
    u = extract_usage({"input_tokens": 80, "output_tokens": 5,
                       "cache_read_input_tokens": 20,
                       "cache_creation_input_tokens": 10})
    assert u["input_tokens"] == 80
    assert u["cache_read_input_tokens"] == 20
    assert u["cache_creation_input_tokens"] == 10


def test_extract_usage_gemini_cache_not_double_billed():
    """Gemini usageMetadata keeps promptTokenCount cache-inclusive; subtract the
    cached bucket so cache_read can be billed separately without double-counting."""
    u = extract_usage({
        "promptTokenCount": 100,
        "cachedContentTokenCount": 20,
        "candidatesTokenCount": 5,
        "totalTokenCount": 105,
    })
    assert u["input_tokens"] == 80
    assert u["output_tokens"] == 5
    assert u["cache_read_input_tokens"] == 20


def test_extract_usage_none():
    u = extract_usage(None)
    assert u == {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def test_cost_from_pricing():
    # 1M input @0.27 + 0.5M output @1.10 = 0.27 + 0.55 = 0.82
    assert cost_from_pricing(PRICING, "deepseek-v4-flash", 1_000_000, 500_000) == pytest.approx(0.82)


def test_cost_from_pricing_unknown_model():
    assert cost_from_pricing(PRICING, "mystery", 100, 100) is None


def test_cost_from_pricing_ignores_cache_tokens_when_rates_absent():
    """An {input, output}-only entry must keep costing exactly what it did before
    cache pricing existed — cache tokens flow through as 0-rated, not an error."""
    assert cost_from_pricing(PRICING, "deepseek-v4-flash", 1_000_000, 500_000,
                             cache_read_tokens=1_000_000,
                             cache_creation_tokens=1_000_000) == pytest.approx(0.82)


def test_cost_from_pricing_bills_cache_tokens():
    """Cache tokens are ~80% of real spend on a caching model; a priced cache_read/
    cache_write must actually show up in cost, not just input/output."""
    pricing = {"claude-opus-4-8": {
        "input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25,
    }}
    # 1M fresh input @5.00 + 1M output @25.00 + 1M cache_read @0.50 + 1M cache_write @6.25
    # = 5.00 + 25.00 + 0.50 + 6.25 = 36.75
    cost = cost_from_pricing(pricing, "claude-opus-4-8",
                             1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert cost == pytest.approx(36.75)
    # And cache-only (no fresh input/output) still bills — the case that was
    # silently free before this fix.
    cache_only = cost_from_pricing(pricing, "claude-opus-4-8", 0, 0, 1_000_000, 1_000_000)
    assert cache_only == pytest.approx(6.75)


def test_extract_usage_live_ppapi_responses_shape():
    """Pins a real ppapi Responses `usage` (captured live): input_tokens INCLUDES
    the cached subset (7288 total, 4352 cached), cache rides input_tokens_details
    (cache reads under cached_tokens, writes under cache_write_tokens). The 4352
    cached are stripped to 2936 plain input; billing input + cache_read then counts
    the 7288 once."""
    u = extract_usage({
        "input_tokens": 7288, "output_tokens": 5,
        "input_tokens_details": {"cached_tokens": 4352, "cache_write_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 7293,
    })
    assert u["input_tokens"] == 2936
    assert u["cache_read_input_tokens"] == 4352
    assert u["cache_creation_input_tokens"] == 0


def test_extract_usage_responses_cache_write_from_details():
    """Responses cache WRITES ride input_tokens_details.cache_write_tokens (not the
    top-level Anthropic name); they must be billed and stripped from input."""
    u = extract_usage({
        "input_tokens": 100, "output_tokens": 3,
        "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 30},
    })
    assert u["cache_creation_input_tokens"] == 30
    assert u["cache_read_input_tokens"] == 20
    assert u["input_tokens"] == 50   # 100 - 20 cached - 30 written = 50 plain


def test_openai_shape_cache_not_double_billed():
    """Regression for the double-count concern: OpenAI/Responses `prompt_tokens`
    already INCLUDES the cached subset, so extract_usage strips it and the cached
    tokens are billed once (cache_read rate), not twice (input rate + cache_read)."""
    pricing = {"m": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25}}
    u = extract_usage({"prompt_tokens": 100, "completion_tokens": 0,
                       "prompt_tokens_details": {"cached_tokens": 20}})
    cost = cost_from_pricing(pricing, "m", u["input_tokens"], u["output_tokens"],
                             u["cache_read_input_tokens"], u["cache_creation_input_tokens"])
    # 80 plain input @5.00 + 20 cache_read @0.50, per 1M — the cached 20 are NOT also
    # billed at the input rate.
    assert cost == pytest.approx((80 * 5.0 + 20 * 0.50) / 1_000_000.0)
    assert cost != pytest.approx((100 * 5.0 + 20 * 0.50) / 1_000_000.0)


def test_sink_writes_jsonl(tmp_path):
    p = tmp_path / "usage.jsonl"
    sink = MeteringSink(jsonl_path=p, stdout=False)
    sink.record({"event": "llm", "client_ip": "10.1.0.2", "input_tokens": 5})
    sink.record({"event": "egress", "host": "x.com", "allowed": False})
    sink.close()

    lines = p.read_text().strip().splitlines()
    assert len(lines) == 2
    r0 = json.loads(lines[0])
    assert r0["client_ip"] == "10.1.0.2" and r0["input_tokens"] == 5 and "ts" in r0
    assert json.loads(lines[1])["event"] == "egress"


def test_extract_usage_responses_api_cached():
    """Responses API (codex) reports cached tokens under input_tokens_details,
    not prompt_tokens_details. Note it reuses the name `input_tokens` but with
    OpenAI semantics — cache-INCLUSIVE — so the details object (not the field name)
    is what marks the shape; the 12 cached are stripped to leave 8 plain input."""
    u = extract_usage({"input_tokens": 20, "output_tokens": 6,
                       "input_tokens_details": {"cached_tokens": 12}})
    assert u["input_tokens"] == 8 and u["output_tokens"] == 6
    assert u["cache_read_input_tokens"] == 12
