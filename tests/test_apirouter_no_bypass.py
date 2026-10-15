"""An agent must not be able to reach an LLM upstream except through api-router.

If it could, its tokens would never be metered and the per-team bill would be a
fiction. Two independent defences have to hold, and each is tested here:

  1. CREDENTIAL — an agent holds a principal token the router minted (`cad-team1`),
     never a provider key. Even handed a clear path to api.anthropic.com it cannot
     authenticate, so it cannot spend anyone's money. This is the load-bearing one:
     it survives any topology mistake.
  2. NETWORK — the agent's nets are `internal: true`, so there is no route off the
     host at all; api-router is its only bridge to the NAT'd egress_net.

Defence 1 is exactly what the removed "passthrough" mode broke: with no key on the
router, agents kept their REAL upstream keys and could bill directly against them.
"""

from __future__ import annotations

import dataclasses
import os

import pytest
import yaml

os.environ.setdefault("ANTHROPIC_API_KEY_TEAM1", "REAL-UPSTREAM-KEY-1")
os.environ.setdefault("CODEX_API_KEY_TEAM2", "REAL-UPSTREAM-KEY-2")
for _k in ("CYBERARENA_FACILITY_TOKEN", "API_ROUTER_KEY", "UPSTREAM_API_KEY"):
    os.environ.setdefault(_k, "REAL-ROUTER-KEY")

import re  # noqa: E402

from apirouter.config import from_dict as router_from_dict  # noqa: E402
from gamemgr.apirouter_cfg import (  # noqa: E402
    assign_router_principals,
    generate,
    resolve_upstream_keys,
)
from gamemgr.config import load as load_game_config  # noqa: E402

CONFIG = "config/game_ico_v3_isolated_async_mixed.yaml"

# Real provider-credential shapes only — NOT the `cad-` principal label (which the
# router deliberately also redacts from logs, but is not a provider key).
_PROVIDER_KEY_RE = re.compile(r"(sk-ant-|sk-|AIza)[A-Za-z0-9_\-]{12,}")


def _looks_like_provider_key(tok: str) -> bool:
    return bool(_PROVIDER_KEY_RE.search(tok))


def _started_game():
    """A GameConfig as main.py has it at container-launch time."""
    cfg = load_game_config(CONFIG)
    resolve_upstream_keys(cfg)
    assign_router_principals(cfg)
    return cfg


# --- defence 1: the agent never holds a spendable credential ---------------

def test_agent_holds_a_principal_token_not_a_provider_key():
    """What billing needs of an agent's credential is three properties, NOT a shape:
    it is distinct per team (or attribution collapses), it is not a provider key (or
    the team can spend unmetered), and the router recognises it (or every call 401s).
    The `cad-<team>` spelling is `assign_router_principals`' business — asserting it
    here would tie a billing invariant to a naming choice."""
    cfg = _started_game()
    assert cfg.agents, "config declares no agents — test would vacuously pass"

    tokens = {tid: ac.api_key for tid, ac in cfg.agents.items()}
    assert all(tokens.values()), "an agent was left with no credential at all"
    # 1. distinct per team — two teams on one token collapse into one principal
    assert len(set(tokens.values())) == len(tokens)
    # 2. not a provider key: the token an agent presents must be a non-secret label,
    #    never something shaped like a real credential (which would spend unmetered).
    for tok in tokens.values():
        assert not _looks_like_provider_key(tok), f"agent holds a provider-key-shaped token: {tok!r}"
    # 3. the router recognises each one, so it is billable rather than a 401
    principals = {p["token"]: p["id"] for p in yaml.safe_load(generate(cfg))["principals"]}
    assert set(tokens.values()) <= set(principals)
    assert len({principals[t] for t in tokens.values()}) == len(tokens)  # 1 principal/team


def test_no_real_upstream_key_reaches_any_agent_container():
    """The strong form: the router's real key must not appear ANYWHERE an agent can
    read it — not in its api_key, not in its container env."""
    cfg = _started_game()
    router = next(n for n in cfg.topology.nodes if n.id == cfg.api_router.container)
    real_keys = {v for v in router.env.values() if v}
    assert real_keys, "router carries no key — the premise of this test is gone"

    dev_nodes = {t.dev for t in cfg.topology.teams}
    for node in cfg.topology.nodes:
        if node.id not in dev_nodes:
            continue
        leaked = real_keys & set(node.env.values())
        assert not leaked, f"{node.id} env carries the router's upstream key"
    for agent in cfg.agents.values():
        assert agent.api_key not in real_keys


