"""game.yaml → per-model pricing + per-team USD caps, enforced by the router.

These two are ONE feature, not two: a budget is checked against the cost the router
computes, and cost comes from the pricing table. Wire a cap without a price and it can
never fire — spend stays $0 no matter how many tokens burn. So the tests here follow
the money the whole way: game.yaml → generate() → apirouter's loader → a real 402.

Default stays free: omit `pricing` and rates are 0.0, which is what every config did
before this existed (tokens counted, cost reported as zero).
"""

from __future__ import annotations

import dataclasses
import json
import os

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

os.environ.setdefault("ANTHROPIC_API_KEY_TEAM1", "t1-key")
os.environ.setdefault("CODEX_API_KEY_TEAM2", "t2-key")
for _k in ("CYBERARENA_FACILITY_TOKEN", "API_ROUTER_KEY", "UPSTREAM_API_KEY"):
    os.environ.setdefault(_k, "router-key")

from apirouter.config import from_dict as router_from_dict  # noqa: E402
from apirouter.gateway import build_app  # noqa: E402
from apirouter.metering import MeteringSink  # noqa: E402
from gamemgr.apirouter_cfg import (  # noqa: E402
    assign_router_principals,
    generate,
    resolve_upstream_keys,
)
from gamemgr.config import _from_dict as game_from_dict  # noqa: E402
from gamemgr.config import load as load_game_config  # noqa: E402

CONFIG = "config/game_ico_v3_isolated_async_mixed.yaml"

# 1M in + 1M out, so a rate of $3/$15 per 1M lands exactly $18 of spend per call.
BIG_USAGE = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}


def _priced_game(pricing=None, budgets=None):
    cfg = load_game_config(CONFIG)
    cfg = dataclasses.replace(cfg, api_router=dataclasses.replace(
        cfg.api_router, pricing=pricing or {}, budgets=budgets or {}))
    resolve_upstream_keys(cfg)
    assign_router_principals(cfg)
    return cfg


