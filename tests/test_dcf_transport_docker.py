"""Persistent-memory v1 — DockerExecBackend unit tests.

Mocks `asyncio.create_subprocess_exec` so we don't need a real docker
daemon. Pins:
  ①run_command → CommandResult fields (exit_code, stdout, stderr,
    timed_out) under happy / timeout / non-zero paths.
  ②argv composition: `docker exec -i <name> <cmd...>` for list cmd,
    `docker exec -i <name> bash -c "..."` when cwd / env / str cmd.
  ③write_file uses docker-py put_archive (binary-safe tar wrapping).
  ④kill_process delegates to `pkill -KILL -f <pattern>`.

See docs/persistent_memory_agent_plan_v1.md.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from agents.transport import CommandResult
from agents.transport_docker import DockerExecBackend
from orchestrator.handle import ContainerHandle


def _handle(name: str = "team1_dev") -> ContainerHandle:
    return ContainerHandle(container_id="x", name=name, networks=[])


class _FakeStream:
    """StreamReader stand-in. `read(n)` yields canned bytes until exhausted,
    then blocks (until `eof()` is called) and finally returns b"" (EOF).
    Models docker exec's stdout pipe: bytes accumulate while the proc is
    alive; EOF only after proc dies."""

    def __init__(self, content: bytes):
        self._content = content
        self._returned = False
        self._eof = asyncio.Event()

    def eof(self) -> None:
        self._eof.set()

    async def read(self, n: int = -1) -> bytes:
        if not self._returned and self._content:
            self._returned = True
            return self._content
        # Block until the proc signals EOF, then return empty bytes.
        await self._eof.wait()
        return b""


class _FakeProc:
    """asyncio.subprocess.Process stand-in modeling the new run_command
    contract: drain tasks read from `.stdout` / `.stderr` until EOF;
    `wait()` resolves on terminate/kill or natural exit; `.returncode`
    is populated after wait."""

    def __init__(
        self,
        *,
        returncode: int,
        stdout: bytes,
        stderr: bytes,
        hang: bool = False,
    ):
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.stdin = None
        self._hang = hang
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()
        if hang:
            self.returncode = None
            self._final_rc = returncode
        else:
            self.returncode = returncode
            self.stdout.eof()
            self.stderr.eof()
            self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode  # type: ignore[return-value]

    def terminate(self) -> None:
        self.terminated = True
        # SIGTERM → emulate the process settling. Pipes EOF so drains
        # can complete.
        if self.returncode is None:
            self.returncode = 143  # SIGTERM
        self.stdout.eof()
        self.stderr.eof()
        self._exited.set()

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = 137  # SIGKILL
        self.stdout.eof()
        self.stderr.eof()
        self._exited.set()


def _patch_subprocess(proc):
    """Decorator-free helper: returns the mock factory + patcher context."""
    async def _factory(*args, **kwargs):
        return proc
    return patch("asyncio.create_subprocess_exec", side_effect=_factory)


# ── run_command: happy path ───────────────────────────────────────────


def test_run_command_returns_canonical_result():
    backend = DockerExecBackend(client=MagicMock())
    proc = _FakeProc(returncode=0, stdout=b"hello\n", stderr=b"")

    async def go():
        with _patch_subprocess(proc):
            return await backend.run_command(_handle(), ["echo", "hello"])
    r = asyncio.run(go())

    assert isinstance(r, CommandResult)
    assert r.exit_code == 0
    assert r.stdout == b"hello\n"
    assert r.stderr == b""
    assert r.timed_out is False


def test_run_command_argv_composition():
    """Three branches of argv shape:
      * list cmd, no cwd → `docker exec -i NAME <cmd...>` (no bash wrap)
      * list cmd + cwd → `docker exec -i NAME bash -c 'cd <cwd> && <cmd>'`
      * str cmd → `docker exec -i NAME bash -c '<str>'`
    """
    backend = DockerExecBackend(client=MagicMock())
    seen: list = []

    async def _factory(*args, **kwargs):
        seen.append(list(args))
        return _FakeProc(returncode=0, stdout=b"", stderr=b"")

    async def go():
        with patch("asyncio.create_subprocess_exec", side_effect=_factory):
            await backend.run_command(_handle(), ["echo", "hi"])
            await backend.run_command(_handle(), ["echo", "ok"], cwd="/tmp/work")
            await backend.run_command(_handle(), "echo hi | cat")
    asyncio.run(go())

    # List + no cwd → no bash wrap
    assert seen[0][:4] == ["docker", "exec", "-i", "team1_dev"]
    assert seen[0][4:] == ["echo", "hi"]
    # List + cwd → bash -c, with `cd <cwd>` baked into the shell string
    assert seen[1][4:6] == ["bash", "-c"]
    shell = seen[1][6]
    assert "cd /tmp/work" in shell or "cd '/tmp/work'" in shell
    assert "echo ok" in shell
    # Str cmd → bash -c passthrough
    assert seen[2][4:6] == ["bash", "-c"]
    assert seen[2][6] == "echo hi | cat"


# ── run_command: timeout escalation ───────────────────────────────────


def test_run_command_timeout_terminates_and_returns_timed_out():
    """On wait_for timeout the backend SIGTERMs and returns timed_out=True."""
    backend = DockerExecBackend(client=MagicMock())
    proc = _FakeProc(returncode=0, stdout=b"", stderr=b"", hang=True)

    async def go():
        with _patch_subprocess(proc):
            return await backend.run_command(_handle(), ["sleep", "60"], timeout=0.05)
    r = asyncio.run(go())

    assert r.timed_out is True
    assert proc.terminated is True
    # exit_code = SIGTERM rc (143). The hangProc settles after terminate.
    assert r.exit_code == 143


def test_run_command_timeout_preserves_buffered_stdout():
    """REGRESSION: when a long-running proc times out, the bytes its
    stdout pipe had ALREADY buffered must reach the caller.

    Before the fix this returned empty stdout — `asyncio.wait_for(
    proc.communicate(), timeout)` cancelled the inner gather() and its
    read-tasks dropped their accumulated bytes on the floor. The new
    code drains stdout/stderr into local buffers continuously, so a
    timeout doesn't lose anything."""
    backend = DockerExecBackend(client=MagicMock())
    # Proc hangs (won't exit on its own) but its stdout pipe already
    # has bytes buffered — exactly the live-game shape where claude
    # streamed transcript chunks then ran past the phase timer.
    proc = _FakeProc(
        returncode=0,
        stdout=b'{"transcript":"line1"}\n{"transcript":"line2"}\n',
        stderr=b"some warnings\n",
        hang=True,
    )

    async def go():
        with _patch_subprocess(proc):
            return await backend.run_command(_handle(), ["sleep", "60"], timeout=0.05)
    r = asyncio.run(go())

    assert r.timed_out is True
    assert r.stdout == b'{"transcript":"line1"}\n{"transcript":"line2"}\n', (
        f"buffered stdout was lost on timeout; got {r.stdout!r}"
    )
    assert r.stderr == b"some warnings\n", f"buffered stderr was lost on timeout; got {r.stderr!r}"


