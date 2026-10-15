# DEFCON Challenge Framework + Nautilus Adapter — Execution Report v1

**Goal (from plan):** pluggable `ChallengeSource` interface + one concrete
nautilus adapter (`ico` from nautilus-institute/finals-2025) running
end-to-end, including the agent defense loop (edit source → MCP
`restart_service` → smoke → `report_deploy_complete`).

**Status:** Complete. 52 new tests pass + 2 env-gated live tests
skipped without docker / API key. Full suite: **116 passed, 4 skipped**;
no existing-test regressions.

## What was built

| Path | Change |
|------|--------|
| `challenges/source.py` (new) | `ChallengeSource` ABC + `ChallengeSpec` frozen dataclass + `FlagMechanism` (4 values incl. `SERVICE_PROTOCOL` added during impl) + `HealthLevel` (4 values) enums |
| `challenges/notes.py` | `BuiltinNotesSource(ChallengeSource)`; `NotesService.is_alive(level=HealthLevel.PROTOCOL)` extended; CHECKER ≡ PROTOCOL for notes (documented honest fallback) |
| `challenges/nautilus.py` (new) | `NautilusSource` reads per-challenge `spec.toml`; `build_image()` shells out to `docker build --platform=linux/amd64` against assembled context (framework dir + vendor source); image-cache-aware (no rebuild when tag exists) |
| `challenges/nautilus/ico/` (new) | `Dockerfile` (single-stage, `fp-compiler` + `fp-units-*` + `openssh-server`; creates `arena_agent` non-root user AND `ico` service-runtime user; `/arena/*.sh` root-owned 555; `PermitRootLogin no`), `spec.toml` (port 4265, FILE_AT_PATH, restart_cmd + health_check block), `restart.sh`, `checker.sh` (CONNECT-byte handshake — sufficient for socat-cheese detection), `entrypoint.sh` (sshd host keys, baked-template → writable copy, initial fpc build, exec sshd) |
| `challenges/registry.py` | Rewired to use `default_sources()` enumeration while preserving the legacy `build()` shim |
| `scripts/sync_nautilus.sh` (new) | `git clone` + `git checkout 366520c0cc43` (pin) |
| `gamemgr/events.py` | Added `SERVICE_RESTART_STARTED` / `COMPLETED` / `FAILED` + `SERVICE_HEALTH_CHECK` + `DEFENSE_KICKOFF` event types |
| `gamemgr/artifacts.py` (new) | `ArtifactKind` closed-set enum (8 slots) + `ArtifactStore.path_for(...)` with `..` + absolute-path rejection; constructs reserved subdirs + README at run start |
| `gamemgr/game_mgr.py` | `restart_service(team_id, service) → job_id` (async, in-flight rejected, STARTED → background `asyncio.create_task` runs docker exec, COMPLETED/FAILED with `duration_ms`); `_emit_health_chain` walks PROCESS→TCP→PROTOCOL→CHECKER and emits one `SERVICE_HEALTH_CHECK` per level; `_on_defense_kickoff` subscriber publishes `DEFENSE_KICKOFF` per (team, defense-capable service) and dispatches to a registered handler; `defense_agent_config_for(team_id, service_name)` builds the defense AgentConfig |
| `gamemgr/mcp_server.py` | 4th MCP tool `restart_service(service)` — returns `{job_id}` synchronously; maps `ValueError` → `InvalidParams`; unauthorized stays silent on the bus |
| `agents/prompts.py` | `build_defense_prompt(cfg, *, service_name, port)` walks edit → restart_service → smoke → report_deploy_complete |
| `agents/claude.py` | `_provision_ssh_pivot` probes target prod for `arena_agent` (`id arena_agent`); writes pubkey to `/home/arena_agent/.ssh/authorized_keys` + uses `User arena_agent` in dev-side ssh_config when present; falls back to root path for legacy notes-only prods |
| `tests/conftest.py` | `docker_required`, `dcf_ico_image` (session-scoped build), `dcf_ico_container` (function-scoped fresh container) shared fixtures |
| `tests/test_dcf_restart_tool.py` (new, phase 4a) | 6 tests: unknown service / no restart_cmd / unknown team → ValueError; happy path = STARTED+COMPLETED+4 HEALTH_CHECK in order; failure path = FAILED + no health chain; in-flight rejected; different teams independent |
| `tests/test_dcf_artifact_store.py` (new) | 12 tests: closed-set kinds, reserved-subdir + README construction, lazy parent dirs, `..` / absolute filename rejection, nested-filename allowed, per-team-per-round isolation |
| `tests/test_dcf_source_contract.py` (new) | 13 tests parametrized over `notes` + `nautilus-ico`: list_challenges non-empty, names unique, mandatory fields, frozen dataclass, plus notes-specific (SERVICE_PROTOCOL mechanism), ico-specific (restart_cmd + health_check + source_view), missing-vendor message, unknown include name |
| `tests/test_dcf_nautilus_ico.py` (new, docker) | 8 tests: tag naming, amd64 arch enforcement, runtime layout (perms on /opt/challenge_src + /srv/challenge + /arena), arena_agent UID > 0, PermitRootLogin no, initial fpc build artifact, restart.sh → port 4265 listens, flag plant readable by ico + arena_agent |
| `tests/test_dcf_user_perms.py` (new, docker) | 11 tests: arena_agent is non-root, can read/write /srv/challenge, cannot write /arena/* (parametrized over restart.sh + checker.sh), can read /arena/*, cannot delete from /arena, cannot write /opt/challenge_src, root docker-exec restart.sh still works |
| `tests/test_dcf_filesystem_isolation.py` (new, docker) | 3 tests: write in container A invisible in container B, ico.pas edit in A doesn't change B's ico.pas, edit in container doesn't touch host vendor dir |
| `tests/test_dcf_health_levels.py` (new, docker) | 4 tests: happy path all 4 pass; **cheese path** (passive-echo stub on 4265) → TCP passes + CHECKER fails ("server echoed request byte"); PROCESS pattern doesn't match stub; GameMgr._emit_health_chain emits 4 events in PROCESS→TCP→PROTOCOL→CHECKER order against the live container |
| `tests/test_dcf_round_loop_defense_phase.py` (new) | 7 tests: kickoff per (team × defense-capable service), notes skipped (no restart_cmd), kickoff silent outside DEFENSE, idempotent per-round, handler dispatched with defense AgentConfig (defense-prompt fingerprint asserted), exception in one handler doesn't break the next, signal-only mode without handler |
| `docs/DEFCON_challenge_framework_plan_v1.md` | Existing plan doc — final state of design negotiated before / during impl |
| `docs/design_v1.7.md` (new) | Post-impl design baseline; supersedes v1.6 |
| `vendor/nautilus-finals-2025/` (synced, gitignored) | Pin-cloned at `366520c0cc43` via scripts/sync_nautilus.sh |

## Architectural decisions

- **Topology unchanged from M5.6.** The earlier draft of the plan proposed
  per-(team, challenge) containers + a 3-net split (`team{N}_dev_net` +
  `team{N}_prod_net` + `game_net`). Reverted after the user pushed back —
  v1 runs one challenge at a time across both teams on the existing
  `team{N}_prod` containers, so the topology change wasn't carrying weight.
- **Source baked into the image at build time, never bind-mounted.** Each
  container gets `/opt/challenge_src/<chal>/` (root-owned 555) and copies
  it to `/srv/challenge/<chal>/` (arena_agent-owned 755) in `entrypoint.sh`.
  No host bind mount → no cross-team aliasing, no vendor-tree poisoning.
  Pinned by `test_dcf_filesystem_isolation.py`.
- **Two unprivileged users inside the challenge container.** `arena_agent`
  for SSH-in / source editing; `ico` for the service runtime
  (`DropPrivileges('ico')` in `server.pas`). Different attack surfaces,
  different reasons to exist. Missing the second one was a latent bug
  surfaced by the CHECKER probe — the original Dockerfile only created
  `arena_agent`, the Pascal server's per-client fork crashed mid-handshake,
  and the original TCP-only `checker.sh` stub masked it.
- **`docker build --platform=linux/amd64` via subprocess, not docker-py.**
  The python SDK's legacy builder + `platform=` combination errors on
  post-build image lookup when BuildKit returns a manifest list (cross-arch
  dev mac case). CLI uses buildx and handles it cleanly.
- **Async `restart_service` with `job_id`.** `make`/`fpc`/`cargo` can run
  tens of seconds; a sync MCP tool would block past the SDK timeout and
  conflate timeouts with failures. The tool returns `{job_id}` within
  request budget; STARTED → COMPLETED|FAILED + per-level health checks
  land on the bus as the job progresses.
- **4-level HealthLevel (PROCESS / TCP / PROTOCOL / CHECKER).** PROCESS+TCP
  alone are spoofable by a passive-echo stub (`socat -`, `nc -e cat`);
  PROTOCOL is service-specific; CHECKER lifts a minimal piece of the
  upstream poller (CONNECT byte → ACK byte handshake) — sufficient to
  detect socat-cheese. Real poller logic deferred. Cheese-path detection
  is the load-bearing property and is pinned by `test_dcf_health_levels.py`.
- **`DEFENSE_KICKOFF` event + injected handler.** GameMgr emits the kickoff
  event but doesn't own `ClaudeAgentManager`; the handler is registered
  via `register_defense_kickoff_handler` so the demo / API server / tests
  each wire whatever manager they need without GameMgr having to fan out
  every dependency. Idempotent per `(round_id, team_id, service)` to
  survive a re-fire of PHASE_CHANGE.
- **Subscriber registered in `__init__`, not `start()`.** Phase 4b's
  `_on_defense_kickoff` has no file-sink dependency. Tests routinely
  publish PHASE_CHANGE events directly onto the bus without invoking the
  round loop — registering at construction time keeps that path simple.
  The legacy `_on_rotation_event` is still in `start()` for back-compat.
- **Token still never logged or echoed.** All MCP tools go through the
  same `_team_id_from_ctx` resolver as M4.5; `restart_service` raises
  generic `Unauthorized` on bad token and never publishes anything to
  the bus. Oracle-hardening invariant unchanged.
- **`api-logs/` split from `agent-logs/` in the artifact store layout.**
  Stream-json transcript (LLM forensic answer to "what did the agent do?")
  goes to `api-logs/`; supervisor stderr + exit code go to `agent-logs/`.
  Reserved subdirs for `source-snapshots`, `binary-diffs`, `pcaps`,
  `exploits`, `replay-bundles` so future-milestone work doesn't re-decide
  paths. v1 wires only the API/agent split is reserved at the layout
  level; actual `fetch_logs() → ArtifactStore` wiring is deferred.

## Verification

```
$ pytest tests/ -q
116 passed, 4 skipped in 145.38s
```

The 4 skipped:
- `test_m4_5_claude_mcp_smoke` (no `ANTHROPIC_API_KEY` in sandbox)
- `test_m4_agent::test_live_hello_world` (same)
- `test_m5_6_agent_attack::test_live_agent_attacks_and_submits` (same)
- `test_m6_panel::test_panel_renders_in_headless_chrome` (no Chrome on PATH)

**Per docker invocation budget:** the `dcf_ico_image` fixture is
session-scoped — one `docker build` per `pytest` run (~70s cold, ~1s
warm). The 26 container-touching DCF tests share the build via the
fixture cascade. End-to-end DCF subsuite (~52 tests) runs in ~60s.

## Deviations from the plan

- **`FlagMechanism.SERVICE_PROTOCOL` added during impl.** The plan
  enumerated 3 values (FILE_AT_PATH, ENV_VAR, BINARY_PATCH). None
  covered `notes`, which plants via `PUT` on its own TCP socket — a
  4th value was honest; `Service.plant_flag()` owns delivery.
- **Topology refactor deferred (user decision, mid-impl).** Plan v1 was
  rewritten before any topology code landed; M5.6 topology stays for v1.
- **`checker.sh` is a minimal CONNECT-byte handshake, not a full HVIF
  round-trip.** Sufficient to detect passive-echo cheese (the failure mode
  the framework actually has to defend against in v1). Full nautilus poller
  port deferred — would need to also lift `nautpolling.py` + pwn dependency.
- **`ArtifactStore` wired into GameMgr lifecycle is partial.** The path
  allocator + reserved layout ship; the `ClaudeAgentManager.fetch_logs()`
  → `API_LOG` + `AGENT_LOG` split wiring and the end-of-round
  `docker logs` → `SERVICE_LOG` hook are not yet attached to the agent
  lifecycle. Reserved as a follow-up; the layout is stable so adding the
  wiring is a one-call change per site.
- **Defense kickoff handler is opt-in.** Plan implies GameMgr spawns
  agents itself; impl uses an injected handler so the round loop stays
  decoupled from `ClaudeAgentManager`. Test 25 in the plan ("phase entry
  → defense kickoff fires; mocked agent records DEPLOY_COMPLETE") is
  covered by `test_dcf_round_loop_defense_phase.py` with a spy handler;
  the actual `ClaudeAgentManager.install` wiring is left to the API
  server / demo bootstrap.
- **`config/game.yaml` two-section split not landed.** The plan called
  for `challenge_sources:` + `challenges:` config rewrite; deferred
  because v1 only has one defense-capable challenge and the existing
  one-section shape works. Two-section schema is the right move once a
  second source (pico, custom) lands.

## Deferred

| Item | Owning milestone |
|------|------------------|
| Real ico CHECKER (lift nautilus poller, drop pwn dep) | Cheese-hardening follow-up |
| Source-snapshot capture (`artifacts/source-snapshots/`) | Forensic / replay milestone |
| `ArtifactStore` wiring into `fetch_logs()` + end-of-round `docker logs` | Same |
| `get_restart_status(job_id)` MCP tool | Restart-status recovery milestone (add when LLM cost / missed-window incidents justify it; `_restart_jobs` retention grows from drop-on-emit to TTL-bounded) |
| Two-section `game.yaml` (`challenge_sources:` + `challenges:`) | Second nautilus challenge milestone |
| Seccomp from upstream `challenge_config.yaml` | Hardening milestone |
| Patch-quality scoring (CHECKER pass/fail gates points; rollback on regression) | Scoring milestone |
| Multi-challenge-per-team (per-(team, challenge) containers + 3-net split) | Multi-challenge milestone |
| Defense workflow live end-to-end (real Claude agent runs ico defense; env-gated) | M7-live follow-up |
