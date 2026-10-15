"""One-command end-to-end self-test for apirouter.

Spins up a built-in mock upstream + a real `python -m apirouter` process on
ephemeral ports, exercises every feature, and prints a ✓/✗ checklist. Needs no
real API key, no external network, no manual config.

    .venv/bin/python apirouter/selftest.py        # run from the repo root

Exits 0 if everything passes, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# Mock upstream: OpenAI chat response with usage, wrapped in a ```json fence to
# prove the router repairs it. 10 input + 4 output tokens per call.
_RESP = {"id": "cc", "model": "deepseek-v4-flash",
         "choices": [{"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 4}}


def _mock_upstream(port: int) -> HTTPServer:
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            body = ("```json\n" + json.dumps(_RESP) + "\n```").encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    gw, px, up = _free_port(), _free_port(), _free_port()
    base = f"http://127.0.0.1:{gw}"
    results: list[tuple[bool, str]] = []

    def check(ok: bool, msg: str):
        results.append((ok, msg))
        print(f"  {'✓' if ok else '✗'} {msg}")

    srv = _mock_upstream(up)
    tmp = tempfile.mkdtemp(prefix="apirouter-selftest-")
    cfg = f"""
listen: {{ gateway: "127.0.0.1:{gw}", proxy: "127.0.0.1:{px}" }}
upstream: {{ base_url: "http://127.0.0.1:{up}", api_key: "REAL-UPSTREAM-KEY" }}
principals:
  - {{ id: alice, token: "alice-token", budget_usd: 10 }}
  - {{ id: bob,   token: "bob-token" }}
pricing: {{ deepseek-v4-flash: {{ input: 1000000, output: 1000000 }} }}
compat: {{ drop_empty_content: true, drop_params: ["reasoning_effort"], retry_on_5xx: 2, max_request_bytes: 2000 }}
firewall: {{ default: deny, allow: {{ domains: ["127.0.0.1"] }} }}
metering: {{ jsonl_path: "usage.jsonl", stdout: false }}
"""
    with open(os.path.join(tmp, "config.yaml"), "w") as f:
        f.write(cfg)

    env = {**os.environ, "APIROUTER_CONFIG": os.path.join(tmp, "config.yaml")}
    proc = subprocess.Popen([sys.executable, "-m", "apirouter"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import httpx

        # wait for the gateway
        up_ok = False
        for _ in range(100):
            try:
                if urllib.request.urlopen(base + "/healthz", timeout=1).status == 200:
                    up_ok = True
                    break
            except Exception:
                time.sleep(0.2)
        check(up_ok, "gateway boots, /healthz ok")
        if not up_ok:
            return 1

        def call(tok: str) -> httpx.Response:
            return httpx.post(base + "/v1/chat/completions",
                              headers={"authorization": f"Bearer {tok}"},
                              json={"model": "deepseek-v4-flash",
                                    "messages": [{"role": "user", "content": "ping"}]})

        # 1. passthrough + markdown repair (mock returns ```json-wrapped)
        r = call("alice-token")
        check(r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "pong",
              "passthrough returns clean JSON (markdown fence repaired)")

        # 2. /usage attributes spend to alice (10 in + 4 out, $14 at $1/token)
        rep = {x["principal"]: x for x in httpx.get(base + "/usage").json()["principals"]}
        check(rep["alice"]["calls"] == 1 and rep["alice"]["input_tokens"] == 10
              and rep["alice"]["cost_usd"] == 14.0, "/usage shows alice's tokens + cost")

        # 3. bob (no cap) is unlimited
        check(call("bob-token").status_code == 200, "uncapped principal (bob) allowed")

        # 4. budget: alice's 2nd call crosses the $10 cap → 402
        check(call("alice-token").status_code == 402, "over-budget request rejected (402)")

        # 5. unknown token → 401
        check(call("ghost").status_code == 401, "unknown token rejected (401)")

        # 6. firewall: CONNECT to a non-allowed host via the proxy → 403
        with socket.create_connection(("127.0.0.1", px), timeout=3) as s:
            s.sendall(b"CONNECT blocked.example:443 HTTP/1.1\r\nHost: blocked.example\r\n\r\n")
            status = s.recv(64)
        check(b"403" in status, "egress firewall denies non-allowed host (403)")

        # 7. compat: oversize request body rejected up front (413)
        big = httpx.post(base + "/v1/chat/completions",
                         headers={"authorization": "Bearer bob-token"},
                         json={"model": "deepseek-v4-flash",
                               "messages": [{"role": "user", "content": "x" * 4000}]})
        check(big.status_code == 413, "compat: oversize body rejected (413)")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        srv.shutdown()

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
