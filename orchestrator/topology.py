from __future__ import annotations

from pathlib import Path

import yaml

from models.topology import Network, Node, Team, TopologyGraph


def load(path: str | Path) -> TopologyGraph:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return _from_dict(raw)


def _from_dict(raw: dict) -> TopologyGraph:
    teams = [Team(id=t["id"], dev=t["dev"], prod=t["prod"]) for t in raw.get("teams", [])]
    nodes = [
        Node(
            id=n["id"],
            image=n["image"],
            networks=list(n.get("networks", [])),
            ports=list(n.get("ports", [])),
            install_list=list(n.get("install_list", [])),
            env=dict(n.get("env", {})),
        )
        for n in raw.get("nodes", [])
    ]
    networks = [
        Network(name=name, subnet=cfg["subnet"], internal=bool(cfg.get("internal", False)))
        for name, cfg in raw.get("networks", {}).items()
    ]
    return TopologyGraph(teams=teams, nodes=nodes, networks=networks)