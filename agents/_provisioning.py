"""Shared agent host-setup primitives.

Provider-agnostic install scaffolding factored out of
`agents/claude.py` so every `AgentManager` subclass (Claude, Codex, …)
reuses one implementation instead of copy-pasting the SSH pivot, the
CLI preflight, and the host directory layout.

Everything here is a free function with its docker client + transport
injected, so the managers stay thin and tests can drive it with a fake
transport. Nothing in this module is provider-specific:

  * directory layout constants (`STATE_DIR` … `ENV_PATH`)
  * `exec_sync`            — synchronous docker-py exec
  * `ensure_dirs`          — make the host layout dirs
  * `verify_cli`           — assert the CLI the image baked is there
  * `provision_ssh_pivot`  — ed25519 dev→prod key pivot + sshd
  * `team_env_lines`       — the CYBERARENA_* + extra_env agent.env lines

Provider-specific bits (which CLI binary to expect, which API-key env var
to export, the MCP config file format) stay in each manager.

No agent CLI is installed at runtime: the image is the only install path
(see `verify_cli`).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import time

from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle

from .transport import AgentTransport

log = logging.getLogger(__name__)

# Retry budget for the runtime `npm install -g` used by not-yet-baked CLIs
# (grok, qwen). The bake-only refactor removed this for the baked CLIs
# (claude/codex/opencode use verify_cli instead), but grok/qwen aren't baked
# into the agent image yet, so they still install their CLI at game start.
_NPM_RETRY_ATTEMPTS = 3
_NPM_RETRY_BASE_SECS = 2


def npm_install_global(
    client,
    transport: AgentTransport,
    container: ContainerHandle,
    package: str,
    *,
    attempts: int = _NPM_RETRY_ATTEMPTS,
    base_secs: int = _NPM_RETRY_BASE_SECS,
) -> None:
    """`npm install -g <package>` with exponential backoff on transient
    registry errors. Raises only after the final attempt — exhaustion
    bubbles up as a normal install failure. Used by the CLIs not baked into
    the agent image (grok, qwen)."""
    cmd = ["npm", "install", "-g", package]
    for attempt in range(1, attempts + 1):
        rc, out = exec_sync(client, transport, container, cmd)
        if rc == 0:
            if attempt > 1:
                log.info("npm install %s succeeded on %s (attempt %d/%d)",
                         package, container.name, attempt, attempts)
            return
        if attempt == attempts:
            raise RuntimeError(
                f"npm install {package} failed on {container.name} after "
                f"{attempts} attempts: rc={rc}, out={out[-300:]!r}"
            )
        backoff = base_secs * (2 ** (attempt - 1))
        log.warning(
            "npm install %s failed on %s (attempt %d/%d, rc=%d); retrying in %ds. tail=%r",
            package, container.name, attempt, attempts, rc, backoff, out[-200:],
        )
        time.sleep(backoff)

# Layout inside the agent host. WORKDIR is the cwd pinned per phase so a
# CLI's session store (`claude --continue` / `codex exec resume`) stays
# isolated to this arena.
STATE_DIR = "/var/lib/cyberarena"
LOG_DIR = "/var/log/cyberarena"
RUN_DIR = "/var/run/cyberarena"
WORKDIR = f"{STATE_DIR}/workdir"
EXPLOIT_DIR = f"{STATE_DIR}/exploits"

PROMPT_PATH = f"{STATE_DIR}/prompt.txt"
ENV_PATH = f"{STATE_DIR}/agent.env"

# The source path named in the source_in_dev prompt. The dev copy is rooted
# below the same path exposed by prod so prompt and provisioning cannot drift.
DEV_SOURCE_ROOT = "/srv/challenge"

_SOURCE_SKIP_DIRS = frozenset({"__pycache__", ".git"})
_SOURCE_EXCLUDE_NAMES = frozenset(
    {
        "flag",
        "flag.txt",
        "readflag",
        "checker.sh",
        "restart.sh",
        "scoring.sh",
        "score.sh",
        "entrypoint.sh",
    }
)


def _is_excluded_source_member(rel_parts: tuple[str, ...]) -> bool:
    """Reject unsafe paths plus harness, secret, scoring, VCS, and bytecode."""
    if not rel_parts or any(part in {"", ".", ".."} for part in rel_parts):
        return True
    if any(part in _SOURCE_SKIP_DIRS for part in rel_parts):
        return True
    return rel_parts[-1] in _SOURCE_EXCLUDE_NAMES


def provision_dev_source_from_prod(
    client,
    transport: AgentTransport,
    container: ContainerHandle,
    *,
    service_name: str,
    prod_container_name: str,
) -> str:
    """Stage prod's materialized vulnerable source in the agent dev box.

    Challenge entrypoints have already assembled the authoritative application
    below /srv/challenge in prod. Copy that tree into
    /srv/challenge/<service_name>/ in dev, which is the exact location named
    by the prompt, while excluding harness, secret, scoring, VCS, and bytecode
    material.
    """
    if client is None:
        raise RuntimeError(
            "provision_dev_source_from_prod needs a docker client to read "
            "the prod source tree"
        )

    prod = client.containers.get(prod_container_name)
    try:
        chunks, _stat = prod.get_archive(DEV_SOURCE_ROOT)
    except Exception as exc:
        raise FileNotFoundError(
            f"source_in_dev: prod container {prod_container_name!r} has no "
            f"{DEV_SOURCE_ROOT} tree to stage for {service_name!r}: {exc}"
        ) from exc

    raw = io.BytesIO()
    for chunk in chunks:
        raw.write(chunk)
    raw.seek(0)

    dest_root = f"{DEV_SOURCE_ROOT}/{service_name}"
    exec_sync(
        client,
        transport,
        container,
        ["mkdir", "-p", dest_root],
        check=True,
    )

    written = 0
    excluded: list[str] = []
    # Re-pack the surviving members into ONE archive and ship it in a single
    # `write_tree`. Writing them out one at a time cost a docker round trip AND
    # an in-container `mkdir -p` exec PER FILE, which does not scale with a real
    # application's source tree: jenkins-controller-write (44,931 files),
    # vikunja (47,157) and nginx-ui (47,527) each spent ~2,000s here — and the
    # tree is staged twice, once per team, so ~90k files became ~270k round
    # trips. That time is invisible in the run's budget because it is spent
    # BEFORE ROUND_START, so `round_secs` does not bound it and the job simply
    # looks like it is hanging.
    #
    # Spooled rather than BytesIO so a large tree spills to disk instead of
    # being held in memory a second time on top of `raw`.
    staged = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    try:
        with tarfile.open(fileobj=raw, mode="r:*") as archive, \
                tarfile.open(fileobj=staged, mode="w") as out:
            for member in archive:
                if not member.isfile():
                    continue
                # Docker roots get_archive members at the requested basename
                # ("challenge/..."). Strip it before re-rooting under service_name.
                parts = tuple(member.name.split("/"))
                if parts[:1] == (".",):
                    parts = parts[1:]
                if parts[:1] != ("challenge",):
                    continue
                rel_parts = parts[1:]
                if _is_excluded_source_member(rel_parts):
                    if rel_parts and rel_parts[-1] in _SOURCE_EXCLUDE_NAMES:
                        excluded.append("/".join(rel_parts))
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                # Re-rooted under dest_root by the extract, not by the name, so
                # mode and mtime carry over and the staged copy reads like the
                # tree it came from.
                data = extracted.read()
                info = tarfile.TarInfo(name="/".join(rel_parts))
                info.size = len(data)
                info.mode = member.mode
                info.mtime = member.mtime
                out.addfile(info, io.BytesIO(data))
                written += 1
        if written:
            staged.seek(0)
            transport.write_tree(container, dest_root, staged)
    finally:
        staged.close()

    if excluded:
        log.warning(
            "source_in_dev: excluded %d harness/flag/scoring file(s) from "
            "%s dev source: %s",
            len(excluded),
            service_name,
            ", ".join(sorted(excluded)),
        )
    log.info(
        "source_in_dev: staged %d source file(s) into %s at %s (from %s)",
        written,
        container.name,
        dest_root,
        prod_container_name,
    )
    return dest_root


def exec_sync(
    client,
    transport: AgentTransport,
    container: ContainerHandle,
    cmd: list[str] | str,
    *,
    check: bool = False,
) -> tuple[int, str]:
    """Synchronous exec via docker-py. Used during install where we
    don't need async + timeout semantics (the transport's run_command
    is the async path for per-phase work).

    When `client` is None (transport-only injection in tests) this falls
    back to a synchronous transport invocation via asyncio.run.
    """
    if client is None:
        result = asyncio.run(transport.run_command(container, cmd))
        out = (result.stdout + result.stderr).decode(errors="replace")
        if check and result.exit_code != 0:
            raise RuntimeError(
                f"exec failed ({cmd!r}) on {container.name}: rc={result.exit_code}, out={out!r}"
            )
        return result.exit_code, out

    c = client.containers.get(container.name)
    result = c.exec_run(cmd, demux=False)
    out = result.output.decode(errors="replace") if result.output else ""
    if check and result.exit_code != 0:
        raise RuntimeError(
            f"exec failed ({cmd!r}) on {container.name}: rc={result.exit_code}, out={out!r}"
        )
    return result.exit_code, out


def ensure_dirs(
    client,
    transport: AgentTransport,
    container: ContainerHandle,
    *,
    extra: tuple[str, ...] = (),
) -> None:
    """Make the host layout dirs (+ any provider-specific `extra`)."""
    exec_sync(
        client, transport, container,
        ["mkdir", "-p", STATE_DIR, LOG_DIR, RUN_DIR, WORKDIR, EXPLOIT_DIR, *extra],
        check=True,
    )


def exploit_install_path(service_name: str) -> str:
    """Canonical path for a challenge PoC exploit inside an agent container."""
    return f"{EXPLOIT_DIR}/{service_name}/_exploit.py"


def exploit_support_files(exploit_path: Path) -> list[Path]:
    """Files copied next to `_exploit.py` in the agent container.

    The exploit entrypoint is the contract. Same-directory underscore modules
    (`_*.py`, which includes `__init__.py`) are allowed as private helpers so
    package-local imports keep working after provisioning, without copying the
    whole challenge package.
    """
    pkg_dir = exploit_path.parent
    files = {exploit_path}
    files.update(p for p in pkg_dir.glob("_*.py") if p.is_file())
    return sorted(files, key=lambda p: p.name)


def provision_exploit(
    client,
    transport: AgentTransport,
    container: ContainerHandle,
    *,
    service_name: str,
    exploit_path: Path,
) -> str:
    """Copy a challenge's `_exploit.py` and local helpers into the agent.

    This uses the same transport file-write path as agent.env and SSH keys, so
    exploit delivery works for Docker today and remains behind AgentTransport
    when a future SSH backend is added.
    """
    if not exploit_path.is_file():
        raise FileNotFoundError(f"missing exploit for {service_name}: {exploit_path}")
    dest = exploit_install_path(service_name)
    dest_dir = os.path.dirname(dest)
    exec_sync(
        client, transport, container,
        ["mkdir", "-p", dest_dir],
        check=True,
    )
    for src in exploit_support_files(exploit_path):
        transport.write_file(container, f"{dest_dir}/{src.name}", src.read_bytes())
    exec_sync(client, transport, container, ["chmod", "755", dest], check=True)
    return dest


def verify_cli(
    client,
    transport: AgentTransport,
    container: ContainerHandle,
    binary: str,
    *,
    expected_version: str | None = None,
    version_flag: str = "--version",
) -> None:
    """Assert the agent CLI is present in the image. Installs NOTHING.

    The image (`dockerfiles/agent/<provider>/Dockerfile`, on top of
    `dockerfiles/agent/base`) is the only install path. Adapters
    used to `npm install -g` the same pinned version again at game start, once per
    team: a registry round-trip that re-extracted a package the image had already
    baked, and that forced an isolated network to keep an npm egress hole open for
    it. `install()` now only checks what the image shipped.

    Missing binary → RuntimeError, so a stale/mis-built image fails loudly at
    install time instead of as a `command not found` in the middle of a phase.
    Version mismatch against the pin → WARNING only: the match can still run, but
    the drift is on the record (the image and the pin are synced by hand).
    """
    rc, _ = exec_sync(
        client, transport, container,
        ["sh", "-c", f"command -v {shlex.quote(binary)}"],
    )
    if rc != 0:
        raise RuntimeError(
            f"{binary!r} is not in {container.name}'s image, and the orchestrator no "
            f"longer installs agent CLIs at runtime. Each harness is baked into its "
            f"own cyberarena/agent-<provider> image (dockerfiles/agent/<provider>/"
            f"Dockerfile): check the dev node's `image:` names this provider's, and "
            f"rebuild with `bash scripts/build_images.sh`."
        )
    if not expected_version:
        return
    rc, out = exec_sync(client, transport, container, [binary, version_flag])
    got = " ".join(out.split())
    if rc != 0 or expected_version not in got:
        log.warning(
            "%s: image ships %s %r but the pin expects %s — image is stale relative "
            "to the code. Rebuild with `bash scripts/build_images.sh`.",
            container.name, binary, got, expected_version,
        )


def team_env_lines(cfg: AgentConfig) -> list[str]:
    """The provider-agnostic agent.env lines: team identity, attack
    topology, submit URL, and the extra_env escape hatch.

    Each manager prepends its provider-specific lines (the API-key env
    var, model, base URL) before these.
    """
    lines: list[str] = []
    if cfg.team_id:
        lines.append(f"export CYBERARENA_TEAM_ID={shlex.quote(cfg.team_id)}")
    if cfg.team_token:
        lines.append(f"export CYBERARENA_TEAM_TOKEN={shlex.quote(cfg.team_token)}")
    if cfg.own_prod_host:
        lines.append(f"export CYBERARENA_OWN_PROD={shlex.quote(cfg.own_prod_host)}")
    if cfg.opposing_prod_hosts:
        lines.append(
            "export CYBERARENA_OPPOSING_PRODS="
            + shlex.quote(",".join(cfg.opposing_prod_hosts))
        )
    if cfg.submit_url:
        lines.append(f"export CYBERARENA_SUBMIT_URL={shlex.quote(cfg.submit_url)}")
    for k, v in cfg.extra_env.items():
        lines.append(f"export {k}={shlex.quote(v)}")
    return lines


def provision_ssh_pivot(
    client,
    transport: AgentTransport,
    dev_container: ContainerHandle,
    cfg: AgentConfig,
) -> None:
    """Generate an ed25519 keypair on the host, push the pubkey to
    own_prod's authorized_keys, drop the privkey + ssh config into dev.
    Ephemeral key per install() — never persisted.

    Detects the prod-side SSH user at runtime: `arena_agent` if the
    target image carries that non-root user (every per-challenge image
    bakes it — cyberarena/chal-notes, cyberarena/chal-nautilus-ico),
    else falls back to root for any external/legacy image that doesn't
    follow the convention.
    """
    prod_name = cfg.own_prod_container_name
    log.info("provisioning ssh pivot %s -> %s", dev_container.name, prod_name)

    with tempfile.TemporaryDirectory(prefix="cyberarena-keys-") as keydir:
        priv = os.path.join(keydir, "id_ed25519")
        pub = priv + ".pub"
        r = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", priv, "-q", "-C", f"agent-{cfg.team_id}"],
            check=False, capture_output=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ssh-keygen failed: {r.stderr.decode(errors='replace')!r}")
        with open(priv) as f:
            privkey = f.read()
        with open(pub) as f:
            pubkey = f.read()

    prod_container = ContainerHandle(container_id="", name=prod_name, networks=[])
    rc, _ = exec_sync(client, transport, prod_container, ["id", "arena_agent"])
    has_arena_agent = rc == 0

    # Grant the agent ROOT in its OWN prod box: install the pubkey for root
    # (always) and for arena_agent (the documented source-edit user, when the
    # image bakes it), then enable root SSH login below. The defender can then
    # do anything root can on its own service — edit root-owned files, install
    # tooling, even run /arena/restart.sh directly. The MCP `restart_service`
    # tool + prompt stay the RECOMMENDED path (only MCP emits restart events and
    # re-probes health); with root we simply can no longer *prevent* a direct
    # restart.sh, which is an accepted trade-off. Own-prod only — a team's key
    # is never installed on any other team's prod box.
    key_targets = [("root", "/root")]
    if has_arena_agent:
        key_targets.append(("arena_agent", "/home/arena_agent"))
    line = pubkey.strip()
    for user, home in key_targets:
        ssh_dir = f"{home}/.ssh"
        ak = f"{ssh_dir}/authorized_keys"
        exec_sync(client, transport, prod_container, ["mkdir", "-p", ssh_dir], check=True)
        exec_sync(client, transport, prod_container, ["chmod", "700", ssh_dir], check=True)
        # APPEND (idempotent), not overwrite — a prod box also carries the v3
        # facility SSH key (provision_facility_ssh); clobbering it would break
        # flag plant / health probe. grep -qF || echo keeps both keys, no dup.
        exec_sync(
            client, transport, prod_container,
            ["sh", "-c",
             f"touch {ak} && grep -qF {shlex.quote(line)} {ak} "
             f"|| echo {shlex.quote(line)} >> {ak}"],
            check=True,
        )
        exec_sync(client, transport, prod_container, ["chmod", "600", ak], check=True)
        if user != "root":
            exec_sync(client, transport, prod_container, ["chown", "-R", f"{user}:{user}", ssh_dir], check=True)

    # Images ship `PermitRootLogin no`; flip it on so the agent lands as root.
    exec_sync(
        client, transport, prod_container,
        ["sed", "-i", "s/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/",
         "/etc/ssh/sshd_config"],
        check=True,
    )
    log.info("granted root ssh on %s (own-prod, team %s)", prod_name, cfg.team_id)

    exec_sync(client, transport, prod_container, ["mkdir", "-p", "/run/sshd"], check=True)
    exec_sync(client, transport, prod_container, ["ssh-keygen", "-A"], check=True)
    exec_sync(client, transport, prod_container, ["/usr/sbin/sshd"], check=True)
    # sshd is PID 1 (entrypoint exec's `sshd -D`); SIGHUP makes the master
    # re-read sshd_config (picking up PermitRootLogin) without dropping the
    # container. Tolerant: an image whose PID 1 isn't sshd must not break install.
    exec_sync(client, transport, prod_container, ["sh", "-c", "kill -HUP 1 2>/dev/null || true"])

    exec_sync(client, transport, dev_container, ["mkdir", "-p", "/root/.ssh"], check=True)
    exec_sync(client, transport, dev_container, ["chmod", "700", "/root/.ssh"], check=True)
    transport.write_file(dev_container, "/root/.ssh/id_ed25519", privkey.encode())
    exec_sync(client, transport, dev_container, ["chmod", "600", "/root/.ssh/id_ed25519"], check=True)
    # Default the agent to root on its own prod box (root login enabled above);
    # a bare `ssh {own_prod_host}` lands as root. arena_agent is also authorized,
    # so `ssh arena_agent@{host}` still works for prompts that name it.
    ssh_config = (
        f"Host {cfg.own_prod_host}\n"
        f"    User root\n"
        f"    IdentityFile /root/.ssh/id_ed25519\n"
        f"    StrictHostKeyChecking no\n"
        f"    UserKnownHostsFile /dev/null\n"
        f"    LogLevel ERROR\n"
    )
    transport.write_file(dev_container, "/root/.ssh/config", ssh_config.encode())
    exec_sync(client, transport, dev_container, ["chmod", "600", "/root/.ssh/config"], check=True)


def provision_facility_ssh(
    client,
    transport: AgentTransport,
    prod_containers: list[ContainerHandle],
    facility_containers: list[ContainerHandle],
    *,
    key_path: str = "/keys/facility",
    comment: str = "cyberarena-facility",
) -> str:
    """v3 facility direct-exec: one facility ed25519 keypair authorized for root
    on EVERY prod, with the privkey dropped into each facility container.

    The flag-service plants and the health-poller probes by SSHing into prod
    (SshExecPort) instead of relaying through GameMgr. One shared key reaches all
    prod boxes (role allowlist + identity resolution happen in SshExecPort).

    Coexistence + ordering: `provision_ssh_pivot` OVERWRITES root's
    authorized_keys for a team's own prod, so this APPENDS its line (idempotent)
    and MUST run AFTER the agent pivots — otherwise a later pivot would clobber
    the facility key. Returns the pubkey (for logging/inspection); the privkey is
    written only into the facility containers, never persisted host-side.
    """
    with tempfile.TemporaryDirectory(prefix="cyberarena-fac-keys-") as keydir:
        priv = os.path.join(keydir, "id_ed25519")
        pub = priv + ".pub"
        r = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", priv, "-q", "-C", comment],
            check=False, capture_output=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ssh-keygen failed: {r.stderr.decode(errors='replace')!r}")
        with open(priv) as f:
            privkey = f.read()
        with open(pub) as f:
            pubkey = f.read()

    line = pubkey.strip()
    for prod in prod_containers:
        ssh_dir = "/root/.ssh"
        ak = f"{ssh_dir}/authorized_keys"
        exec_sync(client, transport, prod, ["mkdir", "-p", ssh_dir], check=True)
        exec_sync(client, transport, prod, ["chmod", "700", ssh_dir], check=True)
        # Append idempotently — preserve the agent pivot's key, don't duplicate ours.
        exec_sync(
            client, transport, prod,
            ["sh", "-c",
             f"touch {ak} && grep -qF {shlex.quote(line)} {ak} "
             f"|| echo {shlex.quote(line)} >> {ak}"],
            check=True,
        )
        exec_sync(client, transport, prod, ["chmod", "600", ak], check=True)
        # Ensure root SSH login + a live sshd (idempotent; the pivot may have done
        # this for prods that have an agent — but not every prod necessarily does).
        exec_sync(
            client, transport, prod,
            ["sed", "-i", "s/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/",
             "/etc/ssh/sshd_config"],
            check=True,
        )
        exec_sync(client, transport, prod, ["mkdir", "-p", "/run/sshd"], check=True)
        exec_sync(client, transport, prod, ["ssh-keygen", "-A"], check=True)
        exec_sync(client, transport, prod, ["sh", "-c", "kill -HUP 1 2>/dev/null || true"])

    key_dir = os.path.dirname(key_path) or "/keys"
    for fac in facility_containers:
        exec_sync(client, transport, fac, ["mkdir", "-p", key_dir], check=True)
        transport.write_file(fac, key_path, privkey.encode())
        exec_sync(client, transport, fac, ["chmod", "600", key_path], check=True)

    log.info(
        "provisioned facility ssh key on %d prod box(es), %d facility container(s)",
        len(prod_containers), len(facility_containers),
    )
    return pubkey
