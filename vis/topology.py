"""Realtime network/topology visualizer (standalone, read-only).

Overlays LIVE docker state onto the config's INTENDED topology and serves it as
a self-updating HTML graph: one box per docker network, container nodes inside,
edges for multi-homed nodes, each node labeled with image + port→app. Decoupled
from the game — run it anytime (during setup, mid-match, debugging teardown):

    python -m vis.topology -c config/game_ico_v3_isolated_async_mixed.yaml
    # → http://localhost:8050   (needs docker access, e.g. via `sg docker -c`)

Hybrid data (the chosen mode): nodes/networks in the config but not yet (or no
longer) running still appear, greyed, so you can watch bring-up/teardown. With
no --config it degrades to live-docker-only (arena nets detected by name).
"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import time
from pathlib import Path
from typing import Any

import docker
from docker.errors import DockerException

from orchestrator import topology

log = logging.getLogger("vis.topology")

HTML_PATH = Path(__file__).parent / "topology.html"

# Best-effort port → app hint. The image name is the primary identity; this just
# annotates "what's listening here". Keep in sync with the configs' port usage.
PORT_APP = {
    22: "ssh",
    3128: "egress-proxy",
    4265: "ico-service",
    8000: "gamemgr-api",
    8050: "topology-vis",
    8080: "llm-gateway / mcp",
    9001: "flag-service",
    9201: "health-poller",
}

# Live docker networks that aren't arena nets — never show these.
_DOCKER_BUILTIN_NETS = {"bridge", "host", "none"}


def _config_ports(specs: list[str]) -> list[dict[str, Any]]:
    """Parse config port specs ('127.0.0.1:8001:4265' / '8001:80') -> port rows."""
    out: list[dict[str, Any]] = []
    for spec in specs:
        parts = spec.split(":")
        if len(parts) == 2:
            host_bind, container = parts[0], parts[1]
        elif len(parts) == 3:
            ip, host, container = parts
            host_bind = f"{ip}:{host}"
        else:
            continue
        cport = int(container)
        out.append({"container": cport, "host": host_bind, "app": PORT_APP.get(cport)})
    return out


def _live_ports(attrs: dict) -> list[dict[str, Any]]:
    """Exposed/published ports from a live container's attrs (host=None if unpublished)."""
    out: list[dict[str, Any]] = []
    ports = (attrs.get("NetworkSettings") or {}).get("Ports") or {}
    for cport_proto, binds in sorted(ports.items()):
        cport = int(cport_proto.split("/")[0])
        host = None
        if binds:
            b = binds[0]
            host = ":".join(p for p in (b.get("HostIp"), b.get("HostPort")) if p)
        out.append({"container": cport, "host": host, "app": PORT_APP.get(cport)})
    return out


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _live_subnet(net) -> str | None:
    if net is None:
        return None
    cfgs = ((net.attrs.get("IPAM") or {}).get("Config")) or []
    return cfgs[0].get("Subnet") if cfgs else None


