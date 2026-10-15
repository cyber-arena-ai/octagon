"""In-process port adapters — the default wiring GameMgr builds.

Each adapter is a thin shim over an existing GameMgr capability:

  LocalStatusPort  → a `() -> dict` callable (GameMgr.status)
  LocalEventSink   → anything with a Timeline-shaped `.record(...)`
  LocalExecPort    → a container resolver + GameMgr._exec_in_container

`LocalExecPort` is the one with real behavior: it resolves (team_id,
role) to a container name host-side, then runs the blocking docker exec
on a daemon thread with a deadline so a hung exec releases the caller
instead of wedging the round loop (the daemon thread dies with the
process, so it never blocks interpreter exit).
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Any, Callable

from .ports import ExecResult

log = logging.getLogger(__name__)

StatusFn = Callable[[], dict]
ContainerResolver = Callable[[str, str], str]  # (team_id, role) -> container_name
DockerExec = Callable[[str, str], "tuple[int, str]"]  # (container, cmd) -> (rc, output)
# (container, cmd, stdin, timeout_s) -> (rc, output). Feeds `stdin` to `cmd`
# under a killable deadline the CALLER owns (subprocess timeout host-side).
DockerExecStdin = Callable[[str, str, bytes, float], "tuple[int, str]"]


class LocalStatusPort:
    def __init__(self, status_fn: StatusFn) -> None:
        self._fn = status_fn

    def status(self) -> dict:
        return self._fn()


class LocalEventSink:
    def __init__(self, recorder: Any) -> None:
        # `recorder` is duck-typed (Timeline) so this package stays free
        # of any gamemgr import.
        self._recorder = recorder

    def record(
        self,
        type_: str,
        *,
        team_id: str | None = None,
        round_id: int = 0,
        phase: str = "",
        payload: dict | None = None,
    ) -> Any:
        return self._recorder.record(
            type_, team_id=team_id, round_id=round_id, phase=phase, payload=payload
        )


class LocalExecPort:
    def __init__(
        self,
        *,
        container_resolver: ContainerResolver,
        docker_exec: DockerExec,
        docker_exec_stdin: DockerExecStdin | None = None,
    ) -> None:
        self._resolve = container_resolver
        self._docker_exec = docker_exec
        self._docker_exec_stdin = docker_exec_stdin

    def exec(
        self,
        team_id: str,
        role: str,
        cmd: str,
        *,
        timeout_s: float = 120.0,
    ) -> ExecResult:
        container = self._resolve(team_id, role)
        box: dict[str, Any] = {}

        def _work() -> None:
            try:
                box["ok"] = self._docker_exec(container, cmd)
            except Exception as e:  # noqa: BLE001 — surfaced to caller below
                box["err"] = e

        t = threading.Thread(target=_work, name="facility-exec", daemon=True)
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            log.warning(
                "facility exec timed out after %gs on %s: %.80s",
                timeout_s, container, cmd,
            )
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"exec timed out after {timeout_s:g}s",
                timed_out=True,
            )
        if "err" in box:
            # Preserve the pre-refactor behavior where a docker-exec
            # error propagates to the caller (challenge handlers wrap it).
            raise box["err"]
        exit_code, output = box["ok"]
        return ExecResult(exit_code=exit_code, stdout=output, stderr="")

    def exec_stdin(
        self,
        team_id: str,
        role: str,
        cmd: str,
        stdin: bytes,
        *,
        timeout_s: float = 120.0,
    ) -> ExecResult:
        if self._docker_exec_stdin is None:
            raise RuntimeError(
                "LocalExecPort.exec_stdin called but no docker_exec_stdin seam was "
                "injected (GameMgr wires it; a bare LocalExecPort can't pipe stdin)"
            )
        container = self._resolve(team_id, role)
        # Single timeout owner: the seam's `docker exec -i` runs under
        # subprocess.run(timeout=timeout_s), which hard-kills the client on
        # deadline. So — unlike `exec` — no daemon-thread wrapper here; a second
        # deadline would risk a double-kill / duplicate run / leaked thread.
        try:
            exit_code, output = self._docker_exec_stdin(container, cmd, stdin, timeout_s)
        except subprocess.TimeoutExpired:
            log.warning(
                "facility exec_stdin timed out after %gs on %s: %.80s",
                timeout_s, container, cmd,
            )
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"exec_stdin timed out after {timeout_s:g}s",
                timed_out=True,
            )
        return ExecResult(exit_code=exit_code, stdout=output, stderr="")
