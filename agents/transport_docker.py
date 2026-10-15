"""DockerExecBackend — AgentTransport implementation against a local
Docker daemon.

`run_command` shells out to `docker exec` via `asyncio.create_subprocess_exec`
so the orchestrator's FastAPI event loop stays free to serve the
agent's MCP tool calls during a long-running phase. `write_file` and
`kill_process` use the same approach for consistency (everything is
one `docker exec` call away).

See docs/persistent_memory_agent_plan_v1.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from pathlib import Path

import docker

from .transport import AgentHandle, AgentTransport, CommandResult

log = logging.getLogger(__name__)


def _stamp_chunk(buf: bytearray, chunk: bytes, now: float, line_no: int, ts_sink) -> int:
    """Append `chunk` to the carry-over `buf`, and for each newly COMPLETE
    newline-terminated line write `<line_no>\\t<ts>` to `ts_sink`; return the
    updated line_no. `buf` holds the partial trailing line across chunks so a
    line split over two reads is stamped once, at the read that completes it.

    Extracted from the stdout drain so the timing-sidecar logic is unit-testable
    without a live docker exec. Line numbers align 1:1 with the main stream file
    (same bytes, same `\\n` split), so a consumer zips the two by line number."""
    buf += chunk
    nl = buf.find(b"\n")
    while nl >= 0:
        del buf[:nl + 1]
        line_no += 1
        try:
            ts_sink.write(f"{line_no}\t{now:.4f}\n")
            ts_sink.flush()
        except OSError:
            pass
        nl = buf.find(b"\n")
    return line_no


class DockerExecBackend(AgentTransport):
    """`docker exec`-based AgentTransport. Stateless; one instance handles
    every team's container.
    """

    # Grace period between SIGTERM and SIGKILL when run_command times out.
    KILL_GRACE_SECS = 2.0

    def __init__(self, client: docker.DockerClient | None = None) -> None:
        # docker-py client is used for file I/O (put_archive / get_archive)
        # which is binary-safe and avoids the shell quoting hazards of
        # piping bytes through `docker exec -i`.
        self._client = client or docker.from_env()

    async def connect(self, handle: AgentHandle) -> None:
        # docker exec is stateless; no per-handle setup needed. The
        # SSHBackend equivalent would open a control-master connection.
        return None

    async def run_command(
        self,
        handle: AgentHandle,
        cmd: list[str] | str,
        *,
        stdin: bytes | None = None,
        timeout: float | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        tee_path: "str | Path | None" = None,
    ) -> CommandResult:
        """Run `cmd` via `docker exec`. On timeout: terminate → kill →
        pkill belt-and-suspenders.

        cwd is implemented by prepending `cd <cwd> && ` to a bash -c
        invocation. env is implemented by prepending `K=V ...` (NOT
        `--env` to docker exec, which would require the variables to be
        defined in the container's environment file). Both are best-
        effort conveniences; callers that need richer setup should bake
        it into `cmd` directly.

        `tee_path`: if set, each stdout chunk is appended+flushed to this file as
        it arrives — so a long-running agent's stream-json transcript is readable
        LIVE (e.g. the panel's `/game/agents` poll) instead of only at the end.
        """
        argv = self._build_argv(handle, cmd, cwd=cwd, env=env)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # We drain stdout/stderr into local buffers via dedicated tasks so
        # cancelling on timeout doesn't lose any already-buffered output
        # (the bug `asyncio.wait_for(proc.communicate(), …)` had — when the
        # outer wait_for cancelled the inner communicate(), the bytes its
        # gathered read-tasks had accumulated were dropped on the floor).
        # Drains finish naturally when proc exits and its pipes EOF.
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        tee = None
        ts_tee = None
        if tee_path is not None:
            try:
                Path(tee_path).parent.mkdir(parents=True, exist_ok=True)
                tee = open(tee_path, "wb")   # noqa: SIM115 — closed in finally below
                # Sidecar: arrival wall-clock (epoch secs) per stream-json LINE, as
                # `<line_no>\t<ts>`. Lets metrics derive per-tool-call latency
                # (tool_use→tool_result / item.started→item.completed) and token
                # rate WITHOUT touching the main stream (kept byte-identical for
                # CLI resume + the transcript parser). Best-effort, like `tee`.
                ts_tee = open(str(tee_path) + ".timing", "w")  # noqa: SIM115
            except OSError:
                tee = None   # live tee is best-effort; the final write still lands
                ts_tee = None

        async def _drain(reader: asyncio.StreamReader | None, sink: list[bytes],
                         live=None, ts_sink=None) -> None:
            if reader is None:
                return
            line_no = 0
            buf = bytearray()
            while True:
                chunk = await reader.read(8192)
                if not chunk:
                    return
                sink.append(chunk)
                if live is not None:
                    try:
                        live.write(chunk); live.flush()
                    except OSError:
                        pass
                if ts_sink is not None:
                    # Stamp at chunk arrival; every complete line in this read
                    # shares that instant (sub-8KB ordering is noise vs the tens
                    # of ms a tool round-trip takes).
                    line_no = _stamp_chunk(buf, chunk, time.time(), line_no, ts_sink)

        stdout_task = asyncio.create_task(_drain(proc.stdout, stdout_chunks, tee, ts_tee))
        stderr_task = asyncio.create_task(_drain(proc.stderr, stderr_chunks))

        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin)
            try:
                await proc.stdin.drain()
            finally:
                proc.stdin.close()

        timed_out = False
        pkilled_at: float | None = None
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            # Stamp the moment the phase boundary hit this agent. The
            # subprocess settling can lag by tens of seconds while
            # docker exec propagates the signal — pkilled_at is what
            # the panel should use for ordering, not the event ts.
            pkilled_at = time.time()
            # SIGTERM the docker exec subprocess (may or may not propagate
            # to the in-container child; kill_process is the safety net).
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.KILL_GRACE_SECS)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

        # Pipes are guaranteed to EOF now that the process has exited;
        # both drain tasks will return on their own.
        await stdout_task
        await stderr_task
        if tee is not None:
            try:
                tee.close()
            except OSError:
                pass
        if ts_tee is not None:
            try:
                ts_tee.close()
            except OSError:
                pass

        return CommandResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=b"".join(stdout_chunks),
            stderr=b"".join(stderr_chunks),
            timed_out=timed_out,
            pkilled_at=pkilled_at,
        )

    def write_file(
        self,
        handle: AgentHandle,
        remote_path: str,
        content: bytes,
    ) -> None:
        """Write content into the container via docker-py's put_archive
        (binary-safe; no shell quoting). Creates parent dirs as needed."""
        import io
        import tarfile

        parent, fname = os.path.split(remote_path)
        if parent:
            # Ensure parent exists. Idempotent.
            c = self._client.containers.get(handle.name)
            c.exec_run(["mkdir", "-p", parent])

        tarstream = io.BytesIO()
        with tarfile.open(fileobj=tarstream, mode="w") as tar:
            info = tarfile.TarInfo(name=fname)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
        tarstream.seek(0)

        c = self._client.containers.get(handle.name)
        c.put_archive(parent or "/", tarstream.read())

    def write_tree(
        self,
        handle: AgentHandle,
        dest_dir: str,
        tar_stream,
    ) -> None:
        """Extract a tar archive under `dest_dir` with a single put_archive.

        One API call for the whole tree, versus `write_file`'s three per file
        (containers.get, an in-container `mkdir -p` exec, then put_archive).
        Docker creates intermediate directories from the tar's own entries, so
        the per-file mkdir is not merely slow, it is unnecessary.

        `dest_dir` must exist — put_archive requires it — so it is created once
        here rather than once per file.
        """
        c = self._client.containers.get(handle.name)
        c.exec_run(["mkdir", "-p", dest_dir])
        c.put_archive(dest_dir, tar_stream)

    def read_file(self, handle: AgentHandle, remote_path: str) -> bytes:
        """Read a file from the container via docker-py's get_archive.
        Returns the file's raw bytes (the tarball wrapping is unwrapped)."""
        import io
        import tarfile

        c = self._client.containers.get(handle.name)
        stream, _ = c.get_archive(remote_path)
        raw = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            members = tar.getmembers()
            if not members:
                return b""
            f = tar.extractfile(members[0])
            return f.read() if f else b""

    def kill_process(self, handle: AgentHandle, pattern: str) -> None:
        """Best-effort pkill inside the container. Used by teardown
        paths (e.g. ClaudeAgentManager.kill) that don't have a specific
        PID. Prefer `kill_session` when a session marker is available
        to avoid racing concurrent claude instances in the same container."""
        try:
            c = self._client.containers.get(handle.name)
            c.exec_run(["pkill", "-KILL", "-f", pattern])
        except Exception:
            log.exception("kill_process failed on %s pattern=%r", handle.name, pattern)

    def kill_session(self, handle: AgentHandle, session_marker: str) -> None:
        """Reap the timed-out claude by a per-phase env marker, so it works
        even when the process escapes its launch group.

        phase_command exports `CYBERARENA_PHASE=<marker>` into claude's
        env (inherited by any children). Steps:
          1. PGID kill from the pidfile — best-effort fast path.
          2. Scan `/proc/<pid>/environ` for `CYBERARENA_PHASE=<marker>`
             and SIGKILL every matching PID.
          3. Re-scan to confirm none survive; ERROR if any do.

        Session-scoped: the marker is unique per invocation, so the
        sibling claude the next phase just started (different marker) is
        never touched. Runs as root via docker exec, so /proc and SIGKILL
        of any PID are permitted.
        """
        marker = session_marker
        pidfile = f"/tmp/cyberarena_phase_{marker}.pid"
        env_line = f"CYBERARENA_PHASE={marker}"
        # Single script: PGID best-effort, then env-marker scan/kill, then
        # a re-scan. Emits KILLED=<pids> and REMAIN=<pids> for the host.
        script = f"""
pgid=$(cat {pidfile} 2>/dev/null | tr -d ' ')
[ -n "$pgid" ] && kill -KILL -- -"$pgid" 2>/dev/null
rm -f {pidfile}
killed=""
for d in /proc/[0-9]*; do
  if tr '\\0' '\\n' < "$d/environ" 2>/dev/null | grep -qxF "{env_line}"; then
    pid=${{d#/proc/}}
    kill -KILL "$pid" 2>/dev/null
    killed="$killed $pid"
  fi
done
sleep 0.1
remain=""
for d in /proc/[0-9]*; do
  if tr '\\0' '\\n' < "$d/environ" 2>/dev/null | grep -qxF "{env_line}"; then
    remain="$remain ${{d#/proc/}}"
  fi
done
echo "KILLED=$killed"
echo "REMAIN=$remain"
"""
        try:
            c = self._client.containers.get(handle.name)
            res = c.exec_run(["sh", "-c", script])
            out = res.output.decode(errors="replace") if res.output else ""
        except Exception:
            log.exception("kill_session: docker exec failed on %s marker=%s", handle.name, marker)
            return

        def _val(prefix: str) -> str:
            return next(
                (ln[len(prefix):].strip() for ln in out.splitlines() if ln.startswith(prefix)),
                "",
            )

        killed, remain = _val("KILLED="), _val("REMAIN=")
        if remain:
            log.error(
                "kill_session: marker=%s STILL alive after SIGKILL on %s (pids:%s) — agent not reaped",
                marker, handle.name, remain,
            )
        elif killed:
            log.debug("kill_session: reaped marker=%s on %s (pids:%s)", marker, handle.name, killed)
        else:
            log.debug(
                "kill_session: no live process carried marker=%s on %s (already exited?)",
                marker, handle.name,
            )

    # ── internals ───────────────────────────────────────────────────────

    def _build_argv(
        self,
        handle: AgentHandle,
        cmd: list[str] | str,
        *,
        cwd: str | None,
        env: dict[str, str] | None,
    ) -> list[str]:
        """Compose the full `docker exec` argv. For str cmd we go through
        bash -c (single command line); for list cmd we hand each token
        through individually unless cwd/env force a shell."""
        prefix: list[str] = []
        if env:
            prefix = [f"{k}={shlex.quote(v)}" for k, v in env.items()]

        if cwd or env or isinstance(cmd, str):
            if isinstance(cmd, list):
                shell = " ".join(shlex.quote(a) for a in cmd)
            else:
                shell = cmd
            if cwd:
                shell = f"cd {shlex.quote(cwd)} && {shell}"
            if prefix:
                shell = " ".join(prefix) + " " + shell
            return ["docker", "exec", "-i", handle.name, "bash", "-c", shell]
        return ["docker", "exec", "-i", handle.name, *cmd]
