"""apirouter — self-contained egress gateway + LLM metering proxy.

Two listeners in one process (see docs/api_router_plan_v1.md):

  * LLM metering gateway (FastAPI) — a verbatim reverse passthrough: forwards
    each request unchanged to the configured upstream and returns its response,
    repairing markdown-wrapped JSON the proxy sometimes returns and recording
    per-request token / cost metadata as JSONL. No format translation.
  * Egress firewall (forward proxy) — allow/denies all container outbound
    HTTP/HTTPS by domain glob + CIDR.

Nothing in this package imports the arena orchestrator (gamemgr / orchestrator /
models). It ships its own Dockerfile + requirements and runs standalone.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
