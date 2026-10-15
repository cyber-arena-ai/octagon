from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    id: str
    image: str
    networks: list[str]
    ports: list[str] = field(default_factory=list)
    install_list: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # docker --add-host equivalents. Most useful for dev containers that need
    # to reach the host's gameserver API: `{"host.docker.internal": "host-gateway"}`.
    extra_hosts: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Network:
    name: str
    subnet: str
    # When True the network is created with Docker's --internal: no default
    # gateway / no NAT, so containers on it cannot route to the public internet.
    # Use it for agent-side nets where apirouter is the only egress; apirouter
    # itself joins one internal + one default network so it bridges the two.
    internal: bool = False


@dataclass(frozen=True)
class Team:
    id: str
    dev: str
    prod: str
    token: str = ""


@dataclass(frozen=True)
class TopologyGraph:
    teams: list[Team]
    nodes: list[Node]
    networks: list[Network]

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"unknown node id: {node_id!r}")

    def network(self, name: str) -> Network:
        for net in self.networks:
            if net.name == name:
                return net
        raise KeyError(f"unknown network: {name!r}")
