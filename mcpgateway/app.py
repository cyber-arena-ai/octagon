"""mcp-gateway ASGI app — FastMCP (cyberarena tools, HTTP-backed) behind the
same `/mcp/` mount + lifespan the host app uses, so the agent's mcp_endpoint
just points here instead of at the api-router relay."""

from __future__ import annotations

import contextlib

import httpx
from fastapi import FastAPI, Header, Request, Response

from gamemgr.mcp_server import (
    MCP_MOUNT_PATH,
    MCP_PUBLIC_ENDPOINT,
    build_mcp_app_with_backend,
)

from .backend import HttpBackend


def build_gateway_app(
    *,
    gm_base_url: str,
    flag_service_url: str,
    allowed_hosts: tuple[str, ...] = (),
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    backend = HttpBackend(gm_base_url, flag_service_url, client=client)
    # Agents reach the gateway by its container name (e.g. mcp-gateway:8080);
    # that Host must be in the DNS-rebinding allow-list or the MCP handshake 421s.
    mcp_server, mcp_asgi = build_mcp_app_with_backend(
        backend, extra_allowed_hosts=allowed_hosts)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(title="mcp-gateway", lifespan=lifespan)
    app.mount(MCP_MOUNT_PATH, mcp_asgi)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "facility": "mcp-gateway",
                "endpoint": MCP_PUBLIC_ENDPOINT}

    # Solo-DEFENSE verifier relay. The `script` verifier runs on an internal
    # team net and can't reach GameMgr's host API directly — but it CAN reach the
    # gateway (its mcp_endpoint host). This plain relay (NOT an MCP schema tool, so
    # the real defender never sees it) forwards the verifier's report + token to
    # GameMgr and returns GameMgr's reply verbatim — including `stop: true` on the
    # verified-fix latch, so the runner exits before its next attempt. GameMgr
    # 409s the relay outside solo-defense.
    @app.post("/verify/attempt")
    async def verify_attempt(
        request: Request, x_team_token: str | None = Header(default=None)
    ):
        body = await request.body()
        gm = gm_base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if x_team_token:
            headers["X-Team-Token"] = x_team_token
        owns_client = client is None
        http = client or httpx.AsyncClient()
        try:
            r = await http.post(
                f"{gm}/verify/attempt", content=body, headers=headers, timeout=15.0)
        finally:
            if owns_client:
                await http.aclose()
        return Response(
            content=r.content, status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    return app