def test_router_rejects_a_token_it_did_not_mint():
    """An agent cannot invent a principal to bill: unknown tokens get 401, and are
    not metered (no billing, and no token-probing oracle either)."""
    import httpx
    from fastapi.testclient import TestClient

    from apirouter.gateway import build_app
    from apirouter.metering import MeteringSink

    cfg = router_from_dict(yaml.safe_load(generate(_started_game())))
    rows: list[dict] = []
    sink = MeteringSink(jsonl_path=None, stdout=False)
    sink.record = rows.append  # type: ignore[method-assign]

    def upstream(_req):   # must never be reached
        raise AssertionError("router forwarded a request bearing an unminted token")

    client = TestClient(build_app(
        cfg, sink=sink, client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))))
    r = client.post("/v1/messages",
                    json={"model": "claude-opus-4-8", "messages": []},
                    headers={"x-api-key": "cad-team99-i-made-this-up"})

    assert r.status_code == 401
    assert not [row for row in rows if row.get("event") == "llm"]


def test_unrouted_team_keeps_its_key_and_never_becomes_a_principal():
    """A team whose base_url points straight at a provider is a legitimate mode: it
    authenticates with its own real key and talks to the provider directly (unmetered
    — `generate` logs a warning). What must NOT happen is that key being registered as
    a router principal: principal tokens are written verbatim into the generated
    config, so it would land a live provider key in a run artifact that is designed to
    be handed to consumers (see RUN_ARTIFACT_SPEC).
    """
    cfg = load_game_config(CONFIG)
    real = "sk-live-provider-key"
    cfg.agents["team2"] = dataclasses.replace(
        cfg.agents["team2"], base_url="https://api.anthropic.com", api_key=real)
    resolve_upstream_keys(cfg)
    assigned = assign_router_principals(cfg)

    # the direct team is left alone — its key IS the credential it calls the API with
    assert "team2" not in assigned
    assert cfg.agents["team2"].api_key == real

    raw = generate(cfg)
    assert real not in raw, "a live provider key was written into the run artifact"
    assert [p["id"] for p in yaml.safe_load(raw)["principals"]] == ["team1"]


# --- defence 2: the agent has no route out except the router ---------------

def test_agent_networks_are_all_internal():
    """`internal: true` is what makes direct egress impossible at L3. If an agent
    net were ever made routable, defence 2 would silently evaporate."""
    cfg = load_game_config(CONFIG)
    nets = {n.name: n for n in cfg.topology.networks}
    dev_nodes = {t.dev for t in cfg.topology.teams}

    for node in cfg.topology.nodes:
        if node.id not in dev_nodes:
            continue
        for net_name in node.networks:
            assert nets[net_name].internal, (
                f"{node.id} sits on {net_name}, which is NOT internal — it can route "
                "straight to the upstream and bypass metering")


def test_api_router_is_the_agents_only_path_to_egress():
    """Whatever else joins egress_net must not also be an agent's network peer that
    forwards arbitrary traffic. Pin the bridge set so adding a new dual-homed node
    is a deliberate, reviewed act rather than an accident."""
    cfg = load_game_config(CONFIG)
    egress = {n.name for n in cfg.topology.networks if not n.internal}
    dev_nodes = {t.dev for t in cfg.topology.teams}
    agent_nets = {net for n in cfg.topology.nodes if n.id in dev_nodes for net in n.networks}

    bridges = {
        node.id for node in cfg.topology.nodes
        if (set(node.networks) & egress) and (set(node.networks) & agent_nets)
    }
    # api-router is the metering gateway; the facility containers are on the agent
    # nets too, but they speak fixed protocols (MCP / flag / health), not open proxy.
    assert cfg.api_router.container in bridges
    assert bridges == {"api-router", "mcp-gateway", "flag_service", "health_poller"}, (
        f"a new node bridges agent nets to egress: {bridges}. If it forwards "
        "arbitrary traffic it is an unmetered path to the LLM.")


@pytest.mark.parametrize("host", ["api.anthropic.com", "app-us.ppapi.ai"])
def test_provider_hosts_reachable_through_proxy_are_still_unusable(host):
    """HONEST LIMIT, pinned so nobody mistakes it for a hole that isn't there.

    The forward-proxy firewall's allow list contains the provider hosts, so an agent
    CAN open a connection to them through api-router:3129. That is not a metering
    bypass, because defence 1 leaves it with no key that any provider would accept —
    but it does mean defence 2 is not what stops it. If you want the network path
    closed too, drop these domains from `api_router.firewall.allow.domains`: the
    router's own LLM calls go out directly from the gateway and never consult the
    firewall, so removing them costs the router nothing.
    """
    cfg = _started_game()
    fw = router_from_dict(yaml.safe_load(generate(cfg))).firewall
    assert fw.decide(host).allowed          # the network path is open...

    real_keys = {v for v in next(
        n for n in cfg.topology.nodes if n.id == cfg.api_router.container).env.values() if v}
    for agent in cfg.agents.values():       # ...but nothing to authenticate with
        assert agent.api_key not in real_keys
