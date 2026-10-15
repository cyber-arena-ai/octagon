"""DeepSeekAgentManager — DeepSeek Harness (`dsh`) adapter for the AgentManager ABC.

DeepSeek Harness (https://github.com/deepseek-ai/deepseek-harness) is DeepSeek's
own open-source agent harness (`dsh`, released 2026-08-13, MIT). It's the NATIVE
harness for the DeepSeek V4 models, so it's the clean way to run v4-flash /
v4-pro in the arena — the same rationale that put kimi on kimi-cli. Its LLM seam
is the `deepseek-official` route: an OpenAI-compatible chat/completions adapter
(`POST {baseURL}/chat/completions`), so it rides the api-router verbatim exactly
like kimi's `openai_legacy` provider.

Same shape as the Claude/Codex/Kimi adapters:
  install(handle, cfg)        # per team: verify baked dsh, ssh pivot,
                              # write agent.env + the arena cordis overlay patch.
  phase_command(model=None)   # `dsh --profile headless --patch <overlay> "<task>"`
                              # consuming the prompt from PROMPT_PATH.

dsh specifics (verified against @deepseek-ai/dsh 0.1.0-rc.6):

  * Install — the validated prebuilt agent image supplies the exact
    @deepseek-ai/dsh pin and install() verifies the binary/version. Runtime
    package installs are forbidden; in-box bundles (base + headless + the
    mcp-client dep) ship with the pinned CLI.
  * Invocation — dsh is a plugin composition driven by "profiles". The shipped
    `headless` profile (`dsh --profile headless "<task>"`) is a one-shot: it
    creates one fresh persisted session, runs to quiescence, prints the final
    assistant text on stdout, and exits 0 (completed) / 1 (else) — the exact
    shape run_phase wants. It mounts NO web server / browser / HTTP listener.
  * Trajectory — headless prints ONLY that final-text blob on stdout, buffered
    to phase end, so tee'ing it gave the arena a single end-of-phase line that a
    SIGKILL (async round/match end) truncated to 0 bytes, with no per-tool-call
    structure. So we do NOT tee dsh's stdout: `phase_command` runs a small
    trajectory emitter (agents/dsh_trajectory_emitter.py) alongside dsh that
    tails dsh's own `session-persistence-jsonl` log — every session event
    (`{type,seq,time,data}`), written live and SIGKILL-durable — and translates
    it to the arena dsh-trajectory schema on stdout, which run_phase tees into
    `stream-async-<n>.jsonl`. The overlay forces that log to plaintext
    (`compression: none`). Schema is the #953/#954 parser⇄emitter contract
    (Artifact #71; parser `gamemgr/transcripts.py::_unpack_dsh_line`).
  * Endpoint / key — the base bundle mounts the `llm-deepseek` adapter with NO
    endpoint inlined: baseURL resolves from `$DEEPSEEK_BASE_URL` (else the
    public API) and the key from `$DEEPSEEK_API_KEY` (via the credential seam,
    then the environment). So we point it at the api-router purely through
    agent.env — no config file needed for routing, unlike codex/kimi.
  * Model — the base `agent-default-model` row selects
    `provider: deepseek-official`, `model: deepseek-v4-flash` by default. dsh
    has NO per-invocation model flag (model comes from that row), so we pin
    cfg.model (deepseek-v4-flash / deepseek-v4-pro) by overriding that one row
    in the arena overlay patch. The wire `model` string is passed through
    unchanged, so either id just works.
  * Config layering — a `--patch <file>` launcher-flag overlay is applied last
    over base+headless, replacing addressed rows' whole `config` and inserting
    new rows. We ship ONE overlay that (1) pins the model, (2) disables the
    model-facing web tool (no server-side web search in the arena, matching the
    other harnesses), and (3) inserts the cyberarena MCP client when configured.
  * MCP — `@deepseek-ai/dsh-mcp-client` (bundled with the CLI) as a
    streamable-http server carrying the team token in an `X-Team-Token` header;
    tools surface to the model as `mcp__cyberarena__<name>`, the same
    server-qualified shape codex/claude use. `failOnStartupError: true` makes a
    dead MCP fail the run loudly rather than run a blind, toolless agent.
  * Memory across phases — the product `headless` CLI is strictly one-shot
    (fresh `session-<uuid>` per invocation); it exposes no --continue. That
    fits ASYNC mode (one long supervised invocation per match, the codex
    reference topology) exactly. In SYNC mode each phase would start fresh with
    no cross-phase memory — a known limitation of the native harness's product
    surface, not a wiring gap.
  * Prompt — passed as the positional task arg (`"$(cat prompt.txt)"`) with
    stdin closed; launcher flags (`--profile`, `--patch`) come first, then the
    task, per dsh's argument-boundary rule.
  * Usage — dsh headless emits no token/cost events on stdout; per-team
    token/cost come from the api-router's usage metering, same as every other
    adapter.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Callable, ClassVar

import docker

from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle

from . import _provisioning as prov
from ._provisioning import ENV_PATH, PROMPT_PATH, STATE_DIR, WORKDIR
from .manager import AgentManager
from .transport import AgentTransport
from .transport_docker import DockerExecBackend

log = logging.getLogger(__name__)

# dsh-local host paths (kept under STATE_DIR with the rest of the arena's agent
# state). DSH_HOME holds the auto-initialized profiles + the session store; the
# arena overlay patch is a --patch layer applied last over base+headless.
DSH_HOME = f"{STATE_DIR}/dsh"
DSH_PATCH_PATH = f"{DSH_HOME}/arena.cordis.yml"

# Trajectory emitter (see agents/dsh_trajectory_emitter.py). dsh headless prints
# only the final assistant text on stdout at phase end; the arena needs a
# populated per-tool-call stream. dsh's `session-persistence-jsonl` backend
# already writes every event to `session.jsonl` under DSH_SESSIONS_ROOT, live —
# the arena overlay forces `compression: none` so it is plaintext. `phase_command`
# runs this emitter alongside dsh: it tails that log and translates each event to
# the arena dsh-trajectory schema on stdout, which run_phase tees into
# `stream-async-<n>.jsonl` live (SIGKILL-safe — already-streamed lines survive).
DSH_SESSIONS_ROOT = f"{DSH_HOME}/sessions"
EMITTER_PATH = f"{DSH_HOME}/dsh_trajectory_emitter.py"
# dsh's own stdout (final text) / stderr are redirected here so the phase's
# stdout carries ONLY the emitter's trajectory lines; kept for debugging.
DSH_FINAL_PATH = f"{DSH_HOME}/dsh-final.txt"
DSH_STDERR_PATH = f"{DSH_HOME}/dsh-stderr.log"

# The emitter module source, shipped verbatim into the container by install().
# Read from the repo copy so the container-side script and the unit-tested module
# never drift.
_EMITTER_SOURCE = (Path(__file__).with_name("dsh_trajectory_emitter.py")).read_bytes()

# The shipped one-shot profile: base + headless, no web/HTTP/browser.
DSH_PROFILE = "headless"
# The LLM route the base bundle registers for DeepSeek (OpenAI-compatible
# chat/completions). cfg.model is passed through as the wire `model` string.
PROVIDER_ROUTE = "deepseek-official"
# Env vars the base `llm-deepseek` adapter resolves per request.
API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL_ENV = "DEEPSEEK_BASE_URL"
# MCP server name as the agent sees it (tools become mcp__cyberarena__<name>).
MCP_SERVER_NAME = "cyberarena"
# Version required from the validated prebuilt agent image.
DSH_VERSION = "0.1.0-rc.6"

# dsh 0.1.0-rc.6's DeepSeek stream assembler can receive a valid tool name
# followed by an empty name delta.  The latter overwrites the former and the
# assembled call is dispatched as `unknown tool ""`.  This compatibility patch
# is applied in-place to the baked bundle; it performs no package install and
# is idempotent across images that already contain the fix.
DSH_TOOL_ASSEMBLER_PATCH = r"""python3 - <<'PY'
from pathlib import Path
import glob

