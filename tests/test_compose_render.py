"""Unit tests for per-team compose rendering (no docker required).

`render_team_compose` is a pure function over the topology, so these run
anywhere. They guard the invariants the runtime depends on: container names
pinned to node ids, the team's networks declared external, and no secrets /
unexpanded ${VAR} placeholders leaking into a file we write to disk.
"""

from __future__ import annotations

import os

import yaml

# The v3 config references these; setdefault so the test loads it with or
# without a .env present (mirrors conftest's API_ROUTER_KEY handling).
for _k in ("CYBERARENA_FACILITY_TOKEN", "ANTHROPIC_API_KEY_TEAM1", "CODEX_API_KEY_TEAM2"):
    os.environ.setdefault(_k, "test-placeholder")

from gamemgr.config import load as load_game_config  # noqa: E402
from orchestrator.compose import (  # noqa: E402
    classify_nets,
    render_team_compose,
    team_node_ids,
)

CONFIG = "config/game_ico_v3_isolated_async_mixed.yaml"


def _render_team1():
    graph = load_game_config(CONFIG).topology
    team1 = next(t for t in graph.teams if t.id == "team1")
    nodes_by_id = {n.id: n for n in graph.nodes}
    nodes = [nodes_by_id[team1.dev], nodes_by_id[team1.prod]]
    return team1, nodes, yaml.safe_load(render_team_compose(team1, graph))


def test_team_node_ids_excludes_infra():
    graph = load_game_config(CONFIG).topology
    ids = team_node_ids(graph)
    assert ids == {"team1_dev", "team1_prod", "team2_dev", "team2_prod"}
    # infra nodes are NOT team nodes
    assert "api-router" not in ids
    assert "mcp-gateway" not in ids
    assert "flag_service" not in ids
    assert "health_poller" not in ids


def test_services_pin_names_to_node_ids():
    _team1, nodes, spec = _render_team1()
    assert set(spec["services"]) == {"team1_dev", "team1_prod"}
    for node in nodes:
        svc = spec["services"][node.id]
        # the load-bearing invariant: name == hostname == node.id
        assert svc["container_name"] == node.id
        assert svc["hostname"] == node.id
        assert svc["image"] == node.image
        assert svc["networks"] == list(node.networks)
        assert svc["pull_policy"] == "never"


def test_team_net_compose_created_others_external():
    _team1, _nodes, spec = _render_team1()
    # team1's dev+prod touch only team1_net (private) + attack_net (shared).
    assert set(spec["networks"]) == {"team1_net", "attack_net"}
    # team1_net is team-private -> compose-created (no `external`), internal, with subnet
    team1_net = spec["networks"]["team1_net"]
    assert "external" not in team1_net
    assert team1_net["internal"] is True
    assert team1_net["name"] == "team1_net"
    assert team1_net["ipam"]["config"][0]["subnet"] == "10.1.0.0/24"
    # attack_net is shared -> external
    assert spec["networks"]["attack_net"]["external"] is True
    assert spec["networks"]["attack_net"]["name"] == "attack_net"


def test_classify_nets():
    graph = load_game_config(CONFIG).topology
    shared, private = classify_nets(graph)
    # team nets are private to their team
    assert private == {"team1_net": "team1", "team2_net": "team2"}
    # everything else is shared — the single attack_net + egress_net
    assert {"attack_net", "egress_net"} <= shared
    assert "team1_net" not in shared and "team2_net" not in shared
    # ALL four facility services are infra members of team1_net (deferred-connect set)
    members = {n.id for n in graph.nodes if "team1_net" in n.networks}
    infra = members - team_node_ids(graph)
    assert infra == {"api-router", "mcp-gateway", "flag_service", "health_poller"}


def test_prod_ports_passed_through():
    _team1, _nodes, spec = _render_team1()
    assert spec["services"]["team1_prod"]["ports"] == ["127.0.0.1:8001:4265"]


def test_no_unexpanded_placeholders_or_secrets():
    graph = load_game_config(CONFIG).topology
    team1, nodes, _spec = _render_team1()
    rendered = render_team_compose(team1, graph)
    # ${VAR} placeholders must already be expanded by the loader; and since
    # team env is non-secret, the literal '$' escaping shouldn't introduce one.
    assert "${" not in rendered
    # team_dev only carries proxy env — assert it's present and intact
    dev_env = _spec_env(nodes)
    assert dev_env.get("HTTPS_PROXY") == "http://api-router:3128"


def _spec_env(nodes):
    for n in nodes:
        if n.id.endswith("_dev"):
            return n.env
    return {}
