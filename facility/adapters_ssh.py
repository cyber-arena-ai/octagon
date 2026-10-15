"""SshExecPort — the ExecPort a CONTAINERIZED facility uses to reach prod (v3).

Where `LocalExecPort` runs `docker exec` on the host and the v2 HTTP path relayed
through GameMgr, this realizes `ExecPort` as **SSH facility→prod directly**: the
facility (flag-service / health-poller) runs the challenge's plant / probe
commands inside prod over SSH, with GameMgr out of the exec path.

Identity, not container names: the caller passes `(team_id, role)`; this resolves
`team_id → prod host` via a map the facility holds (injected at deploy) and
enforces a **role allowlist — only `prod`** (never `dev`, the agent's own box).
The private key is injected via secret/env (see facility_direct_exec_plan_v3.md);
this class only takes its path. Stdlib + the ports module only.
"""

from __future__ import annotations

import logging
import shlex
import subprocess

from .ports import ExecResult

log = logging.getLogger(__name__)

# Default ssh options: non-interactive, and don't fail/prompt on the ephemeral
# prod host keys (containers are recreated per run; we authenticate by our key,
# not by pinning theirs).
#
# Fail-fast timeouts so a plant/probe into an unreachable or mid-restart prod
# aborts in ~10s instead of hanging until the subprocess `timeout_s` backstop
# (a hung SSH used to wedge the flag-service: with the plant blocked, every
# /flags/rotate AND /game/flag/submit stalled). ConnectTimeout bounds the TCP
# handshake; ServerAlive* bounds a connection that stalls AFTER connecting
# (e.g. the remote sshd/box froze mid-command) to ~Interval*CountMax seconds.
_SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=2",
]


class SshExecPort:
    def __init__(
        self,
        *,
        key_path: str,
        host_by_team: dict[str, str],
        user: str = "root",
        runner=subprocess.run,
    ) -> None:
        self._key = key_path
        self._host_by_team = host_by_team  # team_id -> prod hostname
        self._user = user
        self._runner = runner  # injectable for tests

    def exec(
        self,
        team_id: str,
        role: str,
        cmd: str,
        *,
        timeout_s: float = 120.0,
    ) -> ExecResult:
        host = self._resolve(team_id, role)
        # ssh joins all trailing argv with spaces and the remote login shell
        # re-parses the result as ONE command line. So we must hand ssh a single
        # remote-command argument that is already a valid shell command — passing
        # ("bash", "-c", cmd) as separate args flattens to `bash -c <cmd…>`, and
        # the remote bash then takes only cmd's first word as its script (the
        # rest of cmd is dropped), e.g. `python3 -c '<probe>'` collapses to a
        # bare `python3` that reads EOF and exits 0 with empty output. Wrap cmd
        # in `bash -c <quoted>` as a single arg so the remote shell runs it whole.
        remote = f"bash -c {shlex.quote(cmd)}"
        return self._run(team_id, host, remote, cmd, timeout_s=timeout_s)

    def exec_stdin(
        self,
        team_id: str,
        role: str,
        cmd: str,
        stdin: bytes,
        *,
        timeout_s: float = 120.0,
    ) -> ExecResult:
        host = self._resolve(team_id, role)
        # The script arrives on stdin, not as an argv word, so no `bash -c`
        # wrapper is needed: `cmd` is the literal `"bash -s"` (two safe tokens,
        # no reparse hazard). `bash -s` reads its script from stdin. Wrapping it
        # would give a redundant `bash -c 'bash -s'`.
        return self._run(team_id, host, cmd, cmd, timeout_s=timeout_s, input=stdin)

    def _resolve(self, team_id: str, role: str) -> str:
        if role != "prod":
            # Role allowlist: a facility may only exec into prod, never the
            # agent's dev box or an arbitrary role.
            raise ValueError(f"SshExecPort: role {role!r} not allowed (prod only)")
        host = self._host_by_team.get(team_id)
        if not host:
            raise KeyError(f"SshExecPort: no prod host for team {team_id!r}")
        return host

    def _run(
        self,
        team_id: str,
        host: str,
        remote: str,
        cmd_for_log: str,
        *,
        timeout_s: float,
        input: bytes | None = None,
    ) -> ExecResult:
        argv = ["ssh", "-i", self._key, *_SSH_OPTS, f"{self._user}@{host}", remote]
        try:
            r = self._runner(argv, capture_output=True, timeout=timeout_s, input=input)
        except subprocess.TimeoutExpired:
            log.warning("ssh exec timed out after %gs on %s/%s: %.80s",
                        timeout_s, team_id, host, cmd_for_log)
            return ExecResult(exit_code=-1, stdout="",
                              stderr=f"ssh exec timed out after {timeout_s:g}s",
                              timed_out=True)
        return ExecResult(
            exit_code=r.returncode,
            stdout=_decode(r.stdout),
            stderr=_decode(r.stderr),
        )


def _decode(v) -> str:
    if v is None:
        return ""
    return v.decode(errors="replace") if isinstance(v, (bytes, bytearray)) else str(v)
