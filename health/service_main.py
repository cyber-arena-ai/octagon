"""Container entry point for the health-poller (B-2.2).

Run with:  uvicorn health.service_main:app --factory --host 0.0.0.0 --port 8080

Wiring via env (the facility token is a secret injected via env, never baked in):

  CYBERARENA_GM_BASE_URL    host-reachable GameMgr base (status + event-ingest)
  CYBERARENA_FACILITY_TOKEN facility credential for the host callbacks
  CYBERARENA_SSH_KEY_PATH   private key for SSH probe into prod (default /keys/facility)
"""

from __future__ import annotations

import os

from health.service_app import build_health_poller_app


def app():
    gm = os.environ.get("CYBERARENA_GM_BASE_URL")
    if not gm:
        raise RuntimeError("CYBERARENA_GM_BASE_URL is required")
    return build_health_poller_app(
        gm_base_url=gm,
        facility_token=os.environ.get("CYBERARENA_FACILITY_TOKEN", ""),
        key_path=os.environ.get("CYBERARENA_SSH_KEY_PATH", "/keys/facility"),
    )
