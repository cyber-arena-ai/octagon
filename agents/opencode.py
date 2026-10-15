"""OpenCodeAgentManager — opencode adapter for the AgentManager ABC.

opencode (https://opencode.ai) is an OSS terminal coding agent: headless
one-shot (`opencode run`) + session resume (`-c`), and it drives any
OpenAI-compatible endpoint, so a self-hosted OSS model needs no shim.

Two responsibilities, same shape as the Claude/Codex adapters:
  install(handle, cfg)        # per team: preflight the CLI, ssh pivot, write
                              # opencode.json + opencode.env, start the server.
  phase_command(model=None)   # argv running `opencode run -c` on PROMPT_PATH.

opencode specifics: one `opencode.json` (provider + remote MCP, found via
OPENCODE_CONFIG) instead of agent.env + mcp.json; MCP is a remote HTTP server
carrying the team token in an X-Team-Token header; the model is selected as
`<provider>/<model>` under our own provider id. Host scaffolding (ssh pivot,
CLI preflight, dir layout, team env) comes from agents/_provisioning.py; the
per-phase loop is inherited from AgentManager.run_phase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from typing import Callable, ClassVar

import docker

from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle

from . import _provisioning as prov
from ._provisioning import PROMPT_PATH, STATE_DIR, WORKDIR
from .manager import AgentManager
from .transport import AgentHandle, AgentTransport, CommandResult
from .transport_docker import DockerExecBackend

log = logging.getLogger(__name__)

# opencode-specific on-host paths (shared layout comes from _provisioning).
ENV_PATH = f"{STATE_DIR}/opencode.env"
CONFIG_PATH = f"{STATE_DIR}/opencode.json"
SERVE_LOG = f"{STATE_DIR}/opencode-serve.log"

# Every phase attaches to ONE long-lived `opencode serve` per container; bare
# per-run servers collide on the shared SQLite session DB and crash. Fixed
# port is safe — each dev container is its own network namespace.
SERVER_PORT = 4096
SERVER_URL = f"http://127.0.0.1:{SERVER_PORT}"

# Custom OpenAI-compatible provider id; the model is selected as `<id>/<model>`.
PROVIDER_ID = "cyberarena"
# Models whose pre-game probes (install warmup, pre-R1 dry-run) are worded to
# pre-empt reasoning. hy4-preview (2026-09-19) sometimes deliberated even "hi"
# for minutes, overrunning the warmup cap and leaving its turn running
# server-side when the dry-run resumed the session, and spiralled the same way
# on the dry-run itself (~60% of attempts timed out on a healthy agent). ONLY
# these models get the alternative wording; every other entrant keeps the
# prompts it has always been probed with (main._dry_run_prompt, _warmup).
NO_REASONING_PROBE_MODELS: tuple[str, ...] = ("hy4-preview",)
WARMUP_PROMPT = "hi"
WARMUP_PROMPT_NO_REASONING = ("Warmup only. Reply with the single word OK. "
                              "Do not think, plan, use any tool, or explain.")

# npm package that ships the `opencode` binary. Installed ONLY by
# dockerfiles/agent/Dockerfile; kept here as the pointer to what the image must
# bake. Unlike claude/codex there is no version pin to check against yet.
NPM_PACKAGE = "opencode-ai"

# install() callback — fires after config / env / ssh pivot are staged.
OnInstalled = Callable[[str], None]


def _session_id_of(stream_text: str) -> str | None:
    """The opencode sessionID stamped on every `--format json` line, or None."""
    for line in stream_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        sid = obj.get("sessionID")
        if sid:
            return sid
    return None


def _augment_lines(lines: list[str], session_messages: object) -> tuple[list[str], list[int]]:
    """Core of the reasoning backfill.

    Headless `opencode run --format json` drops `reasoning` parts (opencode
    #27361: it counts the reasoning tokens but never emits the text on stdout),
    so the trajectory shows only text + tools. The reasoning DOES survive in the
    server session (`GET /session/<id>/message`). Given that session payload,
    insert each `reasoning` part as a stream-json line
    (`{"type":"reasoning","part":{...,"text":...}}`, the shape
    gamemgr/transcripts already parses) immediately before the first existing
    line of the SAME `messageID`.

    Returns `(out_lines, origin)`: the augmented lines, and for each output line
    the 0-based index of the ORIGINAL line it takes its `.timing` timestamp from
    (an inserted reasoning line borrows the timestamp of the message line it
    precedes) — so the caller can realign the positional `.timing` sidecar. When
    nothing is inserted, `out_lines is lines` and `origin` is the identity map.

    Only messageIDs already present in this stream are augmented, so it is correct
    for both the async whole-run stream and a sync per-round stream. Existing lines
    and their order are untouched, and it is idempotent (a message that already
    carries a reasoning line is skipped)."""
    first_idx: dict[str, int] = {}
    already_reasoned: set[str] = set()
    sid: str | None = None
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except ValueError:
            continue
        if sid is None:
            sid = obj.get("sessionID")
        part = obj.get("part") or {}
        mid = part.get("messageID")
        if mid and mid not in first_idx:
            first_idx[mid] = i
        # Idempotency: if a reasoning line for this message is already present (a
        # prior backfill re-run, or a future opencode that emits it natively),
        # don't add a second one.
        if mid and (obj.get("type") == "reasoning" or part.get("type") == "reasoning"):
            already_reasoned.add(mid)
    if sid is None or not first_idx:
        return lines, list(range(len(lines)))

    inserts: dict[int, list[str]] = {}
    for msg in session_messages if isinstance(session_messages, list) else []:
        if not isinstance(msg, dict):
            continue
        for part in msg.get("parts") or []:
            if not isinstance(part, dict) or part.get("type") != "reasoning":
                continue
            mid = part.get("messageID")
            text = part.get("text") or ""
            if not text.strip() or mid not in first_idx or mid in already_reasoned:
                continue
            ts = (part.get("time") or {}).get("start") or 0
            rline = json.dumps(
                {"type": "reasoning", "timestamp": ts, "sessionID": sid, "part": part},
                ensure_ascii=False,
            )
            inserts.setdefault(first_idx[mid], []).append(rline)
    if not inserts:
        return lines, list(range(len(lines)))

    out_lines: list[str] = []
    origin: list[int] = []
    for i, line in enumerate(lines):
        for rline in inserts.get(i, []):
            out_lines.append(rline)
            origin.append(i)   # inserted reasoning borrows the ts of line i
        out_lines.append(line)
        origin.append(i)
    return out_lines, origin


def _merge_reasoning_lines(stream_text: str, session_messages: object) -> str:
    """Text-only view of the reasoning backfill (see `_augment_lines`). Used where
    the `.timing` sidecar isn't in play; returns the text unchanged when there is
    nothing to add."""
    lines = stream_text.splitlines()
    out_lines, _origin = _augment_lines(lines, session_messages)
    if out_lines is lines:
        return stream_text
    text = "\n".join(out_lines)
    return text + "\n" if stream_text.endswith("\n") else text


def _rewrite_timing(timing_text: str, origin: list[int]) -> str:
    """Realign the positional `.timing` sidecar (`<line_no>\\t<epoch>` per line)
    to an augmented stream: output line k (1-based) takes the timestamp the
    ORIGINAL line `origin[k-1]` (0-based) had. Lines whose source had no recorded
    timestamp are omitted, exactly as the original sparse sidecar leaves them."""
    orig: dict[int, str] = {}
    for ln in timing_text.splitlines():
        parts = ln.split("\t")
        if len(parts) == 2:
            try:
                orig[int(parts[0])] = parts[1]
            except ValueError:
                continue
    out: list[str] = []
    for k, src in enumerate(origin, start=1):
        ts = orig.get(src + 1)   # original sidecar is 1-based
        if ts is not None:
            out.append(f"{k}\t{ts}")
    return "\n".join(out) + ("\n" if timing_text.endswith("\n") else "")


class OpenCodeAgentManager(AgentManager):
    """Host-side controller for the opencode CLI; one instance handles every
    team. Transport is injected (DockerExecBackend by default; tests pass a
    FakeTransport)."""

    # ABC contract (`agents.manager.AgentManager`).
    PROMPT_PATH: ClassVar[str] = PROMPT_PATH
    PHASE_KILL_PATTERN: ClassVar[str] = "opencode run"
    # `agent.cli` selects this adapter; the command is fixed in
    # phase_command, so only the leading "opencode run" is honored.
    IMAGE: ClassVar[str] = "cyberarena/agent-opencode"
    CANONICAL_CLI: ClassVar[str | None] = "opencode run"
    REQUIRED_CLI: ClassVar[str | None] = "opencode"
    # Memory carries across phases via `opencode run -c` (like claude --continue).

    def __init__(
        self,
        *,
        transport: AgentTransport | None = None,
        client: docker.DockerClient | None = None,
    ) -> None:
        if transport is not None:
            self._transport = transport
            self._client = client  # may be None
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
        """One-shot setup per team (idempotent): make dirs, verify the image
        baked opencode, ssh pivot, write opencode.json + opencode.env, then start
        and warm the persistent `opencode serve` daemon. Unlike Claude/Codex, the
        server IS started here (every per-phase run attaches to it)."""
        prov.ensure_dirs(self._client, self._transport, container)

        if self.REQUIRED_CLI:
            prov.verify_cli(self._client, self._transport, container, self.REQUIRED_CLI)

        if cfg.own_prod_container_name:
            prov.provision_ssh_pivot(self._client, self._transport, container, cfg)

        self._write_opencode_config(container, cfg)
        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())

        if cfg.cli.startswith("opencode"):
            self._start_server(container)
            self._warmup(container, cfg)

        log.info("opencode installed on %s (cli=%r, model=%r)",
                 container.name, cfg.cli, cfg.model)

        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv driving one phase: cd to WORKDIR, source opencode.env,
        then `opencode run` with the prompt from PROMPT_PATH as a positional
        arg. `--attach` talks to the persistent server (not a fresh per-run
        one); `-c` resumes the prior phase's session (like claude --continue).
        `session_marker` (set by run_phase) launches under `setsid -w` with the
        leader PID teed to a pid file so on-timeout kill_session reaps the whole
        group; only the `run` client is killed, the server keeps running."""
        # The orchestrator passes the bare model id; opencode needs the
        # provider-prefixed ref or the run dies. _model_ref is idempotent.
        model_arg = (
            f"-m {shlex.quote(self._model_ref(model))} "
            if model
            else '-m "$OPENCODE_MODEL" '
        )
        opencode_argv = (
            f"opencode run --attach {SERVER_URL} "
            # Raw JSON events to stdout (not the formatted TUI) so the run's
            # tee_path captures a structured transcript the live panel can parse
            # (parallels Claude's stream-json / codex's exec --json).
            "--format json "
            # Auto-approve tool calls; headless opencode else rejects any path
            # outside the project root (ssh, /etc, /srv). Container is sacrificial.
            "--dangerously-skip-permissions "
            # Resume the prior phase's session (the install warmup seeds it).
            "-c "
            + model_arg
            + f'"$(cat {shlex.quote(self.PROMPT_PATH)})" '
            # Headless: prompt is argv, not stdin; redirect so the exec pipe EOF is never read.
            "< /dev/null"
        )
        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            # Export CYBERARENA_PHASE so kill_session can reap a `run` child that
            # escaped the group; the serve daemon carries no marker, so it's spared.
            inner = (
                f"echo $$ > {shlex.quote(pid_file)} && "
                f"export CYBERARENA_PHASE={shlex.quote(session_marker)} && "
                f"exec {opencode_argv}"
            )
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = opencode_argv
        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]

    async def run_phase(
        self,
        transport: AgentTransport,
        handle: AgentHandle,
        prompt: str,
        *,
        timeout: float,
        model: str | None = None,
        tee_path: "str | None" = None,
    ) -> CommandResult:
        """Run the phase, then backfill reasoning the headless JSON stream drops.

        `opencode run --format json` never emits `reasoning` parts (opencode
        #27361), so the base capture yields a text+tools-only transcript. After
        the run we read the reasoning back out of the still-running server session
        and merge it into the teed stream. Best-effort: any failure leaves the
        transcript exactly as the base produced it — the phase never fails here."""
        # `--attach` needs a live server; if it died mid-game, restart it so the
        # persisted session resumes rather than every chunk failing (see
        # _ensure_server). Best-effort — a run against a still-dead server just
        # fast-exits as before, which the supervisor's guard already handles.
        try:
            await self._ensure_server(transport, handle)
        except Exception as e:  # noqa: BLE001
            log.warning("opencode server ensure failed on %s: %s",
                        getattr(handle, "name", "?"), e)
        result = await super().run_phase(
            transport, handle, prompt,
            timeout=timeout, model=model, tee_path=tee_path,
        )
        if result.timed_out and self._model_id(model) in NO_REASONING_PROBE_MODELS:
            # ONLY for the reasoning-probe models (hy4-preview): those are the
            # only opencode games the coordinator retries, and the abort exists
            # so that retry starts clean. The base class killed the `run`
            # CLIENT, but the turn it started is still running on the server and
            # the next `run -c` would queue behind it. Every other opencode
            # model keeps the prior behaviour (a timed-out phase just fails);
            # aborting would be pointless there, since teardown follows anyway.
            try:
                await self._abort_sessions(transport, handle)
            except Exception as e:  # noqa: BLE001
                log.warning("opencode session abort failed on %s: %s",
                            getattr(handle, "name", "?"), e)
        if tee_path:
            try:
                await self._augment_stream_with_reasoning(transport, handle, tee_path)
            except Exception as e:  # noqa: BLE001 — augmentation must never fail a phase
                log.warning("opencode reasoning backfill failed on %s: %s",
                            getattr(handle, "name", "?"), e)
        return result

    async def _augment_stream_with_reasoning(
        self, transport: AgentTransport, handle: AgentHandle, tee_path: str,
    ) -> None:
        """Read the phase's reasoning from the opencode server session and merge
        it into the teed `--format json` stream (see `_merge_reasoning_lines`)."""
        try:
            with open(tee_path, encoding="utf-8", errors="replace") as f:
                stream_text = f.read()
        except OSError:
            return
        sid = _session_id_of(stream_text)
        if not sid:
            return
        # opencode serves the session messages at the ROOT `/session/<id>/message`
        # route (validated on opencode 1.18.30: 200 with the parts incl. reasoning).
        # The OpenAPI `/doc` lists an `/api/session/...` path, but on this server
        # that form returns an empty 200 — do NOT "canonicalize" to /api/ without
        # re-validating that it actually returns the messages.
        res = await transport.run_command(
            handle,
            ["bash", "-c",
             f"curl -fsS --max-time 10 {SERVER_URL}/session/{shlex.quote(sid)}/message"],
        )
        if res.exit_code != 0 or not res.stdout:
            log.warning("opencode session fetch failed on %s (rc=%s); "
                        "reasoning not backfilled", getattr(handle, "name", "?"),
                        res.exit_code)
            return
        try:
            session_messages = json.loads(res.stdout.decode("utf-8", errors="replace"))
        except ValueError:
            return
        lines = stream_text.splitlines()
        out_lines, origin = _augment_lines(lines, session_messages)
        if out_lines is lines:
            return  # nothing to backfill
        new_text = "\n".join(out_lines) + ("\n" if stream_text.endswith("\n") else "")
        with open(tee_path, "w", encoding="utf-8") as f:
            f.write(new_text)
        # Keep the positional `.timing` sidecar aligned: it maps line number ->
        # epoch, and both the orche and midend parsers join timestamps to events
        # by physical line number — so inserting reasoning lines without realigning
        # it would skew every later event's timestamp (and drop the tail).
        timing_path = tee_path + ".timing"
        try:
            with open(timing_path, encoding="utf-8", errors="replace") as f:
                timing_text = f.read()
        except OSError:
            return  # no sidecar (e.g. tests / a backend that doesn't tee timing)
        with open(timing_path, "w", encoding="utf-8") as f:
            f.write(_rewrite_timing(timing_text, origin))

    # ── internals (opencode-specific config / env / server) ──────────────

    def _start_server(self, container: ContainerHandle) -> None:
        """Launch the persistent `opencode serve` daemon (detached) and wait
        until it answers on SERVER_PORT. Loads the provider + MCP once for the
        whole match; best-effort wait (warmup/dry-run catch a dead one)."""
        launch = (
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            # --print-logs: opencode's own log (provider resolution, session
            # errors) otherwise goes to ~/.local/share/opencode/log and SERVE_LOG
            # holds only the two-line startup banner — which is all a dry-run that
            # exited 0 with empty client output left to read (2026-09-18).
            f"exec opencode serve --port {SERVER_PORT} --hostname 127.0.0.1 "
            f"--print-logs --log-level DEBUG "
            f">> {shlex.quote(SERVE_LOG)} 2>&1"
        )
        self._exec_detached(container, ["bash", "-c", launch])

        # Poll /doc until it answers; per-curl --max-time so a wedged request
        # can't hang this unbounded exec (and the whole install) forever.
        probe = (
            f"for i in $(seq 1 20); do "
            f"curl -fsS --max-time 3 --connect-timeout 2 -o /dev/null {SERVER_URL}/doc && exit 0; "
            f"sleep 1; done; exit 1"
        )
        rc, out = prov.exec_sync(self._client, self._transport, container, ["bash", "-c", probe])
        if rc != 0:
            log.warning(
                "opencode server not ready on %s after wait (continuing). tail=%r",
                container.name, out[-200:],
            )
        else:
            log.info("opencode server up on %s (%s)", container.name, SERVER_URL)

    async def _server_reachable(self, transport: AgentTransport,
                                handle: AgentHandle) -> bool:
        """Cheap liveness probe: does the persistent `opencode serve` still
        answer on SERVER_URL? Any error (server gone, curl missing) reads as
        unreachable."""
        probe = f"curl -fsS --max-time 3 --connect-timeout 2 -o /dev/null {SERVER_URL}/doc"
        try:
            res = await transport.run_command(handle, ["bash", "-c", probe], timeout=10)
            return res.exit_code == 0
        except Exception:  # noqa: BLE001
            return False

    async def _abort_sessions(self, transport: AgentTransport,
                              handle: AgentHandle) -> None:
        """POST /session/<id>/abort for every session the persistent server
        holds (opencode 1.18.31 route; aborting an idle session is a no-op).
        Best-effort: the caller only loses the fresh start."""
        script = (
            f"for id in $(curl -fsS --max-time 5 {SERVER_URL}/session "
            "| grep -oE '\"id\":\"ses_[^\"]+\"' | cut -d'\"' -f4 | sort -u); do "
            f"curl -fsS --max-time 5 -X POST {SERVER_URL}/session/$id/abort -o /dev/null; "
            "done"
        )
        res = await transport.run_command(handle, ["bash", "-c", script], timeout=30)
        if res.exit_code != 0:
            log.warning("opencode session abort on %s exited %s",
                        getattr(handle, "name", "?"), res.exit_code)

    async def _ensure_server(self, transport: AgentTransport,
                             handle: AgentHandle) -> None:
        """Restart the `opencode serve` daemon if it has died.

        The daemon is started once in install() and every per-chunk
        `opencode run --attach` depends on it. If it dies mid-game (an opencode
        crash — the phase kill path only matches `opencode run`, so it never
        targets `serve`), every following chunk fast-exits with 'Session not
        found' and the async supervisor gives up after ASYNC_MAX_FAST_EXITS: one
        server death loses the whole match (glm/gogs, b0002, 2026-09-18).
        Sessions persist on the container's disk, so a fresh server resumes them
        via `--continue`; restarting a dead server before the chunk turns a
        match-ending failure into a single lost chunk. No-op when the server is
        alive (the common case), so it is safe to call before every chunk.

        Residual not covered: a LIVE server that has lost the session itself
        (would still 'Session not found'); the observed failure was a dead
        server (rc=7 connection-refused), which this fixes."""
        if await self._server_reachable(transport, handle):
            return
        log.warning("opencode server on %s is unreachable; restarting the serve "
                    "daemon so the persisted session can resume",
                    getattr(handle, "name", "?"))
        # _start_server polls /doc until up (blocking); off the event loop.
        await asyncio.to_thread(self._start_server, handle)

    def _warmup(self, container: ContainerHandle, cfg: AgentConfig) -> None:
        """One attached `opencode run` so the server downloads the provider
        plugin + completes first-model init before the first real phase.
        `timeout` hard-caps it; best-effort (the dry-run confirms readiness)."""
        prompt = (WARMUP_PROMPT_NO_REASONING
                  if self._model_id(cfg.model) in NO_REASONING_PROBE_MODELS
                  else WARMUP_PROMPT)
        cmd = [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            f'timeout 120 opencode run --attach {SERVER_URL} --dangerously-skip-permissions -m "$OPENCODE_MODEL" '
            f'{shlex.quote(prompt)} < /dev/null',
        ]
        rc, out = prov.exec_sync(self._client, self._transport, container, cmd)
        if rc != 0:
            log.warning(
                "opencode warmup on %s returned rc=%d (continuing); the "
                "dry-run will confirm readiness. tail=%r",
                container.name, rc, out[-200:],
            )
        else:
            log.info("opencode warmup OK on %s", container.name)

    async def dry_run_diagnostics(self, transport: AgentTransport,
                                  handle: AgentHandle) -> dict[str, str]:
        """The persistent server's log tail. `opencode run --attach` is only a
        client: when the server declines the request (a provider/model that
        fails to resolve, a rejected session) the client exits 0 with nothing on
        stdout or stderr — hy4-preview did exactly that, 4 s, 0 upstream calls,
        2026-09-18 — and the reason is written only to SERVE_LOG."""
        res = await transport.run_command(
            handle, ["bash", "-c", f"tail -c 32768 {shlex.quote(SERVE_LOG)} 2>/dev/null"],
            timeout=15,
        )
        out = (res.stdout or b"").decode("utf-8", "replace")
        return {"opencode-serve": out} if out.strip() else {}

    def _exec_detached(self, container: ContainerHandle, cmd: list[str]) -> None:
        """Start `cmd` detached (used for the long-lived `opencode serve`).
        Falls back to the transport when only a transport is injected (tests)."""
        if self._client is None:
            import asyncio
            asyncio.run(self._transport.run_command(container, cmd))
            return
        c = self._client.containers.get(container.name)
        c.exec_run(cmd, detach=True)

    @staticmethod
    def _model_ref(model: str) -> str:
        """Resolve the user's `model` to opencode's `<provider>/<model>` form,
        always fronting it with PROVIDER_ID (strips any prefix the yaml carries)."""
        bare = model.split("/", 1)[-1] if model else ""
        return f"{PROVIDER_ID}/{bare}" if bare else ""

    @staticmethod
    def _model_id(model: str) -> str:
        """The bare model id (the name the model server exposes)."""
        return model.split("/", 1)[-1] if model else ""

    def _write_opencode_config(self, container: ContainerHandle, cfg: AgentConfig) -> None:
        """Write opencode.json (mode 600): a custom OpenAI-compatible provider
        pointed at cfg.base_url, plus a remote MCP server carrying the team
        token in headers when cfg.mcp_endpoint is set."""
        model_id = self._model_id(cfg.model)
        model_ref = self._model_ref(cfg.model)
        config: dict = {
            "$schema": "https://opencode.ai/config.json",
            # Default model + small_model so the server resolves without a per-run -m.
            "model": model_ref,
            "small_model": model_ref,
            "provider": {
                PROVIDER_ID: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "CyberArena (self-hosted)",
                    "options": {
                        "baseURL": cfg.base_url,
                        # ai-sdk requires a non-empty key even when the backend ignores it.
                        "apiKey": cfg.api_key or "sk-local",
                    },
                    "models": {
                        model_id: {"name": model_id},
                    },
                }
            },
        }
        if cfg.mcp_endpoint:
            config["mcp"] = {
                "cyberarena": {
                    "type": "remote",
                    "url": cfg.mcp_endpoint,
                    "enabled": True,
                    "headers": {"X-Team-Token": cfg.team_token},
                }
            }
        self._transport.write_file(
            container, CONFIG_PATH, (json.dumps(config, indent=2) + "\n").encode()
        )
        prov.exec_sync(
            self._client, self._transport, container,
            ["chmod", "600", CONFIG_PATH], check=True,
        )

    def _build_env_file(self, cfg: AgentConfig) -> str:
        """opencode.env: OPENCODE_CONFIG (so `opencode run` finds our config)
        + OPENCODE_MODEL (the `<provider>/<model>` ref) + the shared CYBERARENA_*
        / extra_env lines. The model endpoint + key live in opencode.json."""
        lines = [
            f"export OPENCODE_CONFIG={shlex.quote(CONFIG_PATH)}",
        ]
        model_ref = self._model_ref(cfg.model)
        if model_ref:
            lines.append(f"export OPENCODE_MODEL={shlex.quote(model_ref)}")
        lines.extend(prov.team_env_lines(cfg))
        return "\n".join(lines) + "\n"
