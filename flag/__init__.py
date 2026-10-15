"""Flag facility.

Two pieces:
  * `FlagStore` — in-memory state (planted flags by (team, service);
    inverted index by flag string; per-submitter accepted-flag set).
    Owns the OWN/DUPLICATE/UNKNOWN/ACCEPTED decision logic.
  * `FlagServer` — orchestration over the store. Plants flags into
    challenge containers (SERVICE_PROTOCOL via adapter, FILE_AT_PATH
    via docker exec), runs per-round rotation, verifies submissions
    and records FLAG_PLANTED / FLAG_SUBMITTED via its FacilityContext
    event sink.

GameMgr keeps the public surface (`plant_initial_flags`, `plant_flags`,
`submit_flag`, `flags` property) but delegates all logic to FlagServer.
Pluggable per `Challenge.flag_handler` — no demo service is hardcoded
into the facility.
"""

from .server import FlagServer
from .store import (
    FLAG_BODY_ALPHABET,
    FLAG_BODY_LEN,
    FlagStore,
    SubmitResult,
    new_flag,
)

__all__ = [
    "FlagServer",
    "FlagStore",
    "SubmitResult",
    "new_flag",
    "FLAG_BODY_LEN",
    "FLAG_BODY_ALPHABET",
]
