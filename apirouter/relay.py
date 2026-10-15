"""Narrow reverse-proxy relay to ONE upstream (the host gameserver).

Lets an isolated agent — on an `internal` network with no host route — reach the
gameserver's MCP/REST through the in-network api-router. Unlike the metering
gateway (gateway.py), this is a plain verbatim forward to a single configured
upstream: no metering, no firewall, no compat rewrites, no auth injection.
Client headers (incl. `X-Team-Token`) pass through unchanged.

It forwards ONLY to `relay.base_url` (e.g. the host GameMgr at
host.docker.internal:8000) — it is deliberately NOT a general host gateway: the
path is appended to the one fixed base, so it can't be used to reach an arbitrary
host:port.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

log = logging.getLogger("apirouter.relay")

# Hop-by-hop / encoding headers we must not forward verbatim.
_HOP = {
    "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "accept-encoding",
}
_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]


def build_relay_app(
    base_url: str,
    *,
    client: httpx.AsyncClient | None = None,
    lifespan=None,
) -> FastAPI:
    """A FastAPI app that reverse-proxies every request to `base_url + <path>`.

    Streams `text/event-stream` bodies through unbuffered (MCP streamable HTTP),
    buffers everything else. Transparent to redirects (passes them to the
    client) and to auth headers.
    """
    app = FastAPI(title="apirouter-relay", lifespan=lifespan)
    base = base_url.rstrip("/")
    # Own client (follow_redirects=False → transparent); not the gateway's.
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0))

    def _fwd_headers(request: Request) -> dict[str, str]:
        h = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        # Force identity so we can pass the raw body straight through without
        # touching content-encoding (httpx would otherwise request gzip).
        h["accept-encoding"] = "identity"
        return h

    def _resp_headers(resp: httpx.Response) -> dict[str, str]:
        return {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "relay_upstream": base}

    @app.api_route("/{path:path}", methods=_METHODS)
    async def relay(path: str, request: Request):
        url = f"{base}/{path}"
        body = await request.body()
        req = http.build_request(
            request.method, url, params=request.query_params,
            headers=_fwd_headers(request), content=body,
        )
        try:
            up = await http.send(req, stream=True)
        except httpx.HTTPError as e:
            log.warning("relay upstream failed %s %s: %r", request.method, url, e)
            return Response(content=f"relay upstream error: {e}".encode()[:500],
                            status_code=502)

        ct = up.headers.get("content-type", "")
        headers = _resp_headers(up)

        if "text/event-stream" in ct:
            async def gen():
                try:
                    async for chunk in up.aiter_raw():
                        yield chunk
                finally:
                    await up.aclose()
            return StreamingResponse(gen(), status_code=up.status_code,
                                     headers=headers, media_type=ct or None)

        raw = await up.aread()
        await up.aclose()
        return Response(content=raw, status_code=up.status_code,
                        headers=headers, media_type=ct or None)

    return app