# ── write_file / read_file: docker-py archive paths ───────────────────


def test_write_file_uses_put_archive_with_tar():
    """write_file builds a single-entry tar and ships it via put_archive
    rooted at the parent dir. Binary-safe; no shell quoting."""
    container = MagicMock()
    client = MagicMock()
    client.containers.get.return_value = container
    backend = DockerExecBackend(client=client)

    backend.write_file(_handle(), "/var/lib/cyberarena/prompt.txt", b"hello\xff\x00")

    # mkdir -p the parent
    mkdir_calls = [
        c for c in container.exec_run.call_args_list
        if c.args and c.args[0] == ["mkdir", "-p", "/var/lib/cyberarena"]
    ]
    assert mkdir_calls, "expected mkdir -p of parent dir"

    # put_archive called with tar bytes containing prompt.txt
    assert container.put_archive.called
    parent_arg, tarbytes = container.put_archive.call_args.args
    assert parent_arg == "/var/lib/cyberarena"
    with tarfile.open(fileobj=io.BytesIO(tarbytes)) as tar:
        members = tar.getmembers()
        assert len(members) == 1
        assert members[0].name == "prompt.txt"
        f = tar.extractfile(members[0])
        assert f is not None
        assert f.read() == b"hello\xff\x00"


def test_read_file_unwraps_tar_from_get_archive():
    container = MagicMock()
    # Build a synthetic tar stream that get_archive would return.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="hello.txt")
        info.size = 5
        tar.addfile(info, io.BytesIO(b"world"))
    container.get_archive.return_value = (iter([buf.getvalue()]), {"size": 5})

    client = MagicMock()
    client.containers.get.return_value = container
    backend = DockerExecBackend(client=client)

    out = backend.read_file(_handle(), "/var/lib/cyberarena/hello.txt")
    assert out == b"world"


