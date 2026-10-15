#!/usr/bin/env python3
"""Solo-DEFENSE verifier loop — provisioned into the `script` verifier team's
container and run once per async chunk (so ONE invocation = the whole run; the
supervisor only keeps it alive and reaps it at game end). It is NOT the
scheduler: it self-paces so a crash-restart can't DoS prod or flood submits.

Each cadence tick it:
  1. mints an `attempt_id` (UUID) and reports VERIFIER_ATTEMPT_STARTED;
  2. runs the challenge's ground-truth `_exploit.py` as a subprocess, passing
     `CYBERARENA_VERIFICATION_ATTEMPT_ID` and a hard timeout. The exploit submits
     the flag itself (direct to the flag-service) and prints ONE JSON line:
       {target_reached, flag_recovered, submission_status};
  3. reports VERIFIER_ATTEMPT_RESULT_REPORTED with those RAW facts (or
     execution_failed on non-zero exit / timeout / unparseable output).
GameMgr — not this runner — resolves the terminal (CAPTURED / BLOCKED / ERROR)
by correlating with the FLAG_SUBMITTED(attempt_id). If GameMgr's reply carries
`stop: true` (the verified-fix latch), the runner exits 0 before its next sleep.

Reports go to the mcp-gateway relay `CYBERARENA_VERIFY_URL` (the gateway bridges
the agent net to GameMgr — the verifier can't reach GameMgr directly). Stdlib
only: runs on the agent image's stock python, no pip at attempt time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _post(url: str, token: str, body: dict, *, timeout: float = 10.0) -> dict:
    """POST JSON with the verifier's team token; return the parsed reply ({} on
    any error — a reporting hiccup must never crash the loop)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Team-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="replace")
        return json.loads(raw) if raw else {}
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"[verify_runner] report POST failed: {e}", file=sys.stderr)
        return {}


def _parse_result(stdout: str) -> dict | None:
    """The exploit's structured result: the LAST parseable JSON object on stdout,
    else the file at $CYBERARENA_VERIFY_RESULT. None if neither yields valid
    JSON (→ the runner reports an execution failure)."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                continue
    path = os.environ.get("CYBERARENA_VERIFY_RESULT", "")
    if path and os.path.isfile(path):
        try:
            obj = json.loads(open(path).read())
            if isinstance(obj, dict):
                return obj
        except (ValueError, OSError):
            return None
    return None


def _run_attempt(exploit_path: str, attempt_id: str, timeout: float) -> dict:
    """Run the exploit once; return the RESULT_REPORTED payload (raw facts). A
    non-zero exit / timeout / unparseable output is reported as execution_failed
    — the runner never classifies CAPTURED/BLOCKED itself (GameMgr does)."""
    env = dict(os.environ)
    env["CYBERARENA_VERIFICATION_ATTEMPT_ID"] = attempt_id
    try:
        proc = subprocess.run(
            [sys.executable, exploit_path],
            cwd=os.path.dirname(exploit_path) or ".",
            env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"attempt_id": attempt_id, "execution_failed": True,
                "target_reached": False, "flag_recovered": False,
                "submission_status": "none", "detail": "timeout"}
    if proc.returncode != 0:
        return {"attempt_id": attempt_id, "execution_failed": True,
                "target_reached": False, "flag_recovered": False,
                "submission_status": "none",
                "detail": f"exit {proc.returncode}: {proc.stderr[-300:]}"}
    result = _parse_result(proc.stdout)
    if result is None:
        return {"attempt_id": attempt_id, "execution_failed": True,
                "target_reached": False, "flag_recovered": False,
                "submission_status": "none", "detail": "no valid JSON result"}
    # RAW FACTS ONLY. A self-reported verdict key (`outcome`) is deliberately
    # NOT forwarded: the exploit runs on the box it is testing, so its own
    # classification carries no authority. GameMgr resolves the terminal from
    # these facts plus the correlated FLAG_SUBMITTED.
    return {
        "attempt_id": attempt_id,
        "execution_failed": False,
        "target_reached": bool(result.get("target_reached")),
        "flag_recovered": bool(result.get("flag_recovered")),
        "submission_status": str(result.get("submission_status", "none")),
    }


def main() -> int:
    verify_url = os.environ.get("CYBERARENA_VERIFY_URL", "")
    token = os.environ.get("CYBERARENA_TEAM_TOKEN", "")
    exploit_path = os.environ.get("CYBERARENA_EXPLOIT_PATH", "")
    initial_delay = _env_int("CYBERARENA_VERIFY_INITIAL_DELAY", 10)
    interval = _env_int("CYBERARENA_VERIFY_INTERVAL", 30)
    timeout = float(_env_int("CYBERARENA_VERIFY_TIMEOUT", 20))

    if not verify_url or not token:
        print("[verify_runner] CYBERARENA_VERIFY_URL and CYBERARENA_TEAM_TOKEN "
              "are required", file=sys.stderr)
        return 2
    if not exploit_path or not os.path.isfile(exploit_path):
        # Readiness is enforced host-side (fail-loud / VERIFIER_UNAVAILABLE); if
        # we somehow got here with no exploit, idle-but-alive so the supervisor
        # doesn't treat a missing exploit as a crash. Reaped at game end.
        print(f"[verify_runner] no exploit at {exploit_path!r}; idling",
              file=sys.stderr)
        while True:
            time.sleep(3600)

    time.sleep(initial_delay)
    while True:
        attempt_id = str(uuid.uuid4())
        _post(verify_url, token, {
            "event": "started", "attempt_id": attempt_id,
            "started_at": time.time(),
        })
        payload = _run_attempt(exploit_path, attempt_id, timeout)
        payload["event"] = "result"
        reply = _post(verify_url, token, payload)
        if reply.get("stop"):
            print("[verify_runner] verified-fix latch: stopping", file=sys.stderr)
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
