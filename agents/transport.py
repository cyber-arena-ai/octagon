"""AgentTransport — pluggable execution backend for the agent host.

`ClaudeAgentManager` never reaches for `docker exec` directly. All
side effects on the agent host (running commands, writing files,
killing processes) go through this interface. Two implementations
live as siblings:

  - DockerExecBackend  (agents/transport_docker.py)  — local Docker
  - SSHBackend         (deferred)                    — remote VM / AWS

See docs/persistent_memory_agent_plan_v1.md for the design.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestrator.handle import ContainerHandle


# AgentHandle is opaque to the manager; each transport interprets it.
# For DockerExecBackend it's ContainerHandle (only `.name` is used).
# SSHBackend would carry host/user/key. Aliased here so manager-side
# code can type-hint `AgentHandle` without importing ContainerHandle.
AgentHandle = ContainerHandle


@dataclass(frozen=True)
class CommandResult:
    """Returned by AgentTransport.run_command. `timed_out=True` means
    the backend already escalated SIGTERM → SIGKILL; stdout/stderr
    are whatever was captured up to that point.

    `pkilled_at`: when the timeout actually fired and SIGTERM was issued
    (wall-clock seconds, time.time()). None when the process exited
    cleanly. Surfaces "when the phase boundary really hit this agent"
    separately from "when the subprocess finally settled" — useful for
    panel ordering, since the latter can lag by tens of seconds while
    docker exec propagates the signal."""

    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    pkilled_at: float | None = None


class AgentTransport(Protocol):
    """One-host execution surface. Implementations are stateless across
    handles (the same transport instance handles every team)."""

    async def connect(self, handle: AgentHandle) -> None:
        """One-time transport setup. No-op for docker; opens a control
        master / persistent connection for ssh."""
        ...

    async def run_command(
        self,
        handle: AgentHandle,
        cmd: list[str] | str,
        *,
        stdin: bytes | None = None,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run `cmd` on the agent host. Returns exit/stdout/stderr/timed_out.
        Backend handles its own SIGTERM → SIGKILL escalation on timeout."""
        ...

    def write_file(
        self,
        handle: AgentHandle,
        remote_path: str,
        content: bytes,
    ) -> None:
        """Write content to a path on the agent host. Idempotent."""
        ...

    def write_tree(
        self,
        handle: AgentHandle,
        dest_dir: str,
        tar_stream,
    ) -> None:
        """Extract a whole tar archive under `dest_dir` in ONE operation.

        The bulk counterpart to `write_file`, and the only sane way to move a
        source tree: `write_file` costs a round trip (plus a container `mkdir`
        exec) PER FILE, so staging a real application's source — 45k files,
        twice, once per team — cost ~270k round trips and stalled setup for 34
        minutes before the round could start. A tar already encodes its own
        directory structure, so one call replaces all of it.

        `tar_stream` is bytes or a seekable binary file object, so a large tree
        can spill to disk instead of being held in memory twice.
        """
        ...

    def read_file(self, handle: AgentHandle, remote_path: str) -> bytes:
        """Read a file from the agent host. Used to pull artifacts when
        the backend can't return them inline via run_command stdout."""
        ...

    def kill_process(self, handle: AgentHandle, pattern: str) -> None:
        """Force-kill processes matching pattern (pgrep-style). Used by
        teardown paths (e.g. ClaudeAgentManager.kill) that don't have a
        specific PID. Best-effort: never raises.

        Prefer `kill_session` when a session marker is available —
        `kill_process` races when the same container hosts multiple
        phases in sequence (a freshly-started next-phase claude
        matches the same pattern).
        """
        ...

    def kill_session(self, handle: AgentHandle, session_marker: str) -> None:
        """Kill the specific process whose PID was written to
        `/tmp/cyberarena_phase_<marker>.pid` by phase_command's bash
        wrapper. No-op (best-effort) when the file is missing or empty.

        Replaces the racy pattern-based `kill_process` on timeout:
        targets exactly the claude instance started under this session
        marker, not a sibling that just started in the next phase."""
        ...
