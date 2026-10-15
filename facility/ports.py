"""Facility ports — the boundary between a facility (FlagServer,
HealthPoller, …) and its host (GameMgr).

A facility never reaches into GameMgr directly. It talks through narrow
ports so the same facility code runs whether the host is in-process
(Local* adapters, see `adapters_local.py`) or remote-over-HTTP (a future
adapter, see `docs/facility_abstraction_plan_v1.md` §"Phase 2/3").

  StatusPort  — round / phase, for stamping events + phase gating.
  EventSink   — append one telemetry event (mirrors Timeline.record).
  ExecPort    — run a command inside a team's container, addressed by
                *identity* (team_id, role) — never by raw container name,
                so the host resolves the container and an untrusted caller
                can't exec into something it shouldn't.
  AuthoritativeStatePort — read one statically allowlisted application-state
                region through the trusted host, without executing in prod.

Stdlib-only. No imports from gamemgr / flag / health, so the boundary
stays acyclic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from challenges.interface import StateReadResult


@dataclass(frozen=True)
class ExecResult:
    """Outcome of one `ExecPort.exec`.

    `stdout` carries the command output. The in-process docker-exec
    transport merges stderr into stdout (docker's default), so `stderr`
    is "" there; it's kept distinct in the contract for transports that
    can split the streams (e.g. an SSH-based one). `timed_out` is True
    when the call exceeded its deadline — the command may still be
    running host-side, but the caller was released so a hung exec can't
    wedge the round loop.
    """

    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False


@runtime_checkable
class StatusPort(Protocol):
    def status(self) -> dict:
        """Return at least `{'round': int, 'phase': str}`; the in-process
        adapter also includes `'total_rounds'`."""
        ...


@runtime_checkable
class EventSink(Protocol):
    def record(
        self,
        type_: str,
        *,
        team_id: str | None = None,
        round_id: int = 0,
        phase: str = "",
        payload: dict | None = None,
    ) -> Any:
        """Append one event. Signature mirrors
        `gamemgr.timeline.Timeline.record` (EventType is a `str` subclass,
        so it's accepted as `type_` without importing it here)."""
        ...


@runtime_checkable
class ExecPort(Protocol):
    def exec(
        self,
        team_id: str,
        role: str,
        cmd: str,
        *,
        timeout_s: float = 120.0,
    ) -> ExecResult:
        """Run `cmd` (as `bash -c`) inside the container identified by
        (`team_id`, `role`). The host resolves the identity to a concrete
        container — callers pass identity, not a container name.

        `timeout_s` defaults generous: the restart-handler compile path
        legitimately runs for tens of seconds, so this is a hang safety
        net, not a tight per-call budget. Tighter, per-operation budgets
        (short for health probes) are a Phase 2 refinement once the
        boundary carries operation context.
        """
        ...

    def exec_stdin(
        self,
        team_id: str,
        role: str,
        cmd: str,
        stdin: bytes,
        *,
        timeout_s: float = 120.0,
    ) -> ExecResult:
        """Run `cmd` inside the container identified by (`team_id`, `role`),
        feeding `stdin` to the command's standard input.

        The one caller today is the health-poller's CHECKER runner, which
        runs `cmd="bash -s"` with the checker script as `stdin` — so the
        executed bytes come from the facility, never from the (agent-root,
        tamperable) prod filesystem. Kept SEPARATE from `exec` so the common
        no-stdin path isn't reshaped for a single special capability.

        `timeout_s` is the single owner of the deadline: adapters must thread
        it to the underlying killable call (`subprocess.run(timeout=...)`) and
        must NOT add a second, independent deadline on top.
        """
        ...


@runtime_checkable
class AuthoritativeStatePort(Protocol):
    """Read a challenge-declared application-state region outside prod.

    Callers address a region by trusted identities plus an opaque name. Paths
    and commands are deliberately absent from this boundary: the host resolves
    them from the challenge's static allowlist. Optional — only a `retrieve()`
    that must not trust code running inside the defender's container needs it.
    """

    def read(
        self,
        team_id: str,
        service: str,
        region: str,
    ) -> StateReadResult:
        ...
