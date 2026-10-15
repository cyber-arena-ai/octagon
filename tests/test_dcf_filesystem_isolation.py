"""DEFCON: per-container filesystem isolation.

Pins the rule the framework relies on: no host bind mount.
Concretely:
- A write to /srv/challenge/ico/foo inside container A is INVISIBLE
  to container B (same image, different container instance).
- That same write is INVISIBLE to vendor/nautilus-finals-2025/ico/service/
  on the host — the upstream source tree never sees agent edits.

If either invariant fails, a defense edit by team1 would leak into team2
(losing the per-team isolation that makes A/D meaningful) or pollute
the vendor tree (poisoning subsequent container starts).

Two containers from the same image stand in for team1_prod + team2_prod —
the existing M5.6 topology doesn't share mounts between team containers,
so this test directly proves the property regardless of orchestrator state.
"""

from __future__ import annotations

import subprocess
import time

import pytest


def _exec(name: str, cmd: list[str], *, user: str | None = None) -> subprocess.CompletedProcess:
    args = ["docker", "exec"]
    if user:
        args += ["--user", user]
    args += [name, *cmd]
    return subprocess.run(args, capture_output=True, text=True)


@pytest.fixture
def two_ico_containers(dcf_ico_image):
    """Two fresh containers from the same image. Yields (name_a, name_b)."""
    names = [f"dcf-iso-a-{int(time.time())}", f"dcf-iso-b-{int(time.time())}"]
    for n in names:
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)
        subprocess.run(
            ["docker", "run", "-d", "--name", n, "--platform=linux/amd64", dcf_ico_image],
            check=True, capture_output=True,
        )
    # Wait for entrypoints to finish.
    for n in names:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _exec(n, ["pgrep", "sshd"]).returncode == 0:
                break
            time.sleep(0.3)
    yield names[0], names[1]
    for n in names:
        subprocess.run(["docker", "rm", "-f", n], capture_output=True)


# ── Cross-container isolation ──────────────────────────────────────────


def test_write_in_one_container_invisible_in_another(two_ico_containers):
    """The core per-team isolation invariant. Container A's edit to
    /srv/challenge/ico/foo MUST NOT appear in container B."""
    a, b = two_ico_containers

    sentinel = "team1-only-patch-9c3f7b"
    r = _exec(
        a,
        ["bash", "-c", f"echo {sentinel!r} > /srv/challenge/ico/leak_canary.txt"],
        user="arena_agent",
    )
    assert r.returncode == 0, f"write in container A failed: {r.stderr}"

    # Container B must not have the file (or must not have its content).
    rb = _exec(b, ["cat", "/srv/challenge/ico/leak_canary.txt"])
    assert rb.returncode != 0, (
        f"container B sees container A's file! Filesystem isolation broken. "
        f"stdout={rb.stdout!r}"
    )


def test_write_in_one_does_not_modify_the_other_existing_file(two_ico_containers):
    """Stronger form: patching ico.pas in A must not show up in B's
    ico.pas (would imply a shared bind mount)."""
    a, b = two_ico_containers

    marker = "{ DCF-ISO-TEST-MARKER-A }"
    r = _exec(
        a,
        ["bash", "-c", f"echo '{marker}' >> /srv/challenge/ico/ico.pas"],
        user="arena_agent",
    )
    assert r.returncode == 0

    rb = _exec(b, ["grep", "-F", marker, "/srv/challenge/ico/ico.pas"])
    assert rb.returncode != 0, (
        "container B's ico.pas was modified by container A's edit — shared mount detected"
    )


# ── Host-vendor isolation ──────────────────────────────────────────────


def test_write_in_container_does_not_touch_host_vendor_dir(dcf_ico_container):
    """Defense edits inside the container MUST NOT leak back into
    vendor/nautilus-finals-2025/ico/service/. That dir is the pristine
    pin; subsequent builds inherit from it, so leakage would poison all
    future containers + git would see modifications in a `vendor/`
    subtree that's supposed to be byte-identical to the pinned SHA.

    No bind mount on `/srv/challenge/ico/` in the run command (see
    conftest) — this test confirms the implementation matches that
    intent by reading the host file before + after a container write.
    """
    from pathlib import Path

    # Locate the host's vendored ico.pas. The path is stable per
    # scripts/sync_nautilus.sh.
    host_repo_root = Path(__file__).resolve().parent.parent
    vendor_ico_pas = host_repo_root / "vendor" / "nautilus-finals-2025" / "ico" / "service" / "ico.pas"
    if not vendor_ico_pas.is_file():
        pytest.skip(f"vendor not synced: {vendor_ico_pas}")

    before = vendor_ico_pas.read_bytes()

    name = dcf_ico_container
    r = _exec(
        name,
        ["bash", "-c", "echo '{ host-leak-canary }' >> /srv/challenge/ico/ico.pas"],
        user="arena_agent",
    )
    assert r.returncode == 0

    after = vendor_ico_pas.read_bytes()
    assert before == after, (
        f"host vendor file changed after container write — bind-mount leak. "
        f"Path: {vendor_ico_pas}"
    )