def _router(game_cfg, tmp_path):
    """The real router, built from the config game.yaml generated."""
    cfg = router_from_dict(yaml.safe_load(generate(game_cfg)))

    def upstream(_req):
        return httpx.Response(200, headers={"content-type": "application/json"},
                              content=json.dumps({"usage": BIG_USAGE}).encode())

    sink = MeteringSink(jsonl_path=tmp_path / "usage.jsonl", stdout=False)
    app = build_app(cfg, sink=sink,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    return TestClient(app), cfg, tmp_path / "usage.jsonl"


def _call(client, token, model="claude-sonnet-4-6"):
    return client.post("/v1/messages", json={"model": model, "messages": []},
                       headers={"x-api-key": token})


# --- default: free ---------------------------------------------------------

def test_default_is_free_and_uncapped(tmp_path):
    """No pricing, no budgets → rates 0.0, no caps. Pre-existing behaviour."""
    client, cfg, usage = _router(_priced_game(), tmp_path)
    assert cfg.budgets == {}
    assert all(r == {"input": 0.0, "output": 0.0} for r in cfg.pricing.values())

    assert _call(client, "cad-team1").status_code == 200
    assert json.loads(usage.read_text().strip())["cost_usd"] == 0.0


# --- pricing: cost actually gets computed ----------------------------------

def test_pricing_from_game_yaml_produces_real_cost(tmp_path):
    game_cfg = _priced_game(pricing={"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}})
    client, _, usage = _router(game_cfg, tmp_path)

    assert _call(client, "cad-team1").status_code == 200
    row = json.loads(usage.read_text().strip())
    assert row["cost_usd"] == pytest.approx(3.0 + 15.0)   # 1M in + 1M out
    assert row["principal"] == "team1"


def test_unpriced_model_still_works_and_costs_zero(tmp_path):
    """Pricing one model must not break the others — an absent model is free, not an
    error, so a half-filled table degrades to today's behaviour rather than 500s."""
    game_cfg = _priced_game(pricing={"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}})
    client, _, usage = _router(game_cfg, tmp_path)

    # Must be a model the CONFIG names (here: team2's) — apirouter_cfg.generate seeds
    # the table with every configured model at 0.0, so this exercises the priced-zero
    # path. A model absent from BOTH records cost as null + warns, which is a
    # different behaviour; don't conflate them by naming an unconfigured model.
    assert _call(client, "cad-team2", model="gpt-5.4").status_code == 200
    assert json.loads(usage.read_text().strip())["cost_usd"] == 0.0


# --- budget: the cap actually fires ----------------------------------------

def test_budget_cap_fires_with_402(tmp_path):
    """The whole point: a team that burns past its cap is cut off.

    SOFT cap, by design (see budget.py): cost is only known after a call returns, so
    the check is a pre-check on accumulated spend. The call that crosses the line
    completes; the next one is refused. $18/call against a $10 cap ⇒ call 1 goes
    through (spend was still $0), call 2 is refused."""
    game_cfg = _priced_game(
        pricing={"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}},   # $18/call
        budgets={"team1": 10.00})
    client, _, _ = _router(game_cfg, tmp_path)

    assert _call(client, "cad-team1").status_code == 200    # pre-check: spent $0
    r = _call(client, "cad-team1")                          # pre-check: $18 >= $10
    assert r.status_code == 402
    assert "budget" in r.json()["error"]["type"]


def test_budget_is_per_team_not_shared(tmp_path):
    """team1 exhausting its cap must not cut team2 off — principals are the isolation
    boundary, which is the reason per-team tokens exist at all."""
    game_cfg = _priced_game(
        pricing={"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}},
        budgets={"team1": 1.00})                            # team2: uncapped
    client, _, _ = _router(game_cfg, tmp_path)

    _call(client, "cad-team1")
    assert _call(client, "cad-team1").status_code == 402    # team1 is done
    assert _call(client, "cad-team2").status_code == 200    # team2 unaffected


def test_usage_endpoint_reports_spend_and_remaining(tmp_path):
    game_cfg = _priced_game(
        pricing={"claude-sonnet-4-6": {"input": 3.0, "output": 15.0}},
        budgets={"team1": 50.00})
    client, _, _ = _router(game_cfg, tmp_path)
    _call(client, "cad-team1")

    rows = client.get("/usage").json()["principals"]
    team1 = next(p for p in rows if p["principal"] == "team1")
    assert team1["cost_usd"] == pytest.approx(18.0)
    assert team1["budget_usd"] == pytest.approx(50.0)
    assert team1["remaining_usd"] == pytest.approx(32.0)


# --- config validation -----------------------------------------------------

def test_budget_for_unknown_team_is_rejected():
    """Same reference-integrity rule as routes→upstreams: a cap on a team that does
    not exist caps nothing, and would sit there looking like protection."""
    with pytest.raises(ValueError, match="unknown team 'team9'"):
        game_from_dict({
            "networks": {}, "nodes": [],
            "teams": [{"id": "team1", "dev": "d", "prod": "p"}],
            "api_router": {"container": "api-router", "budgets": {"team9": 5.0}},
        })


@pytest.mark.parametrize("bad", [-1, "5.00", True])
def test_rejects_nonsense_rates_and_caps(bad):
    base = {"networks": {}, "nodes": [], "teams": [{"id": "team1", "dev": "d", "prod": "p"}]}
    with pytest.raises(ValueError, match="non-negative number"):
        game_from_dict({**base, "api_router": {
            "container": "r", "pricing": {"m": {"input": bad, "output": 1.0}}}})
    with pytest.raises(ValueError, match="non-negative number"):
        game_from_dict({**base, "api_router": {
            "container": "r", "budgets": {"team1": bad}}})


def test_budget_without_pricing_warns_that_it_can_never_fire(caplog):
    """The trap this feature invites: a cap enforced against a cost that is always $0.
    It fails open and silently, so generate() has to say so."""
    game_cfg = _priced_game(budgets={"team1": 5.00})      # no pricing at all
    with caplog.at_level("WARNING"):
        generate(game_cfg)
    assert "can NEVER trigger" in caplog.text
    assert "team1" in caplog.text
