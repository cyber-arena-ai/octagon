"""build_topology namespace overlay (single-host concurrency).

With `--namespace s{k}` the live docker names are prefixed `s{k}-`, while the
config uses bare names. build_topology(ns=...) must strip the prefix so THIS
job's containers match (show live), map their attached nets back to logical
names, and ignore OTHER co-located jobs' containers. At ns="" the bare config
names naturally don't match namespaced docker names (unchanged behavior).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from vis.topology import build_topology

CFG = str(Path(__file__).parent.parent / "config" /
          "game_ico_v3_isolated_async_mixed.yaml")


def _container(name, nets):
    return SimpleNamespace(
        name=name, status="running",
        image=SimpleNamespace(tags=["cyberarena/x:latest"], short_id="sha256:ab"),
        attrs={"NetworkSettings": {"Networks":
               {n: {"IPAddress": "10.1.0.2"} for n in nets}}, "State": {}})


def _net(name):
    return SimpleNamespace(name=name, attrs={"Internal": True})


class _FakeClient:
    def __init__(self, containers, nets):
        self.containers = SimpleNamespace(list=lambda all=False: containers)
        self.networks = SimpleNamespace(list=lambda: nets)


def _node(topo, nid):
    return next(n for n in topo["nodes"] if n["id"] == nid)


def test_namespace_overlay_matches_and_isolates():
    # a batch box running two co-located jobs: this one is slot s2, plus an s0 job.
    client = _FakeClient(
        containers=[
            _container("s2-team1_prod", ["s2-team1_net", "s2-attack_net"]),
            _container("s2-api-router", ["s2-team1_net", "s2-egress_net"]),
            _container("s0-team1_prod", ["s0-team1_net"]),   # ANOTHER job's container
        ],
        nets=[_net("s2-team1_net"), _net("s2-attack_net"), _net("s2-egress_net"),
              _net("s0-team1_net")],
    )
    topo = build_topology(CFG, client=client, ns="s2")

    # this job's containers matched (prefix stripped) → shown live
    assert _node(topo, "team1_prod")["live"] is True
    assert _node(topo, "api-router")["live"] is True
    # attached nets mapped back to logical names (no s2- leaking into the graph)
    membership = _node(topo, "team1_prod")["networks"]
    assert "team1_net" in membership
    assert all(not nn.startswith("s2-") for nn in membership)
    # the OTHER job's container must not leak in as a phantom node
    assert all(n["id"] != "s0-team1_prod" for n in topo["nodes"])


def test_bare_view_does_not_match_namespaced_docker():
    # ns="" against namespaced docker names → no live overlay (bare != prefixed).
    client = _FakeClient(
        containers=[_container("s2-team1_prod", ["s2-team1_net"])],
        nets=[_net("s2-team1_net")],
    )
    topo = build_topology(CFG, client=client, ns="")
    assert _node(topo, "team1_prod")["live"] is False