def build_topology(config_path: str | None = None, client: Any = None,
                   ns: str = "") -> dict[str, Any]:
    """Hybrid topology: config intent overlaid with live docker state.

    `ns` is the run's docker namespace (orchestrator `--namespace`). Live docker
    names are prefixed `{ns}-` (see orchestrator.naming.qualify) while the config
    uses bare names, so with `ns` set we consider ONLY this namespace's resources
    and strip the prefix back to the logical name before matching — giving an
    exact overlay for one job even when several run co-located on one host. Other
    namespaces' (and unrelated) containers are excluded. `ns=""` is unchanged."""
    result: dict[str, Any] = {
        "generated_at": time.time(),
        "config": config_path,
        "docker_error": None,
        "networks": [],
        "nodes": [],
        "edges": [],
        "hosts": [],        # host-side processes (not containers) — e.g. GameMgr on :8000
        "host_edges": [],   # container -> host links
    }

    intended_nodes = {}
    intended_nets = {}
    if config_path:
        graph = topology.load(config_path)
        intended_nodes = {n.id: n for n in graph.nodes}
        intended_nets = {net.name: net for net in graph.networks}

    def _logical(name: str) -> str | None:
        """Live docker name → logical (config) name for this namespace. ns="" →
        identity. ns set → only names prefixed "{ns}-" (this job's), stripped to
        the bare name; anything else returns None (excluded from the overlay)."""
        if not ns:
            return name
        pfx = f"{ns}-"
        return name[len(pfx):] if name.startswith(pfx) else None

    live_containers: dict[str, Any] = {}
    live_nets: dict[str, Any] = {}
    try:
        client = client or docker.from_env()
        for c in client.containers.list(all=True):
            lname = _logical(c.name)
            if lname is not None:
                live_containers[lname] = c
        for n in client.networks.list():
            lname = _logical(n.name)
            if lname is not None:
                live_nets[lname] = n
    except DockerException as e:
        result["docker_error"] = str(e)

    # Relevant network names: intended nets + nets intended nodes attach to. With
    # no config, fall back to every non-builtin live net (best-effort arena view).
    net_names: set[str] = set(intended_nets)
    for n in intended_nodes.values():
        net_names.update(n.networks)
    if not config_path:
        net_names = {name for name in live_nets if name not in _DOCKER_BUILTIN_NETS}

    net_internal: dict[str, bool] = {}
    for name in sorted(net_names):
        net = intended_nets.get(name)
        live = live_nets.get(name)
        internal = bool(net.internal) if net else bool((live.attrs.get("Internal") if live else False))
        net_internal[name] = internal
        result["networks"].append({
            "name": name,
            "subnet": net.subnet if net else _live_subnet(live),
            "internal": internal,
            "present": live is not None,
            "intended": net is not None,
        })

    # Node set: intended nodes + any live container attached to a relevant net.
    node_ids: set[str] = set(intended_nodes)
    for name, c in live_containers.items():
        attached = set((c.attrs.get("NetworkSettings") or {}).get("Networks") or {})
        if name in intended_nodes or (attached & net_names):
            node_ids.add(name)

    for nid in sorted(node_ids):
        node = intended_nodes.get(nid)
        c = live_containers.get(nid)
        membership = list(node.networks) if node else []
        ips: dict[str, str | None] = {}
        status, health = "absent", None
        image = node.image if node else "?"
        ports = _config_ports(node.ports) if node else []

        if c is not None:
            attrs = c.attrs
            raw_cnets = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
            # map attached-network names back to logical (strip the ns prefix) so
            # membership/edges/ips line up with the config's bare net names
            cnets = {ln: nd for rn, nd in raw_cnets.items()
                     if (ln := _logical(rn)) is not None}
            membership = sorted(set(membership) | set(cnets))
            ips = {nn: (nd.get("IPAddress") or None) for nn, nd in cnets.items()}
            status = c.status
            health = ((attrs.get("State") or {}).get("Health") or {}).get("Status")
            tags = c.image.tags
            image = tags[0] if tags else (node.image if node else c.image.short_id)
            live_ports = _live_ports(attrs)
            if live_ports:
                ports = live_ports

        membership = [nn for nn in membership if nn in net_names] or membership
        result["nodes"].append({
            "id": nid,
            "image": image,
            "networks": membership,
            "ips": ips,
            "status": status,
            "health": health,
            "ports": ports,
            "intended": node is not None,
            "live": c is not None,
        })
        for nn in membership:
            if nn in net_names:
                result["edges"].append({
                    "node": nid,
                    "network": nn,
                    "ip": ips.get(nn),
                    "internal": net_internal.get(nn, False),
                })

    # GameMgr runs as a HOST process (REST + panel + MCP backend), reached by
    # containers via host.docker.internal:<port> (CYBERARENA_GM_BASE_URL). It is
    # not a container, so synthesize a host node + edges from whoever references it.
    gm_refs: list[str] = []
    gm_port: int | None = None
    for nid, node in intended_nodes.items():
        base = (node.env or {}).get("CYBERARENA_GM_BASE_URL", "")
        if "host.docker.internal" in base:
            gm_refs.append(nid)
            m = re.search(r"host\.docker\.internal:(\d+)", base)
            if m:
                gm_port = int(m.group(1))
    if gm_refs:
        port = gm_port or 8000
        up = _port_open("127.0.0.1", port)
        result["hosts"].append({
            "id": "gamemgr",
            "detail": "GameMgr · REST + panel + MCP backend",
            "port": port,
            "status": "running" if up else "absent",
            "live": up,
        })
        # The host IS the gateway of the NAT'd (non-internal) network — containers
        # reach it over that gateway, not over the internal nets. So attach the host
        # to the egress network(s) rather than drawing a line to each caller; the
        # callers already show edges to that same net.
        egress = [n["name"] for n in result["networks"] if not n["internal"]]
        if egress:
            for en in egress:
                result["host_edges"].append({"host": "gamemgr", "network": en, "port": port})
        else:  # no gatewayed net (e.g. no config) — fall back to direct caller links
            for nid in sorted(gm_refs):
                result["host_edges"].append({"node": nid, "host": "gamemgr", "port": port})

    return result


def write_snapshot(config_path: str | None, out_path: Path) -> Path:
    """Render a self-contained topology.html (data inlined) — no server needed.

    Inlines the current topology as `window.__SNAPSHOT__` into the template; the
    page renders it once and skips polling. Good as a forensic run artifact.
    """
    import json

    topo = build_topology(config_path)
    template = HTML_PATH.read_text()
    inline = f"<script>window.__SNAPSHOT__ = {json.dumps(topo)};</script>"
    html = template.replace("<!-- SNAPSHOT_DATA -->", inline, 1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    return out_path


def _latest_run_dir() -> Path | None:
    runs = Path("runs")
    if not runs.is_dir():
        return None
    dirs = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.name)
    return dirs[-1] if dirs else None


def build_app(config_path: str | None = None):
    """FastAPI app: `/` serves the renderer, `/api/topology` the live JSON."""
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, JSONResponse

    app = FastAPI(title="cyber-arena topology")

    @app.get("/")
    async def index():
        return FileResponse(HTML_PATH, media_type="text/html")

    @app.get("/api/topology")
    async def api_topology():
        return JSONResponse(build_topology(config_path))

    return app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vis.topology", description=__doc__)
    p.add_argument("--config", "-c", default=None, help="game yaml for the intended topology overlay")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8050)
    p.add_argument("--once", action="store_true",
                   help="write a self-contained static topology.html and exit (no server)")
    p.add_argument("--out", default=None,
                   help="--once output path (default: runs/<latest>/topology.html, else ./topology.html)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        out = Path(args.out) if args.out else ((_latest_run_dir() or Path(".")) / "topology.html")
        write_snapshot(args.config, out)
        log.info("wrote static snapshot -> %s", out)
        return 0

    import uvicorn

    log.info("topology vis on http://%s:%d  (config=%s)", args.host, args.port, args.config)
    uvicorn.run(build_app(args.config), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
