# DEFCON Challenge Framework + Nautilus Adapter — Plan v1

**Goal**: pluggable `ChallengeSource` interface, then ship one concrete
adapter ([nautilus-institute/finals-2025](https://github.com/nautilus-institute/finals-2025))
running its `ico` challenge end-to-end, including the agent defense loop
(edit source → MCP `restart_service` → smoke → `report_deploy_complete`).

**Two adapters from day one** — `notes` (Python TCP daemon) +
`nautilus.ico` (FreePascal binary) — keeps the ABC honest. Parametrized
contract test runs the same body against both.

---

## ⬜ Out of scope

- 7 other nautilus challenges (same recipe, later).
- Auto-generated Dockerfiles (hand-author per challenge).
- Patch quality scoring (we verify the agent CAN run defense, not that the patch closes the vuln).
- Cross-challenge dependencies (one container, one port, isolated).
- Multi-flag per service per round (unchanged from M5.5).
- Seccomp profile from upstream `challenge_config.yaml` (documented gap).
- v1 emits `SERVICE_HEALTH_CHECK { CHECKER, passed=false }` but does **not** gate scoring or roll back (scoring milestone owns that).

---

## Generic contract

New module `challenges/source.py` exports:

- **`ChallengeSource` (ABC)** — one subclass per backing catalog (notes, nautilus, picoCTF, ...). Two abstract methods:
  - `list_challenges() → list[ChallengeSpec]`
  - `build_image(spec) → image_tag` (build the docker image, return the tag)
- **`ChallengeSpec` (frozen dataclass)** with fields:
  - `name` — unique in the game; `game.yaml` references this
  - `source` — `"notes"` / `"nautilus"` / etc.
  - `port` — service port inside the container
  - `image_tag` — e.g. `cyberarena/chal-nautilus-ico:<sha>`
  - `flag_mechanism` — see enum below
  - `source_view` (optional) — subpath inside the upstream challenge dir whose contents the adapter **COPIES** into the runtime image at build time, ending up at `/srv/challenge/<name>/`. **NEVER a host bind mount** (would leak across teams + poison vendor tree).
  - `metadata` (dict) — opaque to the rest of the system; adapter consumes
- **`FlagMechanism` enum**: `FILE_AT_PATH` (write to e.g. `/flag` at deploy) / `ENV_VAR` / `BINARY_PATCH` (rewrite sentinel in compiled binary)
- **`HealthLevel` enum**: `PROCESS` (pid alive) / `TCP` (port answers) / `PROTOCOL` (speaks the protocol) / `CHECKER` (gameplay-correct: vuln reachable, flag retrievable). Higher levels imply the lower ones.

`Service` ABC otherwise **unchanged**, with one signature update: `is_alive(handle, level=HealthLevel.PROTOCOL)`. The default keeps M5/M5.5/M5.6 callers backwards-compatible.

---

## Topology — unchanged from M5.6

**One service container per team**, named `team{N}_prod`. That single
container hosts whatever challenge the game is configured to run. Game
runs one challenge at a time across both teams; multi-challenge in a
single game is out of scope for v1.

Networks inherit M5.6 verbatim:
- `team{N}_net` (per team, never switched) — dev + prod
- `game_net` (cross-team, switched per phase — ATTACK attached, DEFENSE detached)

Implication: when v1 changes the active challenge (e.g. from `notes`
to `nautilus-ico`), the team's `prod` container image must change too
(notes runs in `cyberarena/prod-base`; ico needs `cyberarena/chal-nautilus-ico:<sha>`).
That image switch is the game configuration's responsibility — written
into `game.yaml`'s `nodes[].image` for the prod containers.

The split-net / per-(team, challenge) topology that an earlier draft
proposed is deferred. If a future game needs multiple challenges
running simultaneously per team, revisit.

---

## Nautilus adapter

`challenges/nautilus.py`:

- Pin-clones `nautilus-institute/finals-2025` into `vendor/nautilus-finals-2025/` (gitignored) via `scripts/sync_nautilus.sh`.
- Per-challenge dir `challenges/nautilus/<name>/` contains `Dockerfile`, `spec.toml`, `restart.sh`, `checker.sh` (all 4 hand-authored).
- `list_challenges()` reads every `spec.toml`. `build_image()` runs `docker build` against `{our Dockerfile, vendor/<chal>/}` and tags `cyberarena/chal-nautilus-<name>:<pin>`.

### `ico` — v1 scope

| Property | Value |
|----------|-------|
| Source | `vendor/nautilus-finals-2025/ico/service/` (Object Pascal) |
| Port | 4265 |
| Flag | `FILE_AT_PATH` → `/flag` |
| Build | FreePascal `fpc` |
| A/D-ready | yes (upstream README: "multiple bugs", "patch quickly") |

`challenges/nautilus/ico/spec.toml` carries:

- Top-level: `name = "nautilus-ico"`, `source = "nautilus"`, `port = 4265`, `flag_mechanism = "file_at_path"`, `flag_path = "/flag"`, `restart_cmd = "/arena/restart.sh"`, `source_view = "service"` (the `service/` subpath baked into image at `/srv/challenge/ico/`).
- `[health_check]` section: `process_pattern` (the `pgrep` target identifying the running `ico` binary) + `checker_command` (path to the checker script, baked root-owned and outside `source_view`).

`challenges/nautilus/ico/restart.sh` does:

1. `cd` into the agent's writable source dir (`/srv/challenge/ico/`).
2. Re-compile via `fpc` with upstream's release flags (from `ico/Makefile`).
3. `pkill` the old binary if running.
4. `nohup` the new binary into background; stdout/stderr redirected to `/var/log/ico.stdout`.

### Integration tax (recurs per binary challenge)

1. **Single-stage Dockerfile** keeping the toolchain. Upstream's multi-stage strips `fpc` from runtime → in-container rebuild impossible. Our single-stage keeps `fp-compiler` + `fp-units-*`. Bigger image, defense works.
2. **Source baked into image** at `/opt/challenge_src/<name>/` (read-only template); entrypoint copies → `/srv/challenge/<name>/` (writable container layer). Agent edits its own container's copy. **No host bind mount.**

Crib from upstream: `service/` dir (via build context), package list, `fpc ... -oico ico.pas` invocation. Skip `*.hvif` build-time tests (not needed at runtime). The nautilus poller in `ico/poller/` becomes our `/arena/checker.sh`.

---

## Defense workflow

DEFENSE phase enters → defense agent spawns (fresh per phase) → agent runs:

1. **Edit** — SSH into own `team{N}_prod` (M5.6 pivot, over `team{N}_net`). Edit `/srv/challenge/<name>/` — the prod container's own writable layer.
2. **Restart** — call `mcp__cyberarena__restart_service(service=<name>)`. **Returns `{job_id}` immediately**; orchestrator runs `spec.restart_cmd` via `docker exec` in a background task.
3. **Wait + smoke** — TCP-probe own prod's service port every few seconds (loopback inside the team's network) until it answers. Bound the wait (~60s).
4. **Report** — call `mcp__cyberarena__report_deploy_complete(...)` (M4.5 tool).

### Critical invariants

**Filesystem isolation** (pinned by `test_dcf_filesystem_isolation.py`):
- No host bind mount. Period. Each `team{N}_prod` container gets its own copy via entrypoint `cp -r /opt/challenge_src/<name>/ /srv/challenge/<name>/`.
- A write to `/srv/challenge/<name>/foo` in team1's prod is invisible to team2's prod (separate container filesystems) AND invisible to `vendor/.../ico/service/foo` on host (no bind mount).

**Authorization** (pinned by `test_dcf_restart_tool.py` + `test_dcf_user_perms.py`):
- `restart_service(service)` resolves `team_id` from `X-Team-Token` header (M4.5 flow), then looks up `f"team{N}_prod"`. Tool argument is the **bare** challenge name — no syntax for "restart team2's container".
- No docker socket in any agent-reachable container (re-confirms M5.6 §10).
- **Orchestrator is the only entity allowed to attach/detach `game_net`**; agents never receive Docker or network privileges. The agent's only path to "modify network state" is the MCP `restart_service` tool, which the orchestrator executes under its own privilege.
- `restart_cmd` + `checker_command` live in `spec.toml` (framework-provided). Never LLM-supplied.

**User + permission model inside the challenge container** (revises M5.6's "root SSH" stance — the M5.6 root-access was for *attack* introspection on own prod; with M7's defense-mode + restart MCP, the agent doesn't need root in-container anymore):

| Path | Owner | Mode | Agent can |
|------|-------|------|-----------|
| `/srv/challenge/<name>/` | `arena_agent:arena_agent` | `755` | **read + write** (the edit surface) |
| `/opt/challenge_src/<name>/` | `root:root` | `555` | read only (immutable template) |
| `/arena/restart.sh`, `/arena/checker.sh` | `root:root` | `555` | read + execute, **not write** |
| `/flag` | `root:root` | `644` | read (service needs it; agent reading own flag is fine) |
| `/home/arena_agent/.ssh/authorized_keys` | `arena_agent:arena_agent` | `600` | n/a (sshd reads, agent already authed) |

- **SSH lands as `arena_agent`** (non-root). Per-challenge Dockerfile creates the user; `ClaudeAgentManager._provision_ssh_pivot` puts the pubkey in `/home/arena_agent/.ssh/authorized_keys`; the dev-side ssh config specifies `User arena_agent`. sshd config sets `PermitRootLogin no`.
- **`/arena/*` is read-only to the agent by simple Unix permissions** — agent can't tamper with `restart.sh` / `checker.sh` even though it can read them. Permissions are enforced at the kernel layer, not the prompt layer.
- **Orchestrator runs `docker exec` as root** for the actual restart work (rebuild + restart). Agent never invokes `docker exec` itself.
- Service process can run as a further-restricted user (e.g. upstream ico's `ico` user) — orthogonal to the agent's SSH user; service hardening is its own thing.

### MCP `restart_service` tool (4th on the M4.5 server) — async

Takes a single argument: the bare challenge name (string). Returns immediately with a `job_id` (12-hex). Error cases:

- Unknown service → `InvalidParams`
- A restart already in flight for this `(team, service)` → `InvalidParams`
- Bad / missing token → generic `Unauthorized` (silent on bus, per M4.5 oracle-hardening rule)

Async because `make` / `cargo` / `npm install` can run tens of seconds — a synchronous tool would block the MCP HTTP request past the SDK timeout and conflate timeouts with build failures. Validates `service` via `game.is_known_service()` (M4.5).

### Events

| Event | Source | Trigger |
|-------|--------|---------|
| `SERVICE_RESTART_STARTED` | host | tool entry, before background task |
| `SERVICE_RESTART_COMPLETED` | host | background task exit 0 |
| `SERVICE_RESTART_FAILED` | host | background task non-zero or exception |
| `SERVICE_HEALTH_CHECK` | host | after COMPLETED, one per level (PROCESS→TCP→PROTOCOL→CHECKER) |
| `DEPLOY_COMPLETE` (M4.5) | agent via `report_deploy_complete` | agent's claim |

Restart-event payload fields (every restart event carries these):

- `job_id` — 12-hex correlation id, same for STARTED/COMPLETED/FAILED of the same job
- `team_id`, `service` — which team and which challenge
- STARTED additionally: `started_at` (unix ts)
- COMPLETED additionally: `duration_ms`
- FAILED additionally: `duration_ms`, `error` (short string)

`SERVICE_HEALTH_CHECK` payload fields: `team_id`, `service`, `level` (one of `process` / `tcp` / `protocol` / `checker`), `passed` (bool), `detail` (short string).

In-flight tracking lives on `GameMgr` as a dict keyed by `job_id`. A new restart for a `(team, service)` pair that already has a running job is rejected at the MCP layer (InvalidParams).

### Game loop hook

`gamemgr/game_mgr.py` subscribes to `PHASE_CHANGE → DEFENSE` and spawns
a defense agent per team via `ClaudeAgentManager` with a defense-mode
prompt. Fresh agent per phase (simpler than persistent).

---

## Health levels — defense soundness

Why the 4-layer split: agent could "patch" by stubbing out only the
vuln path. PROCESS / TCP / PROTOCOL all pass, but the challenge is
gameplay-dead. CHECKER catches this (the nautilus poller was built
exactly for this purpose).

| Level | Owner | What the agent could try to skip if this level didn't exist |
|-------|-------|---------------------------------------------------------------|
| PROCESS | orchestrator (`pgrep`) | `pkill ico` and don't restart |
| TCP | orchestrator (`socket.connect`) | Run `ico` bound to wrong port |
| PROTOCOL | `Service.is_alive(PROTOCOL)` | Replace `ico` with `socat - TCP-LISTEN:4265 EXEC:cat` |
| **CHECKER** | `Service.is_alive(CHECKER)` (ico: `/arena/checker.sh` — lifted nautilus poller) | Keep benign paths but no-op the vuln/flag path |

For `notes`: CHECKER ≡ PROTOCOL (notes has no separable gameplay surface — honest, not a workaround).

v1 emits the signal; **does not** enforce. Hollow patches show up as
`SERVICE_HEALTH_CHECK level=CHECKER passed=false` on the bus —
operator and future scoring see them. Pinned by
`test_dcf_health_levels.py` (the cheese-path test).

---

## Artifact persistence

Reserve `/artifacts/` structure now so future exploit / pcap / replay
work drops in without re-deciding paths. Each run directory
(`runs/<utc-ts>/`, existing from M4.5) gains an `artifacts/` subdir
alongside `events.jsonl`. Inside it, one subdir per artifact kind, each
laid out as `<kind>/team{N}/round-{N}/<filename>`.

Filled in v1:

- `api-logs/` — Claude CLI's stream-json transcript per agent run (every LLM turn: prompt, assistant message, tool_use, tool_result, usage tokens).
- `agent-logs/` — supervisor process-level diagnostics (stderr, exit_code). Distinct from `api-logs/` so post-mortems can grep one without the other.
- `service-logs/` — `docker logs` snapshot per `(team, challenge, round)`.

Reserved (empty in v1, named so consumers don't re-decide paths later):

- `source-snapshots/` — tar of `/srv/challenge/<chal>/` post-defense
- `binary-diffs/` — diff vs build-time baseline
- `pcaps/` — packet captures
- `exploits/` — attacker's exploit artifact
- `replay-bundles/` — packaged game-state snapshot for offline replay

New module `gamemgr/artifacts.py` exports:

- **`ArtifactKind` enum** — closed set covering the 8 slots above (`API_LOG`, `AGENT_LOG`, `SERVICE_LOG`, `SOURCE_SNAPSHOT`, `BINARY_DIFF`, `PCAP`, `EXPLOIT`, `REPLAY_BUNDLE`). New artifact type = one enum value, no per-caller path negotiation.
- **`ArtifactStore`** with `path_for(kind, *, team_id, round_id, filename) → Path`. Returns the stable path under `runs/<ts>/artifacts/<kind>/team{N}/round-{N}/<filename>`. Creates parent dirs lazily. Rejects `..` in filename (no escape outside `run_dir/artifacts/`).

**v1 callers** (3; rest reserved):
- `ClaudeAgentManager.fetch_logs()` at end of each phase → split:
  - **stdout** (Claude CLI's stream-json transcript) → `API_LOG` (`stream.jsonl`)
  - **stderr + exit_code** → `AGENT_LOG`
- `GameMgr` end-of-round hook → `docker logs ...` → `SERVICE_LOG`.

**Why split `api-logs/` from `agent-logs/`**: stream-json semantically captures
"what the LLM was asked and what it answered" — every turn's prompt, every
assistant message, every `tool_use` + `tool_result`, token usage, stop reasons.
That's the forensic answer to "what did the agent actually do?" — it deserves
a slot separate from the supervisor's process-level stderr (npm noise, ssh
warnings, exit codes) so post-mortems can grep one without the other.

**Redaction safety**: stream-json never contains the API key (sent over HTTPS
in headers by the CLI, never echoed to stdout). Safe to share. If a future
milestone adds an HTTP-proxy capture for raw request bodies, that path MUST
strip `Authorization` / `x-api-key` headers before writing into `api-logs/`.

---

## Config

`config/game.yaml` grows two sections:

- **`challenge_sources:`** — list of source declarations. Each entry has `source:` (matches a `ChallengeSource` subclass key, e.g. `notes` or `nautilus`). Nautilus-typed entries additionally take `repo_ref:` (vendor path) and `include:` (list of upstream challenge dir names to enable; v1 → `[ico]`).
- **`challenges:`** — list of game-level challenge declarations. Each entry has `name:` (referenced elsewhere), `source:` (must match an entry in `challenge_sources`), and `deploy_on:` (list of team ids, e.g. `[team1, team2]`).

v1 yaml declares: the built-in `notes` source + nautilus source with `include: [ico]`; two challenges (`notes` and `nautilus-ico`), both deployed on `team1` and `team2`.

---

## Tasks

| # | File | Task |
|---|------|------|
| 1 | `challenges/source.py` | `ChallengeSource` ABC + `ChallengeSpec` + `FlagMechanism` + `HealthLevel` enums + `Service.is_alive(level)` signature update |
| 2 | `challenges/notes.py` | `BuiltinNotesSource(ChallengeSource)`; extend `is_alive(level)`; CHECKER ≡ PROTOCOL (documented) |
| 3 | `challenges/nautilus.py` | `NautilusSource`; reads `spec.toml`; `build_image()` calls `docker build` |
| 4 | `challenges/nautilus/ico/` | `Dockerfile` (single-stage, fpc + fp-units; creates `arena_agent` non-root user; installs `/arena/restart.sh` + `/arena/checker.sh` both `root:root 555`; `/srv/challenge/ico/` chowned `arena_agent:arena_agent 755`; sshd with `PermitRootLogin no`), `spec.toml` (incl. `[health_check]`), `restart.sh`, `checker.sh` (lifts nautilus poller) |
| 5 | `challenges/registry.py` | Replace hardcoded dict with `ChallengeSource`-list discovery |
| 6 | `orchestrator/orchestrator.py` | No topology change. (Earlier draft proposed per-(team, challenge) containers + 3-net split; v1 keeps M5.6 topology — one `team{N}_prod` per team, `team{N}_net` + `game_net`.) |
| 7 | `gamemgr/config.py` | Parse `challenge_sources:` + new `challenges:` shape |
| 8 | `scripts/sync_nautilus.sh` | `git clone` + `git checkout <pinned-sha>` |
| 9 | `gamemgr/events.py` | Add `SERVICE_RESTART_STARTED` / `COMPLETED` / `FAILED` + `SERVICE_HEALTH_CHECK` |
| 10 | `gamemgr/game_mgr.py` | `restart_service(team_id, service) → job_id`: emit STARTED, spawn `asyncio.create_task` (run via `docker exec`, emit COMPLETED/FAILED with `duration_ms`); after COMPLETED, run all 4 health levels (emit one `SERVICE_HEALTH_CHECK` each); reject if (team, service) already in-flight. Track in `_restart_jobs`. Plus defense-phase subscriber |
| 11 | `gamemgr/mcp_server.py` | `restart_service(service)` MCP tool (4th on M4.5 server). Returns `{job_id}` synchronously; long work runs in GameMgr background |
| 12 | `gamemgr/artifacts.py` | `ArtifactKind` + `ArtifactStore.path_for(...)`. Wire end-of-phase + end-of-round hooks in `GameMgr`: agent stdout → `API_LOG`, stderr+exit_code → `AGENT_LOG`, `docker logs <team{N}_prod>` → `SERVICE_LOG` |
| 13 | `agents/prompts.py` | `build_defense_prompt(cfg, spec)` — edit / restart / report |
| 14 | `agents/claude.py` | `_provision_ssh_pivot` writes pubkey to `/home/arena_agent/.ssh/authorized_keys` (not `/root/.ssh/`) when target is a challenge-bearing prod (`spec.metadata.health_check` is set); dev-side `ssh_config` sets `User arena_agent` for `team{N}_prod`. Source view is baked into the image; no source-mount work here |
| 15 | `config/docker/Dockerfile.prod` | Stays as fallback for sources without their own image (notes) |
| 16 | `config/game.yaml` | Two-section split: `challenge_sources:` + `challenges:` |
| 17 | `tests/test_dcf_source_contract.py` | Parametrized over `notes` + `nautilus-ico` |
| 18 | `tests/test_dcf_nautilus_ico.py` | Build image, plant flag, port 4265 alive, TCP probe round-trips |
| 19 | `tests/test_dcf_restart_tool.py` | (a) async return shape `{job_id}`; (b) STARTED → COMPLETED ordering + matching `job_id`; (c) failure → FAILED with `error`; (d) concurrent restart rejected; (e) auth pinning (bad token, unknown service, cross-team unreachable) |
| 20 | `tests/test_dcf_filesystem_isolation.py` | Write to `/srv/challenge/ico/foo` in team1 invisible to team2 + invisible on host vendor dir |
| 21 | `tests/test_dcf_network_isolation.py` | M5.6 invariants still hold under the new image: (a) dev reaches own prod via `team{N}_net`; (b) dev cannot reach opposing prod (no interface on `game_net`); (c) DEFENSE detaches prod from `game_net` (existing M3 behavior unchanged) |
| 22 | `tests/test_dcf_artifact_store.py` | `path_for` returns stable paths; parents lazy-created; rejects `..` in filename; all `ArtifactKind` values distinct; mock `ClaudeAgentManager.fetch_logs()` → assert stdout lands under `api-logs/` and stderr under `agent-logs/` (split is verifiable, not just intent) |
| 23 | `tests/test_dcf_user_perms.py` | SSH in as `arena_agent` to a live `team1_chal_ico` container: (a) `id` returns non-zero UID; (b) `cat /arena/restart.sh` succeeds; (c) `echo x >> /arena/restart.sh` **fails** with permission denied; (d) write to `/srv/challenge/ico/foo` succeeds; (e) sshd rejects a `root@host` key (per `PermitRootLogin no`) |
| 24 | `tests/test_dcf_health_levels.py` | (a) known-good ico → all 4 levels pass + emit events; (b) **cheese-path**: ico binary replaced with `socat -` → PROCESS+TCP pass but PROTOCOL+CHECKER fail |
| 25 | `tests/test_dcf_round_loop_defense_phase.py` | Phase entry → defense kickoff fires; mocked agent records `DEPLOY_COMPLETE` |
| 26 | `tests/test_dcf_defense_workflow.py` (live, env-gated) | Real Claude agent runs defense recipe on ico end-to-end |
| 27 | `docs/DEFCON_challenge_framework_plan_v1.md` + `..._report_v1.md` | docs |
| 28 | `docs/design_v1.7.md` (new, supersedes v1.6 the way v1.6 superseded v1) | Written **after** implementation lands, capturing the actual post-DEFCON state: `ChallengeSource` + `ChallengeSpec` + `HealthLevel` framework, ico challenge running on the unchanged M5.6 topology (one `team{N}_prod` per team), defense-phase flow, MCP `restart_service` tool + restart event chain, `arena_agent` non-root SSH user, artifact persistence layout (api-logs split from agent-logs). v1.6 stays in `docs/` as the prior baseline, untouched |

---

## Verification

Test files to run (per task list, grouped by infra need):

- **No docker required**: `test_dcf_source_contract` (parametrized over `notes` + `nautilus-ico`), `test_dcf_restart_tool`, `test_dcf_artifact_store`, `test_dcf_round_loop_defense_phase`.
- **Docker required**: `test_dcf_nautilus_ico`, `test_dcf_filesystem_isolation`, `test_dcf_network_isolation`, `test_dcf_user_perms`, `test_dcf_health_levels`.
- **Live, env-gated** (needs real Claude API key): `test_dcf_defense_workflow`.
- **Full regression**: the entire `tests/` suite — existing 60+ tests must stay green.

Pass criteria (each maps to a test):

- Contract test green against `notes` and `nautilus-ico`.
- `ico`: image builds, flag plants at `/flag`, port 4265 reachable via the standard attack path, source baked at `/opt/challenge_src/ico/` and visible at `/srv/challenge/ico/`.
- **Auth invariant** — no token-path lets a team restart another team's container.
- **Async invariant** — `restart_service` returns `{job_id}` within MCP request budget; STARTED → COMPLETED|FAILED with matching `job_id`.
- **Filesystem isolation** — cross-team and host-vendor writes invisible.
- **Network isolation** — M3 invariants unchanged: dev never on `game_net`; cross-team reach only during ATTACK on `game_net`. Orchestrator is the only entity attaching/detaching.
- **In-container user perms** — agent SSHes in as non-root `arena_agent`; `/arena/*` framework scripts are `root:root 555` so the agent can read/exec but not modify; `/srv/challenge/<name>/` is agent-writable.
- **Cheese-path detection** — hollow patches (socat-stub) visibly fail PROTOCOL + CHECKER on the bus.
- Defense kickoff fires on DEFENSE entry; full restart + health chain lands on the bus.
- **Artifacts** — end of run, `runs/<utc-ts>/artifacts/{api-logs,agent-logs,service-logs}/` contain per-team-per-round files (`api-logs/.../stream.jsonl` = full LLM transcript); five reserved kinds exist as empty subdirs.
- Existing 60+ tests stay green.

---

## Rollback / safety

- **`vendor/` not synced** → `NautilusSource.list_challenges()` raises at setup with "run `scripts/sync_nautilus.sh`" before any container starts.
- **Per-challenge `docker build` fails** → `Orchestrator.setup` aborts before round loop. No half-deployed state.
- **Defense agent crashes** → attack phase still runs; that team's `DEPLOY_COMPLETE` doesn't fire that round.
- **Disable nautilus** → remove its block from `challenge_sources:`. `notes` keeps working.

---

## Open questions (resolve during impl)

- Pinned SHA for nautilus repo in `sync_nautilus.sh`.
- `is_alive(PROTOCOL)` for ico — minimal HVIF round-trip. Crib from nautilus poller.
- **`get_restart_status(job_id)` MCP tool**: deferred. v1 agent has only TCP-probe + event-bus signals for restart progress. Without this tool, an agent that misses the COMPLETED window (e.g. its turn ended before the build finished) has no clean way to recover. Add when LLM cost or missed-window incidents justify it. When it lands, `GameMgr._restart_jobs` retention grows from "drop on emit" to a bounded TTL (~5 min).
- Source-snapshot capture (fill `artifacts/source-snapshots/`) — cheap (~1 MB/round), useful for forensic replay. Lean: defer.
- Build-time test artifacts (`*.hvif`) — skip from runtime image.
- Seccomp (`use_naut_seccomp: true` upstream) — skip in v1, security gap.
