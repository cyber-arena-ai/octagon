"""Facility abstraction.

A facility (FlagServer, HealthPoller, …) is a unit of game-server work
that talks to its host (GameMgr) only through narrow ports — status,
events, and exec — bundled in a `FacilityContext`. Authoritative state is
instead scoped through a per-target closure. In v3 facilities run in their OWN
containers against the HTTP adapters (`adapters_http`) + SSH exec
(`adapters_ssh`); GameMgr configures + signals them. The `Local*` adapters
survive as lightweight in-memory test doubles for unit-testing the facility
classes, and `LocalExecPort` also backs GameMgr's own host-side docker-exec
(deploy / restart / target_for). See
`docs/UML/facility.md` + `docs/archived/facility_direct_exec_plan_v3.md`.
"""

from .base import Facility, FacilityContext
from .adapters_local import LocalEventSink, LocalExecPort, LocalStatusPort
from .ports import (
    AuthoritativeStatePort,
    EventSink,
    ExecPort,
    ExecResult,
    StatusPort,
)

__all__ = [
    "Facility",
    "FacilityContext",
    "StatusPort",
    "EventSink",
    "ExecPort",
    "ExecResult",
    "AuthoritativeStatePort",
    "LocalStatusPort",
    "LocalEventSink",
    "LocalExecPort",
]
