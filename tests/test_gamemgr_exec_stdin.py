"""GameMgr._exec_in_container_stdin — backs LocalExecPort.exec_stdin via
`docker exec -i <qualified> bash -s`, feeding the checker script on stdin. The
subprocess runner is patched, so this is offline. Only `self.ns` is needed, so
we call the unbound method on a tiny stub rather than building a full GameMgr."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from gamemgr.game_mgr import GameMgr


class _Completed:
    def __init__(self, rc=0, out=b"ok\n"):
        self.returncode, self.stdout, self.stderr = rc, out, b""


def _call(monkeypatch, *, ns, rc=0, out=b"ok\n"):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["input"] = kw.get("input")
        seen["timeout"] = kw.get("timeout")
        seen["stdout"] = kw.get("stdout")
        seen["stderr"] = kw.get("stderr")
        return _Completed(rc=rc, out=out)

    monkeypatch.setattr(subprocess, "run", fake_run)
    stub = SimpleNamespace(ns=ns)
    result = GameMgr._exec_in_container_stdin(
        stub, "team1_prod", "bash -s", b"echo hi\n", 30.0)
    return result, seen


def test_runs_docker_exec_i_bash_s_with_qualified_name(monkeypatch):
    (rc, out), seen = _call(monkeypatch, ns="game7", out=b"CHECKER ok\n")
    assert rc == 0 and out == "CHECKER ok\n"
    # docker exec -i <qualified> bash -s ; name qualified with the namespace.
    assert seen["argv"] == ["docker", "exec", "-i", "game7-team1_prod", "bash", "-s"]
    assert seen["input"] == b"echo hi\n"
    assert seen["timeout"] == 30.0
    # stderr MERGED into stdout (like the SDK sibling _exec_in_container), so a
    # checker.sh that writes its verdict to stderr isn't dropped.
    assert seen["stdout"] is subprocess.PIPE
    assert seen["stderr"] is subprocess.STDOUT


def test_empty_namespace_leaves_name_bare(monkeypatch):
    _, seen = _call(monkeypatch, ns="")
    assert seen["argv"][3] == "team1_prod"


def test_nonzero_exit_propagates(monkeypatch):
    (rc, _out), _ = _call(monkeypatch, ns="", rc=3)
    assert rc == 3


def test_timeout_propagates_as_TimeoutExpired(monkeypatch):
    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))
    monkeypatch.setattr(subprocess, "run", fake_run)
    stub = SimpleNamespace(ns="")
    with pytest.raises(subprocess.TimeoutExpired):
        GameMgr._exec_in_container_stdin(stub, "team1_prod", "bash -s", b"x", 1.0)
