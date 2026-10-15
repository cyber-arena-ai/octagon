"""Facility ABC + FacilityContext.

`FacilityContext` is the environment bundle a facility receives: the
narrow ports (`ports.py`) it may use. `Facility` is a deliberately thin
base — identity plus optional async lifecycle hooks. It does NOT force a
daemon shape: a facility with no background loop (FlagServer) overrides
nothing; one with periodic tasks (HealthPoller) overrides `stop`.

Why thin: the value of this abstraction is the *boundary* (the ports),
not a uniform lifecycle. Different facilities are driven differently by
GameMgr (FlagServer per phase via `rotate`, HealthPoller per phase via
`on_attack_enter`), and we don't want the ABC to flatten that.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .ports import EventSink, ExecPort, StatusPort


@dataclass(frozen=True)
class FacilityContext:
    """What a facility is allowed to touch on its host.

    `exec` is optional: facilities that never run in-container commands (their
    targets carry the exec callable directly) leave it unset.

    An `AuthoritativeStatePort` is deliberately NOT bundled here. It is reached
    only through a per-target `read_authoritative_state` closure the flag
    service builds (see `flag.service_app`), so a region read is always scoped
    to the (team, service) it was resolved for and no facility can address one
    freely off its context.
    """

    status: StatusPort
    events: EventSink
    exec: ExecPort | None = None


class Facility(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier (e.g. "flag", "health"). Used for logs and,
        once containerized, to stamp the `source` of ingested events."""
        ...

    async def start(self) -> None:
        """Optional. Default no-op — most facilities are driven by
        GameMgr phase callbacks rather than a self-owned loop."""
        return None

    async def stop(self) -> None:
        """Optional. Default no-op. Override to cancel background work."""
        return None
