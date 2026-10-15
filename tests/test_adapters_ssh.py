"""v3 step 4 core: SshExecPort — facility→prod exec over SSH, role-allowlisted to
prod, identity-resolved (team_id -> host). The ssh runner is injected, so these
are offline (no real ssh)."""

from __future__ import annotations

import shlex
import subprocess

import pytest

from facility.adapters_ssh import SshExecPort
from facility.ports import ExecResult


def _port(runner, hosts=None):
    return SshExecPort(
        key_path="/keys/facility",
        host_by_team=hosts if hosts is not None else {"team1": "team1_prod"},
        runner=runner,
    )


class _Done:
    def __init__(self, rc=0, out=b"ok\n", err=b""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_builds_ssh_argv_and_returns_result():
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        seen["timeout"] = kw.get("timeout")
        return _Done(rc=0, out=b"planted", err=b"")

    res = _port(runner).exec("team1", "prod", "echo hi", timeout_s=5)
    assert isinstance(res, ExecResult)
    assert res.exit_code == 0 and res.stdout == "planted" and res.timed_out is False
    argv = seen["argv"]
    assert argv[0] == "ssh" and "/keys/facility" in argv
    assert "root@team1_prod" in argv
    # The command is handed to ssh as a SINGLE trailing arg: `bash -c <quoted>`.
    # ssh joins trailing argv with spaces and the remote shell re-parses, so the
    # remote command must be one self-contained shell string (multi-word cmds
    # like `python3 -c '<probe>'` would otherwise be truncated to their 1st word).
    assert argv[-1] == "bash -c " + shlex.quote("echo hi")
    assert seen["timeout"] == 5


def test_ssh_opts_fail_fast_timeouts():
    """A plant/probe into an unreachable or mid-restart prod must abort fast,
    not hang until the subprocess timeout backstop (a hung SSH used to wedge the
    flag-service, stalling rotations AND submits). ConnectTimeout bounds the TCP
    handshake; ServerAlive* bounds a connection that stalls after connecting."""
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        return _Done()

    _port(runner).exec("team1", "prod", "echo hi")
    joined = " ".join(seen["argv"])
    assert "ConnectTimeout=10" in joined
    assert "ServerAliveInterval=5" in joined
    assert "ServerAliveCountMax=2" in joined


def test_role_allowlist_rejects_non_prod():
    with pytest.raises(ValueError):
        _port(lambda *a, **k: _Done()).exec("team1", "dev", "id")
    with pytest.raises(ValueError):
        _port(lambda *a, **k: _Done()).exec("team1", "", "id")


def test_unknown_team_raises():
    with pytest.raises(KeyError):
        _port(lambda *a, **k: _Done(), hosts={}).exec("team1", "prod", "id")


def test_nonzero_exit_and_stderr_captured():
    def runner(argv, **kw):
        return _Done(rc=2, out=b"", err=b"boom")
    res = _port(runner).exec("team1", "prod", "false")
    assert res.exit_code == 2 and res.stderr == "boom"


def test_timeout_returns_timed_out_result():
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))
    res = _port(runner).exec("team1", "prod", "sleep 999", timeout_s=1)
    assert res.timed_out is True and res.exit_code == -1


def test_custom_user():
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        return _Done()
    SshExecPort(key_path="/k", host_by_team={"team1": "h"},
                user="arena_agent", runner=runner).exec("team1", "prod", "id")
    assert "arena_agent@h" in seen["argv"]


# ── exec_stdin: ship a script over stdin to `bash -s`, no bash -c double-wrap ──


def test_exec_stdin_pipes_script_and_no_double_wrap():
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw.get("input")
        seen["timeout"] = kw.get("timeout")
        return _Done(rc=0, out=b"CHECKER ok\n", err=b"")

    res = _port(runner).exec_stdin(
        "team1", "prod", "bash -s", b"echo hi\n", timeout_s=30)
    assert res.exit_code == 0 and res.stdout == "CHECKER ok\n"
    # The script arrives on stdin; the remote command is the literal "bash -s"
    # (NOT wrapped in `bash -c 'bash -s'`).
    assert seen["argv"][-1] == "bash -s"
    assert seen["input"] == b"echo hi\n"
    # Caller's timeout is threaded straight to subprocess.run.
    assert seen["timeout"] == 30


def test_exec_stdin_preserves_rc_and_stderr():
    def runner(argv, **kw):
        return _Done(rc=3, out=b"", err=b"cheese-path detected")
    res = _port(runner).exec_stdin("team1", "prod", "bash -s", b"...")
    assert res.exit_code == 3 and res.stderr == "cheese-path detected"


def test_exec_stdin_timeout_returns_timed_out():
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))
    res = _port(runner).exec_stdin("team1", "prod", "bash -s", b"...", timeout_s=1)
    assert res.timed_out is True and res.exit_code == -1


def test_exec_stdin_role_allowlist_and_unknown_team():
    with pytest.raises(ValueError):
        _port(lambda *a, **k: _Done()).exec_stdin("team1", "dev", "bash -s", b"x")
    with pytest.raises(KeyError):
        _port(lambda *a, **k: _Done(), hosts={}).exec_stdin(
            "team1", "prod", "bash -s", b"x")


def test_exec_itself_still_uses_bash_c_wrapper():
    """Regression: adding exec_stdin must not reshape the ordinary exec path."""
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw.get("input")
        return _Done()
    _port(runner).exec("team1", "prod", "echo hi")
    assert seen["argv"][-1] == "bash -c " + shlex.quote("echo hi")
    assert seen["input"] is None  # no stdin plumbed on the ordinary path
