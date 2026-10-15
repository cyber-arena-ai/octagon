"""ClaudeAgentManager — Claude Code adapter for the AgentManager ABC.

Two responsibilities (see docs/persistent_memory_agent_plan_v1.md):

  install(handle, cfg)            # once per team at game start: preflight
                                  # the baked CLI, SSH pivot, env, mcp.json.
                                  # No process launched.
  phase_command(model=None)       # returns the shell argv that
                                  # invokes `claude -p --continue ...`
                                  # consuming the prompt from PROMPT_PATH.

The per-phase dance (write prompt → run_command → kill on timeout)
lives in `AgentManager.run_phase`. GameMgr owns artifact persistence
+ AGENT_* event emission via `record_phase_started` /
`record_phase_done`.

All side effects on the agent host go through `AgentTransport`.

Memory across phases carries via Claude CLI's `--continue` flag: the
CLI's session store lives at /root/.claude/projects/<workdir-cwd>/
inside the container. We pin the cwd to STATE_DIR/workdir so
different arenas' sessions stay isolated.

`status` / `kill` / `fetch_logs` are concrete-only convenience
methods kept for live smoke tests + scripts/m6_screenshot_demo.py.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Callable, ClassVar

import docker

from models.agent import AgentConfig, AgentLogs, AgentState, AgentStatus
from orchestrator.handle import ContainerHandle

from . import _provisioning as prov
from ._provisioning import (
    ENV_PATH,
    LOG_DIR,
    PROMPT_PATH,
    RUN_DIR,
    STATE_DIR,
    WORKDIR,
)
from .manager import AgentManager
from .transport import AgentHandle, AgentTransport
from .transport_docker import DockerExecBackend

log = logging.getLogger(__name__)

# PINNED claude-code CLI version. Installed ONLY by dockerfiles/agent/Dockerfile;
# this constant is what `install` expects to find there (`prov.verify_cli` warns on
# a mismatch, so keep the two in sync). Unpinned `@anthropic-ai/
# claude-code` follows npm `latest`; 2.1.208 began round-tripping opus's
# `redacted_thinking` blocks that the upstream rejects (400 "Invalid `data` in
# `redacted_thinking` block") -> opus process exits 1 -> supervisor restart ->
# crash-loop (3-7x/match, ~50% wall wasted). 2.1.207 is the last version this
# harness ran clean on (07-11: single-chunk, no redacted_thinking). Bumping it is
# a deliberate, tested change — never track latest. (CODEX_VERSION is pinned the
# same way, but was deliberately bumped ahead of verification; see its comment.)
CLAUDE_CODE_VERSION = "2.1.207"

# Claude-local host paths. The shared layout (STATE_DIR / LOG_DIR /
# RUN_DIR / WORKDIR / PROMPT_PATH / ENV_PATH) is imported from
# _provisioning so every agent CLI shares one directory convention.
PHASE_CONTEXT_PATH = f"{STATE_DIR}/phase_context.json"
MCP_CONFIG_PATH = f"{STATE_DIR}/mcp.json"

# Legacy paths — kept so fetch_logs / status remain meaningful for any
# code path that still reads them. New code should look at the
# per-phase stream-{phase}.jsonl / agent-{phase}.log artifacts in the
# host's run directory instead.
PID_PATH = f"{RUN_DIR}/agent.pid"
WRAPPER_PID_PATH = f"{RUN_DIR}/agent.wrapper.pid"
EXIT_CODE_PATH = f"{RUN_DIR}/agent.exit_code"
STDOUT_PATH = f"{LOG_DIR}/agent.stdout"
STDERR_PATH = f"{LOG_DIR}/agent.stderr"

# install() callback. Fires after env / mcp.json / ssh pivot / workdir
# are staged. Claude is spawned per-phase by run_phase, so this is the
# only install-side lifecycle hook.
OnInstalled = Callable[[str], None]


class ClaudeAgentManager(AgentManager):
    """Host-side controller. One instance handles every team; each
    method takes a ContainerHandle for the team's dev container.

    The transport is injected (DockerExecBackend by default). Tests
    can pass a FakeTransport that records calls in-memory.
    """

    # ABC contract (`agents.manager.AgentManager`).
    PROMPT_PATH: ClassVar[str] = PROMPT_PATH
    PHASE_KILL_PATTERN: ClassVar[str] = "claude -p"
    # `agent.cli` selects this adapter; the command is fixed in
    # phase_command, so only the leading "claude" token is honored.
    IMAGE: ClassVar[str] = "cyberarena/agent-claude"
    CANONICAL_CLI: ClassVar[str | None] = "claude"
    REQUIRED_CLI: ClassVar[str | None] = "claude"

    def __init__(
        self,
        *,
        transport: AgentTransport | None = None,
        client: docker.DockerClient | None = None,
    ) -> None:
        # Back-compat: existing tests construct `ClaudeAgentManager(client=mock)`.
        # If only client is given, synthesize a DockerExecBackend from it.
        if transport is not None:
            self._transport = transport
            self._client = client  # may be None; some legacy methods need it
        else:
            self._client = client or docker.from_env()
            self._transport = DockerExecBackend(client=self._client)

    # ── public API ──────────────────────────────────────────────────────

    def install(
        self,
        container: ContainerHandle,
        cfg: AgentConfig,
        *,
        on_installed: OnInstalled | None = None,
    ) -> None:
        """One-shot setup per team. Idempotent (SSH key re-push is fine,
        etc.) but designed to be called exactly once per game by the
        orchestrator.

        Steps:
          1. Make STATE_DIR / LOG_DIR / RUN_DIR / WORKDIR.
          2. Verify the image baked `claude` (installs nothing — see
             `prov.verify_cli`; skipped when REQUIRED_CLI is None).
          3. SSH pivot if cfg.own_prod_container_name set.
          4. Write agent.env (no PROMPT_PATH here — prompts are per-phase).
          5. Write mcp.json if cfg.mcp_endpoint set.
          6. Fire on_installed (caller bridges to AGENT_INSTALLED).

        NO process is launched. The first `phase_command` invocation
        (driven by `AgentManager.run_phase`) is what actually starts Claude.
        """
        # All dirs the per-phase flow expects to exist.
        self._exec_sync(container, ["mkdir", "-p", STATE_DIR, LOG_DIR, RUN_DIR, WORKDIR], check=True)

        # Clear stale state from any prior run (legacy paths) so status()
        # doesn't report a ghost agent from before re-install.
        self._exec_sync(
            container,
            ["sh", "-c", f"rm -f {PID_PATH} {WRAPPER_PID_PATH} {EXIT_CODE_PATH}"],
            check=True,
        )

        if self.REQUIRED_CLI:
            prov.verify_cli(
                self._client, self._transport, container, self.REQUIRED_CLI,
                expected_version=CLAUDE_CODE_VERSION,
            )

        if cfg.own_prod_container_name:
            self._provision_ssh_pivot(container, cfg)

        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())

        if cfg.mcp_endpoint:
            self._write_mcp_config(container, cfg)

        log.info("agent installed on %s (cli=%r)", container.name, cfg.cli)

        # Claude is spawned per-phase by `AgentManager.run_phase`;
        # GameMgr.record_phase_* emits AGENT_PROMPT_SENT / COMPLETED /
        # TIMEOUT / FAILED.
        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv that drives one phase of Claude Code.

        Wraps the actual claude invocation in `bash -c` so we can:
          - cd into WORKDIR (claude --continue is cwd-scoped, so the
            session store at /root/.claude/projects/<cwd-hash>/ stays
            isolated to this arena)
          - source agent.env (ANTHROPIC_API_KEY / BASE_URL / CLAUDE_MODEL)
          - redirect stdin from PROMPT_PATH (claude reads the user prompt
            from stdin in -p mode)

        `model` overrides `$CLAUDE_MODEL` (which agent.env exports from
        cfg.model). Production leaves it None; tests pass it explicitly
        when the test mock client doesn't ship a model env var.

        `session_marker` (optional, set by run_phase per-invocation):
        when given, claude is launched under `setsid -w bash -c …` so it
        gets its own process group. The inner bash writes its PID (=PGID,
        because it's the session leader) to
        `/tmp/cyberarena_phase_<marker>.pid` and then `exec`s claude in
        place (same PID/PGID). Any subprocesses claude spawns (exploits,
        background jobs) inherit that PGID, so the on-timeout
        `kill_session` does `kill -KILL -- -<pgid>` and reaps the whole
        group.

        It also exports `CYBERARENA_PHASE=<marker>` into claude's
        environment. A timed-out Claude Code process can escape the launch
        process group (a PGID-only kill then reports success while the
        agent keeps running — verified 2026-06-10), so `kill_session`
        reaps by scanning `/proc/<pid>/environ` for this marker: it's
        group-independent, catches claude's children (they inherit the
        env), and is safe against the sibling claude the next phase starts
        in the same container because the marker is unique per invocation.
        """
        model_arg = (
            f"--model {shlex.quote(model)} "
            if model
            else '--model "$CLAUDE_MODEL" '
        )
        claude_argv = (
            "claude -p --continue "
            "--output-format stream-json --verbose "
            "--dangerously-skip-permissions "
            # orche #95/#109: `WebSearch` is claude-code's provider-hosted,
            # server-side web tool -- the aggregator runs the search off-box
            # and returns results over the NO_PROXY'd LLM-gateway channel
            # (api-router:8080), so it never traverses the container egress
            # path or the api-router:3128 default-deny firewall: no egress
            # log, invisible to the arena's network isolation. Disable it so
            # any web access must go client-side (WebFetch / shell) through
            # the egress proxy and hit the default-deny allowlist. WebFetch
            # is left enabled: it is client-side and already blocked+logged
            # by the proxy.
            "--disallowedTools WebSearch "
            + model_arg
            + f"--mcp-config {shlex.quote(MCP_CONFIG_PATH)} "
            f"< {shlex.quote(self.PROMPT_PATH)}"
        )
        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            # Export the marker into claude's env so kill_session can reap
            # by scanning /proc/<pid>/environ (group-independent). The
            # pidfile/PGID is kept as a best-effort fast path.
            inner = (
                f"echo $$ > {shlex.quote(pid_file)} && "
                f"export CYBERARENA_PHASE={shlex.quote(session_marker)} && "
                f"exec {claude_argv}"
            )
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = claude_argv
        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]

    # ── concrete-only convenience methods (NOT on the ABC) ─────────────
    # Kept for live-smoke tests + scripts/m6_screenshot_demo.py. Install
    # no longer writes PID/WRAPPER_PID/EXIT_CODE files, so these are
    # mostly best-effort no-ops on this subclass.

    def status(self, container: ContainerHandle) -> AgentState:
        """Legacy status read against supervisor pid files. The
        persistent-memory v1 install doesn't write them (no supervisor
        exists), so this normally returns NOT_INSTALLED.
        """
        wrapper_pid = self._read_int_legacy(container, WRAPPER_PID_PATH)
        pid = self._read_int_legacy(container, PID_PATH)
        exit_code = self._read_int_legacy(container, EXIT_CODE_PATH)

        if pid is None:
            return AgentState(status=AgentStatus.NOT_INSTALLED)

        if exit_code is not None:
            if exit_code == 0:
                status = AgentStatus.COMPLETED
            elif exit_code in (130, 143):
                status = AgentStatus.TERMINATED
            else:
                status = AgentStatus.CRASHED
            return AgentState(status=status, pid=pid, wrapper_pid=wrapper_pid, exit_code=exit_code)

        rc, _ = self._exec_sync(container, ["kill", "-0", str(pid)])
        if rc == 0:
            return AgentState(status=AgentStatus.RUNNING, pid=pid, wrapper_pid=wrapper_pid)
        return AgentState(status=AgentStatus.CRASHED, pid=pid, wrapper_pid=wrapper_pid)

    def kill(self, container: ContainerHandle) -> None:
        """Best-effort SIGTERM to any process matching `claude -p` inside
        the container. The per-phase claude is typically already dead
        by the time kill() is called from teardown; safety net only.
        """
        # Best-effort cleanup of legacy supervisor pid first.
        wrapper_pid = self._read_int_legacy(container, WRAPPER_PID_PATH)
        if wrapper_pid is not None:
            self._exec_sync(
                container,
                ["sh", "-c", f"kill -TERM -- -{wrapper_pid} 2>/dev/null || true"],
            )
        # Then anything still matching claude.
        self._transport.kill_process(container, "claude -p")

    def fetch_logs(self, container: ContainerHandle) -> AgentLogs:
        """Read the legacy stdout/stderr/exit_code paths. Returns empty
        strings under the current model. New code should consume
        per-phase artifacts written by `run_phase` (stream-{phase}.jsonl
        / agent-{phase}.log / metadata-{phase}.json).
        """
        stdout = self._read_text(container, STDOUT_PATH)
        stderr = self._read_text(container, STDERR_PATH)
        exit_code = self._read_int_legacy(container, EXIT_CODE_PATH)
        return AgentLogs(stdout=stdout, stderr=stderr, exit_code=exit_code)

    # ── internals (legacy + helpers) ────────────────────────────────────

    def _write_mcp_config(self, container: ContainerHandle, cfg: AgentConfig) -> None:
        """Drop mcp.json with the team token in headers. Mode 600 so
        it's not world-readable inside the container."""
        payload = {
            "mcpServers": {
                "cyberarena": {
                    "type": "http",
                    "url": cfg.mcp_endpoint,
                    "headers": {"X-Team-Token": cfg.team_token},
                }
            }
        }
        self._transport.write_file(
            container, MCP_CONFIG_PATH, (json.dumps(payload, indent=2) + "\n").encode()
        )
        self._exec_sync(container, ["chmod", "600", MCP_CONFIG_PATH], check=True)

    def _build_env_file(self, cfg: AgentConfig) -> str:
        """agent.env contents: API key + base URL + IS_SANDBOX, plus the
        attack-mode fields when populated. CLAUDE_MODEL is exported so
        the per-phase claude invocation can pick it up without needing
        --model on the command line."""
        lines = [
            f"export ANTHROPIC_API_KEY={shlex.quote(cfg.api_key)}",
            f"export ANTHROPIC_BASE_URL={shlex.quote(cfg.base_url)}",
            "export IS_SANDBOX=1",
        ]
        if cfg.model:
            lines.append(f"export CLAUDE_MODEL={shlex.quote(cfg.model)}")
        lines.extend(prov.team_env_lines(cfg))
        return "\n".join(lines) + "\n"

    def _provision_ssh_pivot(self, dev_container: ContainerHandle, cfg: AgentConfig) -> None:
        """Provision the dev→prod SSH pivot. Thin delegate to the shared
        `_provisioning.provision_ssh_pivot` (ed25519 keypair, push pubkey
        to own_prod, sshd, privkey + ssh config into dev)."""
        prov.provision_ssh_pivot(self._client, self._transport, dev_container, cfg)

    # ── sync exec helper (delegates to the shared _provisioning.exec_sync) ─

    def _exec_sync(
        self,
        container: ContainerHandle,
        cmd: list[str] | str,
        *,
        check: bool = False,
    ) -> tuple[int, str]:
        """Synchronous exec via docker-py. Thin delegate to the shared
        `_provisioning.exec_sync`; kept as a method so the legacy
        status / kill / fetch_logs paths call it unchanged."""
        return prov.exec_sync(self._client, self._transport, container, cmd, check=check)

    def _read_text(self, container: ContainerHandle, path: str) -> str:
        rc, out = self._exec_sync(container, ["sh", "-c", f"cat {path} 2>/dev/null || true"])
        return out

    def _read_int_legacy(self, container: ContainerHandle, path: str) -> int | None:
        rc, out = self._exec_sync(container, ["sh", "-c", f"cat {path} 2>/dev/null || true"])
        out = out.strip()
        if not out:
            return None
        try:
            return int(out)
        except ValueError:
            return None
