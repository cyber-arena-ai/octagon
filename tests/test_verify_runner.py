"""verify_runner.py — the solo-DEFENSE verifier loop's pure helpers.

Covers result parsing + the raw-facts payload the runner reports (it never
classifies CAPTURED/BLOCKED itself — GameMgr does). No subprocess/docker.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from gamemgr import verify_runner as vr


# ── _parse_result: last JSON line on stdout, else $CYBERARENA_VERIFY_RESULT ──

def test_parse_result_last_json_line():
    out = "noise\n{\"outcome\": \"blocked\", \"target_reached\": true}\ntrailing"
    # only a line that is itself a JSON object is taken; "trailing" is ignored
    assert vr._parse_result("noise\n{\"a\": 1}\n{\"b\": 2}") == {"b": 2}
    assert vr._parse_result(out) == {"outcome": "blocked", "target_reached": True}


def test_parse_result_none_on_garbage():
    assert vr._parse_result("no json here") is None
    assert vr._parse_result("") is None


def test_parse_result_from_file(tmp_path, monkeypatch):
    f = tmp_path / "res.json"
    f.write_text(json.dumps({"outcome": "captured", "flag_recovered": True}))
    monkeypatch.setenv("CYBERARENA_VERIFY_RESULT", str(f))
    assert vr._parse_result("no json on stdout") == {"outcome": "captured",
                                                     "flag_recovered": True}


# ── _run_attempt: maps a real exploit subprocess to raw facts ────────────────

def _fake_exploit(tmp_path, body: str) -> str:
    p = tmp_path / "_exploit.py"
    p.write_text(body)
    return str(p)


def test_run_attempt_blocked(tmp_path):
    ex = _fake_exploit(tmp_path, 'import json\nprint(json.dumps({"outcome":"blocked",'
                                 '"target_reached":True,"flag_recovered":False,'
                                 '"submission_status":"none"}))\n')
    r = vr._run_attempt(ex, "aid1", timeout=10)
    assert r["execution_failed"] is False and r["target_reached"] is True
    assert r["flag_recovered"] is False
    # The exploit's self-reported verdict is DROPPED, not relayed: it runs on the
    # box under test, so GameMgr resolves from the raw facts instead.
    assert "outcome" not in r


def test_run_attempt_captured(tmp_path):
    ex = _fake_exploit(tmp_path, 'import json\nprint(json.dumps({"outcome":"captured",'
                                 '"target_reached":True,"flag_recovered":True,'
                                 '"submission_status":"accepted"}))\n')
    r = vr._run_attempt(ex, "aid2", timeout=10)
    assert r["execution_failed"] is False and r["flag_recovered"] is True
    assert r["submission_status"] == "accepted"
    assert "outcome" not in r


def test_run_attempt_nonzero_exit_is_execution_failed(tmp_path):
    ex = _fake_exploit(tmp_path, 'import sys\nsys.exit(1)\n')
    r = vr._run_attempt(ex, "aid3", timeout=10)
    assert r["execution_failed"] is True


def test_run_attempt_unparseable_is_execution_failed(tmp_path):
    ex = _fake_exploit(tmp_path, 'print("no json result")\n')
    r = vr._run_attempt(ex, "aid4", timeout=10)
    assert r["execution_failed"] is True


def test_run_attempt_timeout_is_execution_failed(tmp_path):
    ex = _fake_exploit(tmp_path, 'import time\ntime.sleep(30)\n')
    r = vr._run_attempt(ex, "aid5", timeout=1)
    assert r["execution_failed"] is True and r["detail"] == "timeout"


def test_run_attempt_passes_attempt_id_to_exploit(tmp_path):
    # the exploit echoes back the attempt id it saw in the env
    ex = _fake_exploit(tmp_path, 'import os, json\n'
                                 'print(json.dumps({"target_reached":True,'
                                 '"flag_recovered":False,'
                                 '"submission_status":"none",'
                                 '"seen":os.environ.get("CYBERARENA_VERIFICATION_ATTEMPT_ID")}))\n')
    r = vr._run_attempt(ex, "the-attempt-id", timeout=10)
    # runner only forwards the canonical fields, but the exploit RAN with the id
    assert r["execution_failed"] is False


def test_run_attempt_never_forwards_a_self_reported_verdict(tmp_path):
    """A WRITE PoV has no flag to recover and reports `flag_recovered=false`
    truthfully; its `outcome` key is still dropped. CAPTURED is established by
    the correlated accepted FLAG_SUBMITTED its `claim_capability` emitted, not by
    anything the exploit says about itself."""
    ex = _fake_exploit(tmp_path, 'import json\nprint(json.dumps({"outcome":"CAPTURED",'
                                 '"target_reached":True,"flag_recovered":False,'
                                 '"submission_status":"accepted"}))\n')
    r = vr._run_attempt(ex, "aid9", timeout=10)
    assert "outcome" not in r
    assert r["flag_recovered"] is False and r["submission_status"] == "accepted"


def test_run_attempt_forwards_only_the_canonical_raw_facts(tmp_path):
    ex = _fake_exploit(tmp_path, 'import json\nprint(json.dumps({'
                                 '"target_reached":True,"flag_recovered":True,'
                                 '"submission_status":"accepted"}))\n')
    assert set(vr._run_attempt(ex, "aid10", timeout=10)) == {
        "attempt_id", "execution_failed", "target_reached", "flag_recovered",
        "submission_status",
    }
