"""Per-team docker compose bring-up.

Each team (`<team>_dev` + `<team>_prod`) is brought up as its own
`docker compose` project, rendered at runtime from the same `TopologyGraph`
that drives the SDK path. The shared infra nodes (api-router, mcp-gateway,
flag-service, health-poller) and every network stay SDK-managed — see
`Orchestrator.setup()` and docs/UML/orchestrator.md.

The compose services MUST keep the same identity the rest of the system relies
on (round loop, flag planting, per-phase network switch all look up containers /
networks by exact name): `container_name`/`hostname` are pinned to `node.id`,
networks are referenced as `external: true` so the SDK-created nets are reused.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import yaml

from models.topology import Node, Team, TopologyGraph
from orchestrator.naming import qualify

log = logging.getLogger(__name__)


def team_node_ids(graph: TopologyGraph) -> set[str]:
    """Node ids that belong to a team (vs shared infra)."""
    ids: set[str] = set()
    for t in graph.teams:
        ids.add(t.dev)
        ids.add(t.prod)
    return ids


def classify_nets(graph: TopologyGraph) -> tuple[set[str], dict[str, str]]:
    """Partition networks into shared vs team-private.

    A net is **team-private** to team T if at least one of T's dev/prod is a
    member and no *other* team's dev/prod is. Such a net is created by T's own
    compose project; its non-team (infra) members are the containers the
    orchestrator connects in *after* compose makes the net. Everything else is
    **shared** (spans ≥2 teams, or is infra-only) and stays SDK-created.

    Returns ``(shared, private)`` where ``private`` maps net name -> owning team id
    and ``shared`` is every other net declared in the graph.
    """
    members: dict[str, set[str]] = {}
    for node in graph.nodes:
        for net in node.networks:
            members.setdefault(net, set()).add(node.id)

    team_of: dict[str, str] = {}
    for t in graph.teams:
        team_of[t.dev] = t.id
        team_of[t.prod] = t.id

    private: dict[str, str] = {}
    for net, ids in members.items():
        owners = {team_of[i] for i in ids if i in team_of}
        if len(owners) == 1:
            private[net] = next(iter(owners))

    shared = {net.name for net in graph.networks if net.name not in private}
    # nets that appear only via node membership (not in graph.networks) still
    # need classifying for rendering; default any non-private to shared.
    shared |= {net for net in members if net not in private}
    return shared, private


def _service(node: Node, ns: str = "") -> dict:
    """Render one Node into a compose service definition.

    NS≠"": the docker `container_name` is namespaced, but `node.id` is kept as the
    per-network DNS **alias** (mapping form) so in-network resolution is unchanged.
    NS="": `container_name`=`node.id` + list-form networks — byte-identical to before.
    """
    svc: dict = {
        "image": node.image,
        # Pin the name + hostname so exec-by-name / network-toggle-by-name (the
        # whole runtime) finds this container exactly as the SDK path would.
        "container_name": qualify(node.id, ns),
        "hostname": node.id,
        # cyberarena/* images are local-only (never pushed to a registry) and
        # are pre-built before bring-up. `never` matches the SDK path's
        # containers.create semantics (fail fast if absent, no registry pull).
        "pull_policy": "never",
        "restart": "no",
    }
    if node.networks:
        svc["networks"] = (
            {net: {"aliases": [node.id]} for net in node.networks} if ns
            else list(node.networks))
    if node.ports:
        # config port specs ("127.0.0.1:8001:4265", "8001:80") are valid
        # compose short syntax verbatim.
        svc["ports"] = list(node.ports)
    if node.env:
        # node.env is already ${VAR}-expanded by load_game_config, so values are
        # literal. Escape any '$' as '$$' so compose's own interpolation leaves
        # them untouched (team env has none today; defensive).
        svc["environment"] = {k: v.replace("$", "$$") for k, v in node.env.items()}
    if node.extra_hosts:
        svc["extra_hosts"] = [f"{h}:{ip}" for h, ip in node.extra_hosts.items()]
    return svc


def render_team_compose(team: Team, graph: TopologyGraph, ns: str = "") -> str:
    """Render a team's (dev + prod) nodes into a compose-file YAML string.

    A network the team touches is rendered **compose-created** (driver + subnet +
    internal flag) if it is *this team's* private net — so the team's own compose
    project owns its `team_net` lifecycle. Every other (shared) net is declared
    `external: true`: the SDK creates those before compose runs and compose just
    attaches to them by name. `name:` is always set explicitly so compose never
    prefixes the project name onto the real docker network name.
    """
    _shared, private = classify_nets(graph)
    nodes = [graph.node(team.dev), graph.node(team.prod)]
    services = {node.id: _service(node, ns) for node in nodes}

    net_names: list[str] = []
    for node in nodes:
        for net in node.networks:
            if net not in net_names:
                net_names.append(net)

    # Top-level network keys stay LOGICAL (services reference them by logical
    # name); only the real docker `name:` is namespaced.
    networks: dict[str, dict] = {}
    for name in net_names:
        if private.get(name) == team.id:
            net = graph.network(name)
            cfg: dict = {"driver": "bridge", "name": qualify(name, ns)}
            if net.internal:
                cfg["internal"] = True
            if net.subnet:
                cfg["ipam"] = {"config": [{"subnet": net.subnet}]}
            networks[name] = cfg
        else:
            networks[name] = {"external": True, "name": qualify(name, ns)}

    spec = {"services": services, "networks": networks}
    header = f"# Generated for team {team.id} by orchestrator.compose — do not edit.\n"
    return header + yaml.safe_dump(spec, sort_keys=False)


class ComposeManager:
    """Brings a team up via `docker compose`. Shells out (mirrors how
    challenges.nautilus_ico.build shells out to `docker build`) rather than using
    the SDK, so we get the compose plugin's behavior verbatim."""

    def up(self, project: str, compose_file: Path) -> None:
        """`docker compose -f <file> -p <project> up -d`."""
        log.info("compose up: project=%s file=%s", project, compose_file)
        try:
            subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "-p", project, "up", "-d"],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"docker compose up failed for project {project}\n"
                f"stdout:\n{e.stdout}\nstderr:\n{e.stderr}"
            ) from e
        log.info("compose up complete: project=%s", project)