# ── kill_process ──────────────────────────────────────────────────────


def test_kill_process_runs_pkill():
    container = MagicMock()
    client = MagicMock()
    client.containers.get.return_value = container
    backend = DockerExecBackend(client=client)

    backend.kill_process(_handle(), "claude -p")

    assert any(
        c.args and c.args[0] == ["pkill", "-KILL", "-f", "claude -p"]
        for c in container.exec_run.call_args_list
    ), container.exec_run.call_args_list


def test_kill_process_is_best_effort_swallows_exceptions():
    client = MagicMock()
    client.containers.get.side_effect = RuntimeError("docker is down")
    backend = DockerExecBackend(client=client)

    # Should not raise.
    backend.kill_process(_handle(), "claude -p")


# ── kill_session: env-marker reap (group-independent) ─────────────────


def _backend_with_kill_output(output: bytes):
    container = MagicMock()
    container.exec_run.return_value = MagicMock(output=output)
    client = MagicMock()
    client.containers.get.return_value = container
    return DockerExecBackend(client=client), container


def test_kill_session_script_does_pgid_then_environ_scan_and_reverify():
    backend, container = _backend_with_kill_output(b"KILLED= 4242\nREMAIN=\n")
    backend.kill_session(_handle(), "deadbeef0001")

    argv = container.exec_run.call_args.args[0]
    assert argv[:2] == ["sh", "-c"]
    script = argv[2]
    # (1) PGID best-effort from the pidfile.
    assert "/tmp/cyberarena_phase_deadbeef0001.pid" in script
    assert "kill -KILL -- -" in script
    # (2) env-marker scan over /proc + SIGKILL by PID.
    assert "/proc/[0-9]*" in script
    assert "/environ" in script
    assert "CYBERARENA_PHASE=deadbeef0001" in script
    assert "kill -KILL \"$pid\"" in script
    # (3) re-scan readout for the host.
    assert "KILLED=" in script and "REMAIN=" in script


def test_kill_session_clean_reap_is_quiet(caplog):
    # Killed a pid, nothing remains carrying the marker.
    backend, _ = _backend_with_kill_output(b"KILLED= 4242\nREMAIN=\n")
    with caplog.at_level("WARNING", logger="agents.transport_docker"):
        backend.kill_session(_handle(), "aaaa00000000")
    assert not caplog.records, [r.getMessage() for r in caplog.records]


def test_kill_session_quiet_when_nothing_matched(caplog):
    # Agent already exited in the gap → no pids killed, none remain.
    backend, _ = _backend_with_kill_output(b"KILLED=\nREMAIN=\n")
    with caplog.at_level("WARNING", logger="agents.transport_docker"):
        backend.kill_session(_handle(), "bbbb00000000")
    assert not caplog.records, [r.getMessage() for r in caplog.records]


def test_kill_session_errors_when_marker_survives(caplog):
    """The real failure signal: a process still carries the marker after
    SIGKILL → the reap genuinely failed. Must ERROR, not pass silently."""
    backend, _ = _backend_with_kill_output(b"KILLED= 4242\nREMAIN= 4242 4243\n")
    with caplog.at_level("ERROR", logger="agents.transport_docker"):
        backend.kill_session(_handle(), "cccc00000000")
    assert any(r.levelname == "ERROR" for r in caplog.records)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "STILL alive" in msg and "4242 4243" in msg


def test_kill_session_swallows_docker_exception(caplog):
    client = MagicMock()
    client.containers.get.side_effect = RuntimeError("docker is down")
    backend = DockerExecBackend(client=client)
    # Should not raise.
    backend.kill_session(_handle(), "dddd00000000")
