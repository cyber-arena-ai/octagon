"""LocalExecPort — in-process ExecPort backing GameMgr's own exec. Focus on the
new `exec_stdin` seam (`docker exec -i bash -s`), which must be a single-timeout,
no-daemon-thread path semantically identical to the SSH transport."""

from __future__ import annotations

import subprocess

import pytest

from facility.adapters_local import LocalExecPort
from facility.ports import ExecResult


def _resolver(team_id, role):
    assert role == "prod"
    return f"{team_id}_prod"


def test_exec_stdin_routes_to_seam_with_bash_s_and_timeout():
    seen = {}

    def docker_exec_stdin(container, cmd, stdin, timeout_s):
        seen.update(container=container, cmd=cmd, stdin=stdin, timeout_s=timeout_s)
        return 0, "CHECKER ok\n"

    port = LocalExecPort(
        container_resolver=_resolver,
        docker_exec=lambda c, cmd: (0, ""),
        docker_exec_stdin=docker_exec_stdin,
    )
    res = port.exec_stdin("team1", "prod", "bash -s", b"echo hi\n", timeout_s=30)
    assert isinstance(res, ExecResult)
    assert res.exit_code == 0 and res.stdout == "CHECKER ok\n"
    assert seen == {
        "container": "team1_prod", "cmd": "bash -s",
        "stdin": b"echo hi\n", "timeout_s": 30,
    }


def test_exec_stdin_maps_subprocess_timeout_to_timed_out():
    def docker_exec_stdin(container, cmd, stdin, timeout_s):
        raise subprocess.TimeoutExpired(cmd="docker exec", timeout=timeout_s)

    port = LocalExecPort(
        container_resolver=_resolver,
        docker_exec=lambda c, cmd: (0, ""),
        docker_exec_stdin=docker_exec_stdin,
    )
    res = port.exec_stdin("team1", "prod", "bash -s", b"...", timeout_s=1)
    assert res.timed_out is True and res.exit_code == -1


def test_exec_stdin_without_seam_raises():
    port = LocalExecPort(
        container_resolver=_resolver,
        docker_exec=lambda c, cmd: (0, ""),
    )
    with pytest.raises(RuntimeError, match="docker_exec_stdin"):
        port.exec_stdin("team1", "prod", "bash -s", b"x")


def test_exec_nonzero_and_ordinary_path_unaffected():
    port = LocalExecPort(
        container_resolver=_resolver,
        docker_exec=lambda c, cmd: (2, "boom"),
    )
    res = port.exec("team1", "prod", "false")
    assert res.exit_code == 2 and res.stdout == "boom"
