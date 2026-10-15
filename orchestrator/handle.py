"""ContainerHandle — opaque pointer to a running orchestrator container.

The agents layer + flag/health facilities pass ContainerHandle around
to identify which container a command should target. Decoupled from the
legacy Service ABC (deleted in the challenge-interface-v2 cleanup) so
the dataclass can be imported without dragging in the docker SDK.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContainerHandle:
    container_id: str
    name: str
    networks: list[str]
