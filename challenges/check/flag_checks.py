"""Deterministic proof inputs, mark probes, and handle validation for the
verifier phases."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from challenges.interface import (
    Challenge,
    Flag,
    FlagHandler,
    ObservationStatus,
    OracleKind,
    StateReadStatus,
    VulboxTarget,
    validate_flag_stores,
)
from flag.store import FLAG_BODY_ALPHABET, FLAG_BODY_LEN, new_mark

CHECK_ROUND_CONTEXT_SEED = "00" * 32


def _check_flag_value(store: str, index: int) -> str:
    seed = f"{index}:{store}".encode()
    raw = hashlib.shake_256(seed).digest(FLAG_BODY_LEN)
    body = "".join(FLAG_BODY_ALPHABET[b % len(FLAG_BODY_ALPHABET)] for b in raw)
    return f"FLAG{{{body}}}"


def make_check_flags(challenge: Challenge) -> dict[str, Flag]:
    """Deterministic values for the verifier's plant -> retrieve probe.

    EVERY declared store gets one, whatever its kind: the live game plants all of
    them through the same call, and for a WRITE store the value is the facility
    integrity token whose anchor `retrieve()` must read back."""
    stores = [s.name for s in challenge.flag_store_specs()]
    validate_flag_stores(stores, challenge=challenge.name)
    return {
        store: Flag(_check_flag_value(store, index))
        for index, store in enumerate(stores)
    }


def make_check_marks(challenge: Challenge) -> dict[str, str]:
    """Fresh, deliberately UNPLACED marks for each WRITE store, so the verifier
    can prove the claim half of `retrieve()` reports absence conclusively."""
    specs = challenge.flag_store_specs()
    validate_flag_stores([s.name for s in specs], challenge=challenge.name)
    return {s.name: new_mark() for s in specs if s.kind is OracleKind.WRITE}


# The claim half of `retrieve()` must report a mark nobody placed CONCLUSIVELY
# absent. Both statuses qualify: NOT_FOUND (region read clean) and MISMATCH (the
# region held some other value) — the framework credits neither, and maps both to
# NOT_OBSERVED. AUTH_REJECTED / ERROR mean "could not tell", which would hand the
# attacker an endless retry instead of an answer.
_ABSENT_STATUSES = (ObservationStatus.NOT_FOUND, ObservationStatus.MISMATCH)


def check_marks_absent(
    handler: FlagHandler,
    target: VulboxTarget,
    handles: Mapping[str, str],
    marks: Mapping[str, str],
    *,
    require_authoritative_read: bool = False,
) -> tuple[bool, str]:
    """Require each fresh, unplaced verifier mark to read back as conclusively
    absent (NOT_FOUND or MISMATCH).

    A WRITE handler that returns PRESENT for a mark nobody placed would credit
    every attacker; one that returns ERROR unconditionally would credit none.
    Both are caught here, before a live match. When ``require_authoritative_read``
    is set, the claim cannot execute inside prod and must successfully use the
    defender-independent state reader."""
    failures = []
    for store, mark in marks.items():
        claim_target = target
        successful_reads: set[str] = set()
        if require_authoritative_read:
            reader = target.meta.get("read_authoritative_state")

            def exec_forbidden(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
                raise RuntimeError("exec_in_container disabled for WRITE claim check")

            def tracked_read(region: str) -> Any:
                if reader is None:
                    raise RuntimeError("no authoritative-state reader")
                result = reader(region)
                if getattr(result, "status", None) is StateReadStatus.OK:
                    successful_reads.add(region)
                return result

            claim_target = VulboxTarget(
                host=target.host,
                ports=dict(target.ports),
                meta={
                    **target.meta,
                    "exec_in_container": exec_forbidden,
                    "read_authoritative_state": tracked_read,
                },
            )
        try:
            obs = handler.retrieve(claim_target, handles[store], mark)
            if obs.status not in _ABSENT_STATUSES:
                failures.append(
                    f"{store}: {obs.status.value} ({obs.detail or 'no detail'})")
            if require_authoritative_read and not successful_reads:
                failures.append(f"{store}: did not read authoritative state")
        except Exception as e:
            failures.append(f"{store}: raised {e!r}")
    return (
        not failures,
        "; ".join(failures) if failures
        else f"fresh marks absent from stores {sorted(marks)}",
    )


def validate_handles(flags: Mapping[str, Any], handles: Any) -> tuple[bool, str]:
    if not isinstance(handles, Mapping):
        return False, f"returned {type(handles).__name__}, expected mapping"
    if set(handles) != set(flags):
        return False, f"handle stores {sorted(handles)} != flag stores {sorted(flags)}"
    bad = sorted(store for store, handle in handles.items() if not isinstance(handle, str))
    if bad:
        return False, f"non-string handle for stores {bad}"
    return True, f"returns handles for stores {sorted(flags)}"
