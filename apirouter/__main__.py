"""Entry point: ``python -m apirouter``.

Loads the router config (``$APIROUTER_CONFIG`` or ``/etc/apirouter/config.yaml``),
wires the reverse-passthrough metering gateway + the egress firewall forward
proxy onto one asyncio loop, and serves.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import httpx
import uvicorn

from . import config as config_mod
from .gateway import build_app
from .metering import MeteringSink
from .proxy import start_forward_proxy
from .relay import build_relay_app

log = logging.getLogger("apirouter")

DEFAULT_CONFIG = "/data/config.yaml"


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("APIROUTER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg_path = os.environ.get("APIROUTER_CONFIG", DEFAULT_CONFIG)
    cfg = config_mod.load(cfg_path)
    log.info("loaded config from %s; upstreams: %s", cfg_path, ", ".join(
        f"{uid}={u.base_url}{' (default)' if uid == cfg.routes.default else ''}"
        for uid, u in cfg.upstreams.items()))
    for pattern, upstream_id in cfg.routes.by_model:
        log.info("route: model %s -> %s", pattern, upstream_id)

    sink = MeteringSink(jsonl_path=cfg.metering.jsonl_path, stdout=cfg.metering.stdout)
    http = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0), follow_redirects=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        server = await start_forward_proxy(
            cfg.listen.proxy_host, cfg.listen.proxy_port, cfg.firewall, on_event=sink.record
        )
        # Optional gameserver relay: a narrow reverse proxy so an isolated agent
        # can reach MCP/REST through the router. Runs as a sibling uvicorn server
        # on the same loop.
        relay_server = None
        relay_task = None
        if cfg.relay.enabled:
            relay_app = build_relay_app(cfg.relay.base_url)
            relay_server = uvicorn.Server(uvicorn.Config(
                relay_app, host=cfg.relay.host, port=cfg.relay.port, log_level="info"))
            relay_task = asyncio.create_task(relay_server.serve())
            log.info("relay on %s:%d -> %s", cfg.relay.host, cfg.relay.port, cfg.relay.base_url)
        try:
            yield
        finally:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
            if relay_server is not None:
                relay_server.should_exit = True
                with contextlib.suppress(Exception):
                    await relay_task
            with contextlib.suppress(Exception):
                await http.aclose()
            sink.close()

    app = build_app(cfg, sink=sink, client=http, lifespan=lifespan)

    log.info(
        "gateway on %s:%d  |  forward proxy on %s:%d",
        cfg.listen.gateway_host, cfg.listen.gateway_port,
        cfg.listen.proxy_host, cfg.listen.proxy_port,
    )
    uvicorn.run(app, host=cfg.listen.gateway_host, port=cfg.listen.gateway_port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
