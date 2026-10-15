"""Container entry point for the flag-service (B-1).

Run with:  uvicorn flag.service_main:app --factory --host 0.0.0.0 --port 8080

Reads its wiring from the environment (12-factor; the facility token is a
secret injected via env, never baked into the image / committed):

  CYBERARENA_GM_BASE_URL    host-reachable GameMgr base (status + event-ingest)
  CYBERARENA_FACILITY_TOKEN facility credential for the EventSink callback
  CYBERARENA_SSH_KEY_PATH   private key for SSH plant into prod (default /keys/facility)
"""

from __future__ import annotations

import os

from flag.service_app import build_flag_service_app


def app():
    gm = os.environ.get("CYBERARENA_GM_BASE_URL")
    if not gm:
        raise RuntimeError("CYBERARENA_GM_BASE_URL is required")
    return build_flag_service_app(
        gm_base_url=gm,
        facility_token=os.environ.get("CYBERARENA_FACILITY_TOKEN", ""),
        key_path=os.environ.get("CYBERARENA_SSH_KEY_PATH", "/keys/facility"),
    )
