"""DEFCON: NetworkManager handles multiple cross-team nets.

Pre-DEFCON only one cross-team net existed (`game_net`, for prod↔prod).
With the per-pair attack networks, each team's dev gets its own
`attack_net_<team>` containing only the opposing prod. NetworkManager
must toggle all of them together when phases flip.

This test exercises the membership construction + name predicate
without touching docker.
"""

from __future__ import annotations

import pytest

from models.topology import Network, Node, Team, TopologyGraph
from orchestrator.infra.network import NetworkManager, _is_cross_team


def test_is_cross_team_predicate():
    assert _is_cross_team("game_net") is True
    assert _is_cross_team("attack_net_team1") is True
    assert _is_cross_team("attack_net_red") is True
    # Per-team nets are NOT cross-team — they stay attached always.
    assert _is_cross_team("team1_net") is False
    assert _is_cross_team("team2_net") is False
    assert _is_cross_team("random_net") is False


def _node(id: str, *nets: str) -> Node:
    return Node(id=id, image="x", networks=list(nets), ports=[], install_list=[])


def test_membership_built_from_graph_isolates_per_net():
    """Each cross-team net's membership = exactly the nodes declared on it."""
    graph = TopologyGraph(
        teams=[
            Team(id="team1", dev="team1_dev", prod="team1_prod", token="t1"),
            Team(id="team2", dev="team2_dev", prod="team2_prod", token="t2"),
        ],
        nodes=[
            _node("team1_dev", "team1_net", "attack_net_team1"),
            _node("team1_prod", "team1_net", "game_net", "attack_net_team2"),
            _node("team2_dev", "team2_net", "attack_net_team2"),
            _node("team2_prod", "team2_net", "game_net", "attack_net_team1"),
        ],
        networks=[],
    )
    # docker client isn't touched in this test — we just inspect membership.
    mgr = NetworkManager(client=_NullClient(), graph=graph)

    # game_net = both prods.
    assert sorted(mgr._cross_team_membership["game_net"]) == ["team1_prod", "team2_prod"]
    # attack_net_team1 = team1's dev attacking team2's prod.
    assert sorted(mgr._cross_team_membership["attack_net_team1"]) == ["team1_dev", "team2_prod"]
    # attack_net_team2 = team2's dev attacking team1's prod.
    assert sorted(mgr._cross_team_membership["attack_net_team2"]) == ["team1_prod", "team2_dev"]
    # No per-team nets in cross_team_membership.
    assert "team1_net" not in mgr._cross_team_membership
    assert "team2_net" not in mgr._cross_team_membership


def test_dev_containers_dont_share_a_cross_team_net():
    """Pinned invariant: under the per-pair design, team1_dev and team2_dev
    must NEVER appear on the same cross-team net — that's the whole point
    of `attack_net_team1` vs `attack_net_team2`. Catching this in CI prevents
    a future copy-paste from breaking the dev↔dev isolation guarantee."""
    graph = TopologyGraph(
        teams=[
            Team(id="team1", dev="team1_dev", prod="team1_prod", token="t1"),
            Team(id="team2", dev="team2_dev", prod="team2_prod", token="t2"),
        ],
        nodes=[
            _node("team1_dev", "team1_net", "attack_net_team1"),
            _node("team1_prod", "team1_net", "game_net", "attack_net_team2"),
            _node("team2_dev", "team2_net", "attack_net_team2"),
            _node("team2_prod", "team2_net", "game_net", "attack_net_team1"),
        ],
        networks=[],
    )
    mgr = NetworkManager(client=_NullClient(), graph=graph)

    dev_names = {"team1_dev", "team2_dev"}
    for net_name, members in mgr._cross_team_membership.items():
        devs_on_net = dev_names & set(members)
        assert len(devs_on_net) <= 1, (
            f"both team devs share cross-team net {net_name!r}: {devs_on_net} — "
            f"this breaks the dev↔dev isolation guarantee."
        )


class _NullClient:
    """Stub docker client — NetworkManager.__init__ only stores it; tests
    that don't call set_cross_team_network can pass any object."""

    class _Networks:
        def get(self, name): raise NotImplementedError
    networks = _Networks()
