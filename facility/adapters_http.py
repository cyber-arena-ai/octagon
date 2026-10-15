"""HTTP port adapters — the remote wiring a CONTAINERIZED facility uses.

Mirror the Local* adapters (adapters_local.py) signature-for-signature, but talk
to GameMgr over HTTP instead of in-process, so the same facility code runs
unchanged whether co-located (Local*) or in its own container (Http*). Sync
(like Local*), backed by an httpx.Client.

  HttpStatusPort  → GET  {base}/game/status
  HttpEventSink   → POST {base}/facility/events   (idempotent; facility token)
  HttpExecPort    → POST {base}/facility/exec      (B-0b)
  HttpAuthoritativeStatePort → POST {base}/facility/state/read

Secrets: the facility token is passed in (read from env by the caller) — never
hard-coded here, never logged. Stdlib + httpx only; no gamemgr/flag/health
imports, so the boundary stays acyclic.
"""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

import httpx

from challenges.interface import StateReadResult, StateReadStatus

log = logging.getLogger(__name__)

FACILITY_TOKEN_HEADER = "X-Facility-Token"


class HttpStatusPort:
    """StatusPort over HTTP — `status()` reads GameMgr's `/game/status`."""

    def __init__(self, base_url: str, *, client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._http = client or httpx.Client(timeout=10.0)

    def status(self) -> dict:
        """Read GameMgr's `/game/status`. On ANY HTTP failure — including a slow
        `/game/status` exceeding the timeout — log and return {} rather than
        raise, symmetric with `HttpEventSink.record`. Rationale: a submit must
        still validate + record the capture (and a health probe must still run)
        even when status is briefly unavailable; re-raising here turned a slow
        status into a 500 that silently dropped valid flag submissions (#38).
        Callers already treat status as best-effort (`st.get(...)` with defaults)."""
        try:
            r = self._http.get(f"{self._base}/game/status")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.warning("status fetch failed, treating as unknown: %r", e)
            return {}


class HttpEventSink:
    """EventSink over HTTP — POSTs each event to GameMgr's idempotent ingest.

    Mints a per-event `event_id` so at-least-once delivery is safe: the host
    dedups on it and orders by its own ingest sequence. An ingest failure is
    logged and dropped — a telemetry event must never crash the facility.
    """

    def __init__(self, base_url: str, token: str, *, client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._http = client or httpx.Client(timeout=10.0)

    def record(
        self,
        type_: str,
        *,
        team_id: str | None = None,
        round_id: int = 0,
        phase: str = "",
        payload: dict | None = None,
    ) -> Any:
        event_id = uuid.uuid4().hex[:12]
        # EventType is `str, Enum` (not StrEnum): str() gives "EventType.X", so
        # serialize the underlying value ("X"). Plain strings pass through.
        etype = getattr(type_, "value", type_)
        body = {
            "event_id": event_id,
            "type": str(etype),
            "team_id": team_id,
            "round_id": round_id,
            "phase": phase,
            "payload": payload or {},
        }
        try:
            r = self._http.post(
                f"{self._base}/facility/events",
                json=body,
                headers={FACILITY_TOKEN_HEADER: self._token},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("event-ingest failed (%s): %r", type_, e)
            return None
        return {"id": event_id}


class HttpAuthoritativeStatePort:
    """AuthoritativeStatePort over the facility-authenticated host API.

    Fail-closed like the rest of the boundary: any transport or decode problem
    becomes an ERROR result, never an exception and never an empty-but-OK read —
    an unreadable region must not be mistaken for an absent attacker mark.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._http = client or httpx.Client(timeout=10.0)

    def read(self, team_id: str, service: str, region: str) -> StateReadResult:
        try:
            response = self._http.post(
                f"{self._base}/facility/state/read",
                json={"team_id": team_id, "service": service, "region": region},
                headers={FACILITY_TOKEN_HEADER: self._token},
            )
            response.raise_for_status()
            body = response.json()
            status = StateReadStatus(body.get("status"))
            if status is not StateReadStatus.OK:
                return StateReadResult(
                    StateReadStatus.ERROR,
                    detail=str(body.get("detail") or "authoritative state unreadable"),
                )
            encoded = body.get("files")
            if not isinstance(encoded, dict):
                raise ValueError("files is not an object")
            files = {
                str(name): base64.b64decode(value, validate=True)
                for name, value in encoded.items()
                if isinstance(name, str) and isinstance(value, str)
            }
            if len(files) != len(encoded):
                raise ValueError("invalid file entry")
            return StateReadResult(StateReadStatus.OK, files=files)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            log.warning(
                "authoritative-state read failed for %s/%s/%s: %r",
                team_id, service, region, exc,
            )
            return StateReadResult(
                StateReadStatus.ERROR, detail="authoritative state unavailable"
            )
