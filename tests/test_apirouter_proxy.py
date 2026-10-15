"""apirouter.proxy — forward proxy gated by the firewall.

Spins up in-process stub servers (an HTTP origin + a TCP echo) and the proxy,
all on one asyncio loop, then drives them with httpx (plain HTTP) and a raw
socket (CONNECT).
"""

import asyncio

import httpx

from apirouter.firewall import Firewall
from apirouter.proxy import start_forward_proxy


async def _origin(reader, writer):
    await reader.readuntil(b"\r\n\r\n")
    body = b"OK-ORIGIN"
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\nConnection: close\r\n\r\n" + body)
    await writer.drain()
    writer.close()


async def _echo(reader, writer):
    data = await reader.read(100)
    writer.write(data)
    await writer.drain()
    writer.close()


async def _serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _start_proxy(firewall, events):
    proxy = await start_forward_proxy("127.0.0.1", 0, firewall, on_event=events.append)
    return proxy, proxy.sockets[0].getsockname()[1]


def test_plain_http_allowed():
    async def go():
        origin, oport = await _serve(_origin)
        events = []
        fw = Firewall(default="deny", allow_domains=["127.0.0.1"])
        proxy, pport = await _start_proxy(fw, events)
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{pport}") as c:
            r = await c.get(f"http://127.0.0.1:{oport}/")
        proxy.close(); origin.close()
        return r, events

    r, events = asyncio.run(go())
    assert r.status_code == 200 and r.text == "OK-ORIGIN"
    assert events and events[0]["allowed"] is True


def test_plain_http_denied():
    async def go():
        origin, oport = await _serve(_origin)
        events = []
        fw = Firewall(default="deny")  # nothing allowed
        proxy, pport = await _start_proxy(fw, events)
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{pport}") as c:
            r = await c.get(f"http://127.0.0.1:{oport}/")
        proxy.close(); origin.close()
        return r, events

    r, events = asyncio.run(go())
    assert r.status_code == 403
    assert events and events[0]["allowed"] is False


def test_connect_denied():
    async def go():
        events = []
        fw = Firewall(default="deny")
        proxy, pport = await _start_proxy(fw, events)
        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        writer.write(b"CONNECT blocked.test:443 HTTP/1.1\r\nHost: blocked.test:443\r\n\r\n")
        await writer.drain()
        status = await reader.readline()
        writer.close()
        proxy.close()
        return status, events

    status, events = asyncio.run(go())
    assert b"403" in status
    assert events and events[0]["allowed"] is False and events[0]["method"] == "CONNECT"


def test_connect_allowed_tunnel():
    async def go():
        echo, eport = await _serve(_echo)
        events = []
        fw = Firewall(default="deny", allow_domains=["127.0.0.1"])
        proxy, pport = await _start_proxy(fw, events)
        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        writer.write(f"CONNECT 127.0.0.1:{eport} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        status = await reader.readline()        # "HTTP/1.1 200 Connection Established\r\n"
        await reader.readline()                 # the blank line terminating the head
        # now the tunnel is raw — talk to the echo server through it
        writer.write(b"hi-tunnel")
        await writer.drain()
        echoed = await reader.read(9)
        writer.close()
        proxy.close(); echo.close()
        return status, echoed, events

    status, echoed, events = asyncio.run(go())
    assert b"200" in status
    assert echoed == b"hi-tunnel"
    assert events and events[0]["allowed"] is True
