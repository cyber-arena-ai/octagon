"""RemoteHealthPollerHandle — host-side proxy for a CONTAINERIZED HealthPoller.

Duck-types the slice of `HealthPoller` that GameMgr drives (`on_attack_enter`,
`on_attack_exit`, `trigger_now`, `stop`), forwarding each to the poller
container's control endpoints over HTTP. So GameMgr's phase callbacks
(enter_attack_phase / enter_defense_phase / _run_restart_job) are unchanged —
they call the same method names whether the poller is in-process or remote.

Forwarding of the RUNTIME control calls (attack-enter/exit, trigger-now, stop) is
best-effort: a failed call is logged, never raised into the round loop — a
transient blip must not fail a live match, and a missed probe window surfaces as
telemetry, not a verdict. The one-time startup `configure()` is the exception: it
is fail-loud (retried, then raised), because an unconfigured poller runs zero
probes and the match must not proceed to a fake `succeeded` with no service
telemetry (Finding 2). Sync httpx (the calls are tiny); the async methods just
block briefly on a localhost-class POST, mirroring GameMgr's flag-service forward.
"""

from __future__ import annotations

import logging
import time

import httpx

from facility.adapters_http import FACILITY_TOKEN_HEADER

log = logging.getLogger("health.handle")


class HealthPollerConfigureError(RuntimeError):
    """Raised when the one-time `/poller/configure` can't be pushed after retries
    — the container didn't come up (connection error) or rejected configuration
    (e.g. a load-induced 500 during a simultaneous cold start). A fail-loud
    condition: an unconfigured poller runs zero probes, so the match must end
    rather than report a fake `succeeded` with `probes:0` / `uptime_pct:null`
    (Finding 2)."""


class RemoteHealthPollerHandle:
    def __init__(
        self,
        base_url: str,
        facility_token: str,
        target_specs: list[dict],
        hosts: dict[str, str],
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = facility_token
        self._targets = target_specs  # [{team_id, service, interval_secs}]
        self._hosts = hosts           # team_id -> prod host (SSH targets)
        self._http = client or httpx.Client(timeout=10.0)

    # Startup configure is retried before it fails loud: the 500s that motivated
    # this were load-induced during a simultaneous cold start (Finding 1), so a
    # couple of short-backoff retries absorb the transient without masking a
    # genuinely dead poller.
    _CONFIGURE_RETRIES = 3
    _CONFIGURE_BACKOFF_SECS = 0.5

    @property
    def name(self) -> str:
        return "health"

    def _post(self, path: str, body: dict | None = None) -> None:
        """Best-effort RUNTIME control call: a failure is logged, never raised (a
        transient blip must not fail a live match). NOT used for configure()."""
        try:
            r = self._http.post(f"{self._base}{path}", json=body or {},
                                headers={FACILITY_TOKEN_HEADER: self._token})
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("health-poller control %s failed: %r", path, e)

    def configure(self) -> None:
        """Push the SSH host map + per-target cadence so the container can build
        its SSH-backed probe targets. Called once at startup.

        Fail-loud: retried, then raised as HealthPollerConfigureError so an
        unconfigured poller (container down, or a load-induced 500) ends the
        match instead of silently running zero probes (Finding 2)."""
        body = {"hosts": self._hosts, "targets": self._targets}
        last: Exception | None = None
        for attempt in range(self._CONFIGURE_RETRIES):
            try:
                r = self._http.post(f"{self._base}/poller/configure", json=body,
                                    headers={FACILITY_TOKEN_HEADER: self._token})
                r.raise_for_status()
                return
            except httpx.HTTPError as e:
                last = e
                log.warning("health-poller configure attempt %d/%d failed: %r",
                            attempt + 1, self._CONFIGURE_RETRIES, e)
                if attempt + 1 < self._CONFIGURE_RETRIES:
                    time.sleep(self._CONFIGURE_BACKOFF_SECS * (2 ** attempt))
        raise HealthPollerConfigureError(
            f"/poller/configure failed after {self._CONFIGURE_RETRIES} attempts: {last!r}")

    def on_attack_enter(self, round_id: int) -> list:
        self._post("/poller/attack-enter", {"round_id": round_id})
        return []  # immediate-probe tasks live in the container, not here

    def on_attack_exit(self) -> None:
        self._post("/poller/attack-exit")

    async def trigger_now(
        self, team_id: str, service: str, *, trigger: str = "restart"
    ) -> None:
        self._post("/poller/trigger-now",
                   {"team_id": team_id, "service": service, "trigger": trigger})

    async def stop(self) -> None:
        self._post("/poller/attack-exit")
