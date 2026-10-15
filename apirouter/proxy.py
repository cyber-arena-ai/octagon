"""Forward proxy with egress firewall.

A minimal HTTP/HTTPS forward proxy (the target of ``HTTP_PROXY`` /
``HTTPS_PROXY``). Every connection is gated by the :class:`~apirouter.firewall.Firewall`:

  * ``CONNECT host:port`` (HTTPS) — filtered by the CONNECT host (no TLS
    interception); allowed → a raw TCP tunnel is spliced both ways.
  * absolute-form ``GET http://host/path`` (plain HTTP) — filtered by host;
    allowed → the request is forwarded origin-form and the response relayed.

Denied requests get a ``403`` and an ``egress`` deny record via ``on_event``.
Hostnames are resolved to an IP (best effort) so CIDR rules apply too.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable
from urllib.parse import urlsplit

from .firewall import Firewall

log = logging.getLogger("apirouter.proxy")

OnEvent = Callable[[dict[str, Any]], None]

_MAX_HEAD = 64 * 1024


async def _resolve_ip(host: str) -> str | None:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        return infos[0][4][0] if infos else None
    except Exception:  # noqa: BLE001 - DNS failure is not fatal to the decision
        return None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def _deny(writer: asyncio.StreamWriter, reason: str) -> None:
    body = f"egress denied: {reason}".encode()
    writer.write(
        b"HTTP/1.1 403 Forbidden\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )


class ForwardProxy:
    def __init__(self, firewall: Firewall, on_event: OnEvent | None = None) -> None:
        self.firewall = firewall
        self.on_event = on_event or (lambda e: None)

    def _emit(self, **fields: Any) -> None:
        try:
            self.on_event({"event": "egress", **fields})
        except Exception:  # noqa: BLE001 - logging must never break a request
            pass

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
            writer.close()
            return

        lines = head.split(b"\r\n")
        try:
            method, target, version = lines[0].decode("latin-1").split(" ", 2)
        except ValueError:
            _deny(writer, "malformed request line")
            await _safe_drain(writer)
            writer.close()
            return

        if method.upper() == "CONNECT":
            await self._handle_connect(target, reader, writer)
        else:
            await self._handle_plain(method, target, version, lines, reader, writer)

    async def _gate(self, host: str, writer: asyncio.StreamWriter, **meta: Any) -> bool:
        ip = await _resolve_ip(host)
        decision = self.firewall.decide(host, ip)
        self._emit(host=host, ip=ip, allowed=decision.allowed, reason=decision.reason, **meta)
        if not decision.allowed:
            _deny(writer, decision.reason)
            await _safe_drain(writer)
            writer.close()
        return decision.allowed

    async def _handle_connect(
        self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        host, _, port_s = target.rpartition(":")
        host = host or target
        try:
            port = int(port_s) if port_s else 443
        except ValueError:
            port = 443

        if not await self._gate(host, writer, method="CONNECT", port=port):
            return

        try:
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except OSError as e:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await _safe_drain(writer)
            writer.close()
            log.info("CONNECT %s:%s failed: %r", host, port, e)
            return

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await _safe_drain(writer)
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))

    async def _handle_plain(
        self,
        method: str,
        target: str,
        version: str,
        lines: list[bytes],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        u = urlsplit(target)
        host = u.hostname
        if not host:
            _deny(writer, "non-absolute request URI")
            await _safe_drain(writer)
            writer.close()
            return
        port = u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        if not await self._gate(host, writer, method=method.upper(), port=port):
            return

        # Rebuild the header block origin-form; drop proxy-only headers.
        header_lines = []
        content_length = 0
        for raw in lines[1:]:
            if not raw:
                continue
            name = raw.split(b":", 1)[0].strip().lower()
            if name.startswith(b"proxy-"):
                continue
            if name == b"content-length":
                try:
                    content_length = int(raw.split(b":", 1)[1].strip())
                except ValueError:
                    content_length = 0
            header_lines.append(raw)

        try:
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except OSError as e:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await _safe_drain(writer)
            writer.close()
            log.info("forward %s %s:%s failed: %r", method, host, port, e)
            return

        request = f"{method} {path} {version}\r\n".encode("latin-1")
        request += b"\r\n".join(header_lines) + b"\r\n\r\n"
        up_writer.write(request)
        if content_length > 0:
            body = await reader.readexactly(content_length)
            up_writer.write(body)
        await _safe_drain(up_writer)

        await _pipe(up_reader, writer)
        try:
            up_writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _safe_drain(writer: asyncio.StreamWriter) -> None:
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def start_forward_proxy(
    host: str, port: int, firewall: Firewall, on_event: OnEvent | None = None
) -> asyncio.Server:
    """Start the forward proxy and return the running server (caller closes it)."""
    proxy = ForwardProxy(firewall, on_event)
    server = await asyncio.start_server(proxy.handle, host, port)
    log.info("forward proxy listening on %s:%s", host, port)
    return server
