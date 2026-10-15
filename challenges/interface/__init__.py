"""Abstract challenge interface.

Defines `Challenge` + supporting types. No imports from the rest of the
codebase — challenge authors can implement against this package alone.
Concrete challenges live under `challenges/<name>/`.
"""

from __future__ import annotations

from .challenge import (
    MAX_STATE_REGION_BYTES,
    Challenge,
    ChallengeDocs,
    ExecFn,
    RestartScriptInitialStart,
    StateRegionSpec,
    VulboxImage,
    validate_state_region_specs,
)
from .probe import (
    DEFAULT_STORE,
    BaselinePrincipal,
    CheckResult,
    Flag,
    FlagHandler,
    FlagObservation,
    FlagStoreSpec,
    FunctionalityTest,
    HealthLevel,
    ObservationStatus,
    OracleKind,
    RepairResult,
    RestartHandler,
    SingleFlagHandler,
    StateReadResult,
    StateReadStatus,
    VulboxProbe,
    VulboxTarget,
    exec_read_observe,
    read_authoritative_state,
    state_scan_observe,
    validate_flag_store_specs,
    validate_flag_stores,
)

__all__ = [
    "Challenge",
    "ChallengeDocs",
    "ExecFn",
    "MAX_STATE_REGION_BYTES",
    "RestartScriptInitialStart",
    "StateRegionSpec",
    "validate_state_region_specs",
    "VulboxImage",
    "VulboxProbe",
    "VulboxTarget",
    "FunctionalityTest",
    "FlagHandler",
    "SingleFlagHandler",
    "Flag",
    "BaselinePrincipal",
    "FlagStoreSpec",
    "OracleKind",
    "DEFAULT_STORE",
    "validate_flag_stores",
    "validate_flag_store_specs",
    "RestartHandler",
    "CheckResult",
    "HealthLevel",
    "ObservationStatus",
    "FlagObservation",
    "RepairResult",
    "StateReadStatus",
    "StateReadResult",
    "exec_read_observe",
    "read_authoritative_state",
    "state_scan_observe",
]