old = "if (call.function?.name !== void 0) block.name = call.function.name;"
new = "if (call.function?.name) block.name = call.function.name;"
patterns = (
    "/usr/lib/node_modules/@deepseek-ai/dsh/**/dsh-llm-deepseek/lib/index.js",
    "/opt/dsh-rc6/**/dsh-llm-deepseek/lib/index.js",
)
seen = patched = 0
for raw in patterns:
    for filename in glob.glob(raw, recursive=True):
        path = Path(filename)
        text = path.read_text()
        if old in text:
            path.write_text(text.replace(old, new))
            patched += 1
            seen += 1
        elif new in text:
            seen += 1
if seen == 0:
    raise SystemExit("dsh DeepSeek tool assembler was not found")
print(f"dsh tool assembler matched={seen} patched={patched}")
PY"""

_WARMUP_EXIT_CLASSES = frozenset({
    "not_run", "clean_exit", "timeout", "nonzero_exit", "unknown",
})

OnInstalled = Callable[[str], None]


def _yaml_str(s: str) -> str:
    """Quote a string as a YAML double-quoted scalar (escape `\\` and `"`)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class DeepSeekAgentManager(AgentManager):
    """Host-side controller for the DeepSeek Harness (`dsh`) CLI. One instance
    per team; each method takes the team's dev ContainerHandle. Transport is
    injected (DockerExecBackend by default); tests pass a FakeTransport."""

    PROMPT_PATH: ClassVar[str] = PROMPT_PATH
    PHASE_KILL_PATTERN: ClassVar[str] = "dsh"
    # `agent.cli` selects this adapter; the command is fixed in phase_command,
    # so only the leading "dsh" is honored.
    # One image serves both registry keys (`deepseek` and `dsh`).
    IMAGE: ClassVar[str] = "cyberarena/agent-deepseek"
    CANONICAL_CLI: ClassVar[str | None] = "dsh"
    REQUIRED_CLI: ClassVar[str | None] = "dsh"

    def __init__(
        self,
        *,
        transport: AgentTransport | None = None,
        client: docker.DockerClient | None = None,
    ) -> None:
        self._warmup_exit_class = "not_run"
        if transport is not None:
            self._transport = transport
            self._client = client  # may be None
        else:
            self._client = client or docker.from_env()
            self._transport = DockerExecBackend(client=self._client)

    @property
    def warmup_exit_class(self) -> str:
        """Bounded outcome of the most recent best-effort profile warmup."""

        value = self._warmup_exit_class
        return value if value in _WARMUP_EXIT_CLASSES else "unknown"

    # ── public API ──────────────────────────────────────────────────────

    def install(
        self,
        container: ContainerHandle,
        cfg: AgentConfig,
        *,
        on_installed: OnInstalled | None = None,
    ) -> None:
        """One-shot setup per team. Idempotent; called once per game.

        Steps:
          1. Make the host layout dirs (+ DSH_HOME).
          2. Verify the baked dsh CLI and pinned version.
          3. SSH pivot if cfg.own_prod_container_name set.
          4. Write agent.env (DEEPSEEK_API_KEY + DEEPSEEK_BASE_URL + DSH_HOME +
             tools/telemetry knobs + the shared team vars).
          5. Write the arena overlay patch (pin model, disable web tool, force
             the session log to plaintext for the trajectory emitter, and — when
             cfg.mcp_endpoint is set — insert the cyberarena MCP client).
          6. Ship the trajectory emitter script into the container.
          7. Warm the headless profile offline via `--dump-config` so its
             shipped template auto-initializes before the first real phase.
          8. Fire on_installed (caller bridges to AGENT_INSTALLED).

        NO agent process is launched — the first phase_command starts dsh.
        """
        self._validate_readiness_contract(cfg)
        prov.ensure_dirs(self._client, self._transport, container, extra=(DSH_HOME,))

        if self.REQUIRED_CLI:
            prov.verify_cli(
                self._client,
                self._transport,
                container,
                self.REQUIRED_CLI,
                expected_version=DSH_VERSION,
            )

        # Keep the provider compatibility fix close to the baked-CLI check so
        # stale images fail loudly before a readiness probe can run blind.
        self._patch_tool_name_assembler(container)

        if cfg.own_prod_container_name:
            prov.provision_ssh_pivot(self._client, self._transport, container, cfg)

        self._transport.write_file(container, ENV_PATH, self._build_env_file(cfg).encode())
        self._transport.write_file(
            container, DSH_PATCH_PATH, self._build_overlay_patch(cfg).encode(),
        )
        # Ship the trajectory emitter; phase_command runs it alongside dsh to
        # translate the live session log into the arena trajectory stream.
        self._transport.write_file(container, EMITTER_PATH, _EMITTER_SOURCE)
        prov.exec_sync(
            self._client, self._transport, container,
            ["chmod", "600", DSH_PATCH_PATH], check=True,
        )

        self._warmup_profile(container)

        log.info("agent installed on %s (cli=%r, model=%r)",
                 container.name, cfg.cli, cfg.model)

        if on_installed is not None and cfg.team_id:
            on_installed(cfg.team_id)

    def _patch_tool_name_assembler(self, container: ContainerHandle) -> None:
        """Preserve non-empty tool names across empty streaming deltas.

        DeepSeek's OpenAI-compatible stream may send the function name in one
        delta and an empty name in a later delta.  dsh rc.6 treats the latter as
        authoritative, producing an empty-name tool call.  The small in-place
        bundle patch is deterministic, idempotent, and does not install npm
        packages at runtime.  A missing bundle is a hard error because running
        without this contract cannot satisfy the MCP readiness gate.
        """
        prov.exec_sync(
            self._client,
            self._transport,
            container,
            ["bash", "-c", DSH_TOOL_ASSEMBLER_PATCH],
            check=True,
        )

    @staticmethod
    def _validate_readiness_contract(cfg: AgentConfig) -> None:
        """Reject a config that cannot complete the MCP readiness handshake.

        A successful `dsh --dump-config` only proves that the profile overlay
        parses; it does not prove that the model can reach the arena MCP. The
        live dry-run requires all three values below. Failing before container
        setup keeps deploy/runner misconfiguration distinct from a provider
        response or a model refusal.
        """
        missing = []
        if not cfg.base_url:
            missing.append("base_url")
        if not cfg.mcp_endpoint:
            missing.append("mcp_endpoint")
        if not cfg.team_token:
            missing.append("team_token")
        if missing:
            raise ValueError(
                "DeepSeek readiness contract missing " + ", ".join(missing)
            )

    def phase_command(
        self,
        *,
        model: str | None = None,
        session_marker: str | None = None,
    ) -> list[str]:
        """Shell argv that drives one phase of dsh.

        Wraps `dsh --profile headless --patch <overlay> "<task>"` in `bash -c`
        so we can cd into WORKDIR (dsh treats the invoking dir as the workspace
        root) and source agent.env (DEEPSEEK_* + DSH_HOME + CYBERARENA_*). The
        launcher flags (`--profile`, `--patch`) come first, then the staged
        prompt as the positional task arg with stdin closed.

        `model` is intentionally unused: dsh has no per-invocation model flag —
        the model is pinned in the overlay patch's `agent-default-model` row at
        install time (from cfg.model), so every phase runs that same model.

        `session_marker` (set by run_phase) launches dsh under `setsid -w bash
        -c …` with its PID teed to `/tmp/cyberarena_phase_<marker>.pid` and
        `CYBERARENA_PHASE` exported, so on-timeout kill_session reaps the whole
        group — identical mechanism to Claude/Codex/Kimi. The trajectory emitter
        launched in the run body is a child of that same group, so the on-timeout
        SIGKILL reaps it too (and whatever it already streamed is preserved).
        """
        run_body = self._run_body()

        if session_marker:
            pid_file = f"/tmp/cyberarena_phase_{session_marker}.pid"
            # NB: no `exec` — the run body is a multi-process pipeline (emitter +
            # dsh), so this bash stays as the group leader whose PID is in the
            # pidfile; kill_session reaps the whole group.
            inner = (
                f"echo $$ > {shlex.quote(pid_file)} && "
                f"export CYBERARENA_PHASE={shlex.quote(session_marker)} && "
                f"{run_body}"
            )
            launch = f"exec setsid -w bash -c {shlex.quote(inner)}"
        else:
            launch = run_body

        return [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            + launch,
        ]

    def _run_body(self) -> str:
        """The phase's run pipeline (assumes cwd=WORKDIR and agent.env sourced):

          1. Launch the trajectory emitter in the background, tailing the dsh
             session log under DSH_SESSIONS_ROOT and streaming arena
             dsh-trajectory lines to STDOUT (which run_phase tees, live, into
             `stream-async-<n>.jsonl`).
          2. Run `dsh --profile headless --patch <overlay> "<task>"` with stdin
             closed and its OWN stdout/stderr redirected to files under DSH_HOME
             — so the phase's stdout carries ONLY the emitter's trajectory lines,
             never dsh's single end-of-phase final-text blob (the thing that used
             to be the whole, 0-byte-on-SIGKILL, stream).
          3. On dsh exit, SIGTERM the emitter so it drains the final events and
             exits, then propagate dsh's exit code.
        """
        prompt_q = shlex.quote(self.PROMPT_PATH)
        patch_q = shlex.quote(DSH_PATCH_PATH)
        emit_q = shlex.quote(EMITTER_PATH)
        root_q = shlex.quote(DSH_SESSIONS_ROOT)
        final_q = shlex.quote(DSH_FINAL_PATH)
        err_q = shlex.quote(DSH_STDERR_PATH)
        return (
            f"__not_before=$(date +%s.%N); "
            f"python3 {emit_q} --root {root_q} --not-before \"$__not_before\" & __emit=$!; "
            f"dsh --profile {shlex.quote(DSH_PROFILE)} --patch {patch_q} "
            f'"$(cat {prompt_q})" </dev/null >{final_q} 2>>{err_q}; __rc=$?; '
            # Preserve the CLI's bounded stderr on the host-side command result.
            # dsh normally writes final text/stderr under DSH_HOME; without this
            # drain a clean non-zero startup is indistinguishable from an empty
            # process exit in the runner diagnostics.
            f"kill -TERM $__emit 2>/dev/null; wait $__emit 2>/dev/null; "
            f"if [ -s {err_q} ]; then printf '\\n[DSH_STDERR_BEGIN]\\n' >&2; "
            f"sed -n '1,160p' {err_q} >&2; printf '[DSH_STDERR_END]\\n' >&2; fi; "
            f"exit $__rc"
        )

    # ── internals ───────────────────────────────────────────────────────

    def _warmup_profile(self, container: ContainerHandle) -> None:
        """Best-effort: `dsh --profile headless --patch <overlay> --dump-config`
        composes the tree WITHOUT booting it, calling no model and connecting no
        MCP, which auto-initializes the shipped headless profile and validates
        the overlay layers. Failure is logged, not raised — the first real phase
        would re-init anyway."""
        cmd = [
            "bash", "-c",
            f"cd {shlex.quote(WORKDIR)} && "
            f"set -a && . {shlex.quote(ENV_PATH)} && set +a && "
            f"timeout 120 dsh --profile {shlex.quote(DSH_PROFILE)} "
            f"--patch {shlex.quote(DSH_PATCH_PATH)} --dump-config >/dev/null 2>&1",
        ]
        try:
            rc, _output = prov.exec_sync(
                self._client, self._transport, container, cmd
            )
        except Exception as exc:
            self._warmup_exit_class = "unknown"
            log.warning(
                "dsh profile warmup on %s raised %s (continuing); the first "
                "phase will re-init",
                container.name,
                type(exc).__name__,
            )
            return
        if rc == 0:
            self._warmup_exit_class = "clean_exit"
        elif rc == 124:
            self._warmup_exit_class = "timeout"
        elif isinstance(rc, int):
            self._warmup_exit_class = "nonzero_exit"
        else:
            self._warmup_exit_class = "unknown"
        if rc != 0:
            log.warning(
                "dsh profile warmup on %s returned rc=%d (continuing); the first "
                "phase will re-init", container.name, rc,
            )
        else:
            log.info("dsh headless profile warmed on %s", container.name)

    def _build_env_file(self, cfg: AgentConfig) -> str:
        """agent.env: the DeepSeek key + base URL the `llm-deepseek` adapter
        resolves per request, DSH_HOME so profiles/sessions live under
        STATE_DIR, both tool presentations + telemetry-off knobs, then the shared
        CYBERARENA_* / extra_env lines. The api_key is the per-team principal
        label (the router substitutes the real upstream key), same as the
        other adapters."""
        lines = [
            f"export {API_KEY_ENV}={shlex.quote(cfg.api_key or 'sk-local')}",
        ]
        if cfg.base_url:
            lines.append(f"export {BASE_URL_ENV}={shlex.quote(cfg.base_url)}")
        lines.extend([
            f"export DSH_HOME={shlex.quote(DSH_HOME)}",
            # Keep both the native and compatibility tool presentations.  The
            # DeepSeek route can return tool calls through either presentation;
            # native-only can silently assemble an empty tool name on affected
            # dsh/provider combinations, causing `unknown tool \"\"` instead
            # of invoking report_ready.
            "export DSH_TOOLS_MODE=both",
            # Keep every session event local; no OTLP egress from the arena.
            "export DSH_TELEMETRY_DISABLED=1",
        ])
        lines.extend(prov.team_env_lines(cfg))
        return "\n".join(lines) + "\n"

    def _build_overlay_patch(self, cfg: AgentConfig) -> str:
        """The arena `--patch` overlay (applied last over base+headless):

          * `agent-default-model` — pin provider+model so headless runs
            cfg.model (deepseek-v4-flash / deepseek-v4-pro); a patch replaces
            the row's whole `config`, so both fields are restated.
          * `tool-web` — disabled: no model-facing web search/fetch in the
            arena (matches the other harnesses; avoids egress-firewall noise).
          * `mcp-<cyberarena>` — inserted only when cfg.mcp_endpoint is set: the
            streamable-http MCP client carrying the team token in an
            X-Team-Token header (read from the env at load), failing loud on a
            dead server so the agent never runs blind.
        """
        model = cfg.model or "deepseek-v4-flash"
        out = [
            "# Arena overlay for `dsh --profile headless` (applied last over",
            "# base+headless). Pins the model, drops server-side web search, and",
            "# wires the cyberarena MCP server.",
            "- id: agent-default-model",
            "  config:",
            f"    provider: {PROVIDER_ROUTE}",
            f"    model: {_yaml_str(model)}",
            "",
            "# No model-facing web search/fetch in the arena.",
            "- id: tool-web",
            "  disabled: true",
            "",
            "# Trajectory stream source: make dsh's session log plaintext so the",
            "# arena trajectory emitter can tail it. A --patch replaces the row's",
            "# whole config, so `root` is restated (base default: dshHomePath",
            "# ('sessions')); `compression: none` overrides the default zstd.",
            "- id: session-persistence-jsonl",
            "  config:",
            f"    root: {_yaml_str(DSH_SESSIONS_ROOT)}",
            "    compression: none",
        ]
        if cfg.mcp_endpoint:
            out.extend([
                "",
                "- insert:",
                f"    - id: mcp-{MCP_SERVER_NAME}",
                "      name: '@deepseek-ai/dsh-mcp-client'",
                "      config:",
                f"        serverName: {MCP_SERVER_NAME}",
                "        transport: streamable-http",
                f"        url: {_yaml_str(cfg.mcp_endpoint)}",
                "        headers:",
                "          X-Team-Token: !!js process.env.CYBERARENA_TEAM_TOKEN",
                "        failOnStartupError: true",
                "        toolCallTimeoutMs: 60000",
            ])
        return "\n".join(out) + "\n"
