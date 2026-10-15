"""Health-check facility.

Two pieces:
  * `HealthChecker` — thin wrapper that runs `challenge.functionality_test`
    and emits one SERVICE_HEALTH_CHECK per CheckResult leaf. Stateless.
  * `HealthPoller` — periodic scheduler. Driven by GameMgr at phase
    boundaries; does not subscribe to anything. Exposes `trigger_now`
    for the restart-post one-shot probe.

The package is independent of `gamemgr` internals — it consumes
`Challenge` instances + a `FacilityContext` (events + status ports) and
emits events back. Pluggable per the v2 challenge ABC (no demo-service
specifics hardcoded).
"""

from .checker import HealthChecker
from .poller import HealthPoller, PollTarget

__all__ = ["HealthChecker", "HealthPoller", "PollTarget"]
