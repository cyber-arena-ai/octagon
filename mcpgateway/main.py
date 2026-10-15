"""Container entry point for the mcp-gateway.

Run with:  uvicorn mcpgateway.main:app --factory --host 0.0.0.0 --port 8080
Agents point their mcp_endpoint at  http://<gateway>:8080/mcp/

Wiring via env:
  CYBERARENA_GM_BASE_URL        host-reachable GameMgr base (status/attacks/report/restart)
  CYBERARENA_FLAG_SERVICE_URL   flag-service base (submit goes here directly)
  CYBERARENA_MCP_ALLOWED_HOSTS  comma-sep Host patterns agents use to reach the
                                gateway (e.g. "mcp-gateway:*"); REQUIRED in-net or
                                the MCP handshake 421s on the container-name Host.
"""

from __future__ import annotations

import os

from mcpgateway.app import build_gateway_app


def app():
    gm = os.environ.get("CYBERARENA_GM_BASE_URL")
    fs = os.environ.get("CYBERARENA_FLAG_SERVICE_URL")
    if not gm:
        raise RuntimeError("CYBERARENA_GM_BASE_URL is required")
    if not fs:
        raise RuntimeError("CYBERARENA_FLAG_SERVICE_URL is required")
    allowed = tuple(h.strip() for h in
                    os.environ.get("CYBERARENA_MCP_ALLOWED_HOSTS", "").split(",")
                    if h.strip())
    return build_gateway_app(gm_base_url=gm, flag_service_url=fs, allowed_hosts=allowed)
