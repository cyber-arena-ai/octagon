"""Per-run namespacing (`--namespace NS`): docker identifiers are prefixed while
the bare node.id stays the in-network DNS alias. NS="" must be byte-identical to
the pre-namespacing behavior (compose list form, unprefixed names)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from models.topology import Network, Node, Team, TopologyGraph
from orchestrator.compose import render_team_compose
from orchestrator.infra.docker import ContainerManager
from orchestrator.infra.network import NetworkManager
from orchestrator.naming import qualify

import yaml


def test_qualify_helper():
    assert qualify("team1_prod", "run7") == "run7-team1_prod"
    assert qualify("team1_prod", "") == "team1_prod"      # back-compat: unchanged


def _graph() -> TopologyGraph:
    return TopologyGraph(
        teams=[Team(id="team1", dev="team1_dev", prod="team1_prod", token="t")],
        nodes=[
            Node(id="team1_dev", image="img", networks=["team1_net", "attack_net"]),
            Node(id="team1_prod", image="img", networks=["team1_net", "attack_net"],
                 ports=["127.0.0.1:8001:4265"]),
        ],
        networks=[
            Network(name="team1_net", subnet="10.1.0.0/24", internal=True),
            Network(name="attack_net", subnet="10.11.0.0/24", internal=True),
        ],
    )


def test_compose_render_ns_qualifies_names_keeps_alias():
    spec = yaml.safe_load(render_team_compose(_graph().teams[0], _graph(), "run7"))
    prod = spec["services"]["team1_prod"]
    assert prod["container_name"] == "run7-team1_prod"   # docker name namespaced
    assert prod["hostname"] == "team1_prod"              # DNS hostname stays node.id
    # networks: mapping form, alias = bare node.id so peers still resolve it
    assert prod["networks"]["team1_net"] == {"aliases": ["team1_prod"]}
    # private net rendered compose-created with the QUALIFIED docker name
    assert spec["networks"]["team1_net"]["name"] == "run7-team1_net"


def test_compose_render_ns_empty_is_list_form_unprefixed():
    spec = yaml.safe_load(render_team_compose(_graph().teams[0], _graph(), ""))
    prod = spec["services"]["team1_prod"]
    assert prod["container_name"] == "team1_prod"
    assert prod["networks"] == ["team1_net", "attack_net"]   # list form, unchanged
    assert spec["networks"]["team1_net"]["name"] == "team1_net"


def test_container_manager_qualifies_and_aliases():
    client = MagicMock()   # get returns a mock container (remove + connect both work)
    cm = ContainerManager(client=client, ns="run7")
    node = Node(id="team1_prod", image="img", networks=["team1_net", "attack_net"])
    cm.create_and_start(node)
    # container created under the qualified name, primary net qualified, alias set
    _, kw = client.containers.create.call_args
    assert kw["name"] == "run7-team1_prod"
    assert kw["hostname"] == "team1_prod"
    assert kw["network"] == "run7-team1_net"
    client.api.create_endpoint_config.assert_called_with(aliases=["team1_prod"])
    # the extra net is connected by qualified name with the bare-id alias
    client.networks.get.assert_called_with("run7-attack_net")
    _, ckw = client.networks.get.return_value.connect.call_args
    assert ckw["aliases"] == ["team1_prod"]


def test_network_manager_qualifies_create_and_remove():
    client = MagicMock()
    client.networks.get.side_effect = __import__("docker").errors.NotFound("x")
    nm = NetworkManager(client=client, graph=_graph(), ns="run7")
    nm.create(Network(name="team1_net", subnet="10.1.0.0/24", internal=True))
    _, kw = client.networks.create.call_args
    assert kw["name"] == "run7-team1_net"
    nm.remove("attack_net")
    client.networks.get.assert_called_with("run7-attack_net")


def test_ns_empty_no_prefix():
    client = MagicMock()
    client.networks.get.side_effect = __import__("docker").errors.NotFound("x")
    NetworkManager(client=client, graph=_graph(), ns="").create(
        Network(name="team1_net", subnet="10.1.0.0/24"))
    _, kw = client.networks.create.call_args
    assert kw["name"] == "team1_net"             # back-compat: no prefix
