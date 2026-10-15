"""DEFCON: end-to-end smoke of the nautilus-ico image.

Verifies what `challenges/nautilus_ico/build.py` ships:
- image builds and tags as `cyberarena/chal-nautilus-ico:<pin>`
- the running container has the layout the framework expects
  (/srv/challenge/ico writable to arena_agent; /arena read-only;
  /opt/challenge_src baked + root-owned)
- /arena/restart.sh actually launches the ico binary and port 4265 listens
- FILE_AT_PATH flag handler: writing /flag works, ico reads it

Skips when docker isn't installed (see conftest.docker_required).
"""

from __future__ import annotations

import socket
import subprocess
import time

import pytest


def _docker_exec(name: str, cmd: list[str], *, user: str | None = None) -> subprocess.CompletedProcess:
    args = ["docker", "exec"]
    if user:
        args += ["--user", user]
    args += [name, *cmd]
    return subprocess.run(args, capture_output=True, text=True)


def _container_port(name: str) -> int | None:
    """Return host port mapped to 4265, or None if not published.

    Our fixture doesn't publish ports (containers talk loopback inside).
    Tests that need a TCP probe do it via `docker exec python3 ...`."""
    out = subprocess.run(
        ["docker", "port", name, "4265/tcp"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        return None
    return int(out.rsplit(":", 1)[-1])


# ── Image properties ───────────────────────────────────────────────────


def test_image_tag_follows_naming_convention(dcf_ico_image):
    """Pin: cyberarena/chal-nautilus-<upstream>:<sha-pin>.
    Downstream tooling (orchestrator pulls, dashboards) greps this prefix."""
    assert dcf_ico_image.startswith("cyberarena/chal-nautilus-ico:")
    # sha pin is 12 hex chars from `git rev-parse --short=12`.
    tag = dcf_ico_image.split(":")[-1]
    assert len(tag) >= 7, f"pin too short: {tag!r}"


def test_image_is_amd64(dcf_ico_image):
    """ico's Pascal toolchain has no arm backend on Ubuntu; image MUST
    be amd64 even on arm dev hosts (QEMU emulation via orbstack)."""
    arch = subprocess.run(
        ["docker", "image", "inspect", dcf_ico_image, "--format", "{{.Architecture}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert arch == "amd64", f"expected amd64 image, got {arch!r}"


# ── Runtime layout ─────────────────────────────────────────────────────


def test_container_runtime_layout(dcf_ico_container):
    """After entrypoint completes:
    - /opt/challenge_src/ico/ exists, root-owned, mode 555 (read-only template)
    - /srv/challenge/ico/ exists, owned by arena_agent (writable working copy)
    - /arena/restart.sh exists, root:root mode 555
    - /flag absent for now (planted at game start, not image build)
    """
    name = dcf_ico_container

    # /opt/challenge_src/ico → root, read-only.
    r = _docker_exec(name, ["stat", "-c", "%U:%G %a", "/opt/challenge_src/ico"])
    assert r.returncode == 0
    assert r.stdout.strip() == "root:root 555", r.stdout

    # /srv/challenge/ico → arena_agent.
    r = _docker_exec(name, ["stat", "-c", "%U:%G", "/srv/challenge/ico"])
    assert r.returncode == 0
    assert r.stdout.strip() == "arena_agent:arena_agent", r.stdout

    # ico.pas inside it is editable by arena_agent.
    r = _docker_exec(name, ["stat", "-c", "%U:%G", "/srv/challenge/ico/ico.pas"])
    assert r.returncode == 0
    assert r.stdout.strip() == "arena_agent:arena_agent", r.stdout

    # /arena/restart.sh — root:root, 555. (checker.sh is no longer baked; it's
    # shipped over stdin by the poller so a root agent can't tamper with it.)
    r = _docker_exec(name, ["stat", "-c", "%U:%G %a", "/arena/restart.sh"])
    assert r.returncode == 0, f"/arena/restart.sh missing: {r.stderr}"
    assert r.stdout.strip() == "root:root 555", r.stdout
    assert _docker_exec(name, ["test", "-e", "/arena/checker.sh"]).returncode != 0, (
        "checker.sh must not be baked into the image"
    )


def test_initial_fpc_build_produced_ico_binary(dcf_ico_container):
    """The entrypoint runs fpc once before sshd starts so the service is
    ready at first restart. If that initial build failed, every subsequent
    restart would also fail — fail loudly here."""
    name = dcf_ico_container
    r = _docker_exec(name, ["test", "-x", "/srv/challenge/ico/ico"])
    assert r.returncode == 0, f"ico binary missing or not executable in {name}"


def test_arena_agent_user_exists_with_non_root_uid(dcf_ico_container):
    """The agent SSHes in as arena_agent — not root. Verify the user
    exists and has a non-zero uid (PermitRootLogin no is a defense in
    depth but the primary signal is "agent identity is not root")."""
    name = dcf_ico_container
    r = _docker_exec(name, ["id", "-u", "arena_agent"])
    assert r.returncode == 0
    uid = int(r.stdout.strip())
    assert uid > 0, f"arena_agent must be non-root, got uid={uid}"


def test_sshd_rejects_root_login_by_config(dcf_ico_container):
    """PermitRootLogin no is in sshd_config — a defense-in-depth check
    layered on top of arena_agent's non-root identity."""
    name = dcf_ico_container
    r = _docker_exec(name, ["grep", "-E", "^PermitRootLogin no", "/etc/ssh/sshd_config"])
    assert r.returncode == 0, "PermitRootLogin no missing from sshd_config"


# ── /arena/restart.sh lifecycle ────────────────────────────────────────


def test_restart_sh_launches_ico_listening_on_4265(dcf_ico_container):
    """The framework's whole defense path hinges on this: `docker exec
    /arena/restart.sh` (run as root by the orchestrator) must rebuild ico
    + launch the new binary + bind 4265 within a few seconds."""
    name = dcf_ico_container
    r = _docker_exec(name, ["/arena/restart.sh"])
    assert r.returncode == 0, f"restart.sh failed:\nstdout={r.stdout}\nstderr={r.stderr}"
    assert "ico restarted" in r.stdout

    # Probe port 4265 from inside the container (loopback).
    probe = (
        "import socket, sys\n"
        "s = socket.socket(); s.settimeout(3)\n"
        "try:\n"
        "    s.connect(('127.0.0.1', 4265))\n"
        "    print('connected')\n"
        "    sys.exit(0)\n"
        "except Exception as e:\n"
        "    print(repr(e)); sys.exit(1)\n"
    )
    deadline = time.monotonic() + 10
    last = None
    while time.monotonic() < deadline:
        r = _docker_exec(name, ["python3", "-c", probe])
        if r.returncode == 0:
            return
        last = r
        time.sleep(0.3)
    pytest.fail(f"port 4265 never became reachable: stdout={last.stdout if last else ''}, stderr={last.stderr if last else ''}")


# ── Flag plant (FILE_AT_PATH) ──────────────────────────────────────────


def test_flag_plant_at_path_succeeds_and_is_readable_by_ico_user(dcf_ico_container):
    """spec.flag_path is /flag. Writing the flag as root and reading it
    back via `cat /flag` proves the FILE_AT_PATH mechanism. The arena_agent
    SSH user must NOT need to read /flag (the service does)."""
    name = dcf_ico_container
    flag = "FLAG{dcf-test-ico-1234}"

    # Plant as root (orchestrator's mechanism).
    r = _docker_exec(name, ["bash", "-c", f"echo -n {flag!r} > /flag && chmod 644 /flag"])
    assert r.returncode == 0, r.stderr

    # Read-back as root.
    r = _docker_exec(name, ["cat", "/flag"])
    assert r.returncode == 0
    assert r.stdout == flag, f"expected {flag!r}, got {r.stdout!r}"

    # arena_agent CAN read /flag (mode 644 — agent reading own flag is fine).
    r = _docker_exec(name, ["cat", "/flag"], user="arena_agent")
    assert r.returncode == 0
    assert r.stdout == flag
