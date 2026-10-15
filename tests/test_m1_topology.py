"""M1 integration test: full setup -> verify -> teardown cycle.

Requires Docker daemon running and base images built
(scripts/build_images.sh).
"""

from __future__ import annotations

from pathlib import Path

import docker
import pytest

from orchestrator import topology
from orchestrator.orchestrator import Orchestrator

CONFIG = Path(__file__).parent.parent / "config" / "legacy" / "game.yaml"


@pytest.fixture(scope="module")
def client():
    try:
        c = docker.from_env()
        c.ping()
    except Exception as e:
        pytest.skip(f"docker daemon unavailable: {e}")
    return c


@pytest.fixture(scope="module")
def graph():
    return topology.load(CONFIG)


def test_topology_parse(graph):
    assert len(graph.teams) == 2
    assert len(graph.nodes) == 4
    assert len(graph.networks) == 3
    assert {n.id for n in graph.nodes} == {"team1_dev", "team1_prod", "team2_dev", "team2_prod"}
    assert {n.name for n in graph.networks} == {"team1_net", "team2_net", "game_net"}


def test_setup_then_teardown(client, graph):
    orch = Orchestrator(graph=graph)
    try:
        orch.setup()

        for node in graph.nodes:
            c = client.containers.get(node.id)
            c.reload()
            assert c.status == "running", f"{node.id} not running: {c.status}"

        for net in graph.networks:
            client.networks.get(net.name)

        team1_prod = client.containers.get("team1_prod")
        attached = set(team1_prod.attrs["NetworkSettings"]["Networks"].keys())
        assert {"team1_net", "game_net"}.issubset(attached)

        rc, out = orch.containers.exec("team1_dev", ["which", "nmap"])
        assert rc == 0
        assert "/nmap" in out

    finally:
        orch.teardown()

    for node in graph.nodes:
        with pytest.raises(docker.errors.NotFound):
            client.containers.get(node.id)
    for net in graph.networks:
        with pytest.raises(docker.errors.NotFound):
            client.networks.get(net.name)