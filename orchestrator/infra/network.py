from __future__ import annotations

import logging

import docker
from docker.errors import NotFound

from models.topology import Network, TopologyGraph
from orchestrator.naming import qualify

log = logging.getLogger(__name__)

GAME_NET = "game_net"
# Nets that are toggled per phase (ATTACK opens / DEFENSE closes). Any
# network whose name is or starts with one of these prefixes is treated as
# cross-team. game_net carries prod ↔ prod traffic; an attack net carries
# attacker dev → opposing prod traffic. The prefix matches both per-team
# `attack_net_<team>` nets and a single shared `attack_net` (all team
# containers on one net — dev ↔ dev then allowed).
_CROSS_TEAM_NET_PREFIXES = ("game_net", "attack_net")


def _is_cross_team(net_name: str) -> bool:
    return any(net_name == p or net_name.startswith(p) for p in _CROSS_TEAM_NET_PREFIXES)


class NetworkManager:
    """Manages docker networks for the arena.

    Owns the cross-team network switch (attach/detach for every
    cross-team net listed below) per phase. Backend is docker network
    attach/detach for MVP; an iptables backend (Linux optimization, no
    reconnect interruption) is a future extension sharing this interface.
    """

    def __init__(
        self,
        client: docker.DockerClient | None = None,
        graph: TopologyGraph | None = None,
        ns: str = "",
    ) -> None:
        self._client = client or docker.from_env()
        self.ns = ns
        # Per-network membership: cross_team net name → list of node ids
        # that should be attached when that net is open. Built from the
        # static topology so phase switching is purely a state-restore
        # operation against a known target set.
        self._cross_team_membership: dict[str, list[str]] = {}
        if graph is not None:
            for node in graph.nodes:
                for net_name in node.networks:
                    if _is_cross_team(net_name):
                        self._cross_team_membership.setdefault(net_name, []).append(node.id)

    def _q(self, name: str) -> str:
        return qualify(name, self.ns)

    def create(self, network: Network) -> str:
        """Create a docker network. Idempotent — returns existing id if present.
        `network.name` is logical; the docker name is namespaced."""
        dname = self._q(network.name)
        try:
            existing = self._client.networks.get(dname)
            log.info("network %s already exists (%s)", dname, existing.short_id)
            return existing.id
        except NotFound:
            pass

        ipam_pool = docker.types.IPAMPool(subnet=network.subnet)
        ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
        created = self._client.networks.create(
            name=dname,
            driver="bridge",
            ipam=ipam_config,
            internal=network.internal,
            check_duplicate=True,
        )
        log.info("created network %s (%s, %s%s)", dname, created.short_id,
                 network.subnet, " internal" if network.internal else "")
        return created.id

    def remove(self, name: str) -> None:
        """Remove a docker network. Idempotent. `name` is logical."""
        dname = self._q(name)
        try:
            net = self._client.networks.get(dname)
        except NotFound:
            log.info("network %s already gone", dname)
            return
        net.remove()
        log.info("removed network %s", dname)

    def set_cross_team_network(self, allow: bool) -> None:
        """Open or close every cross-team network for the registered nodes.

        DEFENSE (allow=False): disconnect every (container, cross_team_net)
        pair so the agent can't reach anything off-team while patching.
        ATTACK  (allow=True):  reconnect each one to its declared nets.

        Idempotent: skips containers already in the target state. May
        reset existing TCP connections — see m3_network_plan.md.

        Iterates `_cross_team_membership` so this works for `game_net`
        (prod↔prod), any number of per-team `attack_net_<team>` networks, or a
        single shared `attack_net` carrying every team container.
        """
        for net_name, members in self._cross_team_membership.items():
            dnet = self._q(net_name)
            try:
                net = self._client.networks.get(dnet)
            except NotFound:
                log.warning("set_cross_team_network: %s not found", dnet)
                continue
            for name in members:
                try:
                    container = self._client.containers.get(self._q(name))
                except NotFound:
                    log.warning("set_cross_team_network: container %s not found; skip", self._q(name))
                    continue
                container.reload()
                attached = dnet in container.attrs["NetworkSettings"]["Networks"]
                if allow and not attached:
                    # keep node.id as the alias so cross-team DNS survives the reconnect
                    net.connect(container, aliases=[name] if self.ns else None)
                    log.info("connected %s to %s", self._q(name), dnet)
                elif not allow and attached:
                    # force=True asks docker to drop the endpoint even if
                    # in-flight TCP state is messy. Doesn't actively kill
                    # established TCP connections — kernel sockets can
                    # linger until conntrack times out — but is strictly
                    # more aggressive than the default. Per the DEFCON
                    # live-run finding: existing TCP attack connections
                    # may still drain a bit, but new connections fail
                    # immediately, which is the design goal here.
                    net.disconnect(container, force=True)
                    log.info("disconnected %s from %s (force)", self._q(name), dnet)