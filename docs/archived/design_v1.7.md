# Cyber Arena — System Design (v1.7)

> Current system design. Supersedes
> [design_v1.6.md](design_v1.6.md); milestone-by-milestone
> lineage lives under [archived/](./). The live architectural
> diagrams live in [UML/](../UML/).

## What it is

Two-team Attack/Defense CTF arena. Each round has an **ATTACK** phase
(cross-team networks open; the attacker agent steals flags) and a
**DEFENSE** phase (cross-team closed; the defender agent patches its
own service via SSH + `restart_service`). Run via
`python main.py {setup,start,teardown}` against a `config/*.yaml`; the
live observation panel is at `http://localhost:8000/panel`.

## Modules

| Module | Responsibility |
|---|---|
| `main.py` | CLI; wires `Orchestrator` + `GameMgr` + per-team `AgentManager` from YAML config |
| `orchestrator/` | Docker network + container lifecycle; phase-driven `set_cross_team_network(allow)` |
| `gamemgr/` | Round loop, `Timeline` (append-only telemetry), MCP server, REST API, panel server |
| `flag/` | `FlagStore` (state + submit decision) + `FlagServer` (plant / rotate / submit verify) |
| `health/` | `HealthChecker` (`CheckResult` tree probe) + `HealthPoller` (ATTACK-only periodic + on-demand) |
| `challenges/` | Pluggable `Challenge` ABC; one directory per challenge. See [challenge_interface_v2_plan.md](challenge_interface_v2_plan.md) + [CHALLENGE_AUTHORING_SOP.md](../CHALLENGE_AUTHORING_SOP.md). |
| `agents/` | Pluggable agent CLIs (Claude Code today); install once + `claude -p --continue` per phase via `AgentTransport`. See [persistent_memory_agent_plan_v1.md](persistent_memory_agent_plan_v1.md). |
| `panel/` | Live SVG swim-lane UI; consumes Timeline via `attach_stream` |

`Timeline` is append-only telemetry — no subscribers drive internal
logic. Control flow goes through direct `GameMgr` methods
(`enter_defense_phase`, `enter_attack_phase`, `on_deploy_complete`,
`record_agent_ready`, …). Slow work runs in fire-and-forget
`asyncio.create_task` so no consumer can block another.

## Topology

Per team: `team{N}_prod` (vulnerable service) + `team{N}_dev` (agent
host). Networks: `team{N}_net` always attached; `game_net` +
`attack_net_<team>` are cross-team and toggled per phase. ATTACK
attaches them; DEFENSE detaches. Each `team{N}_prod` is launched from
the challenge's own image (e.g. `cyberarena/chal-nautilus-ico:<v>`);
dev runs `cyberarena/dev-base`.

## Defense workflow

```
RoundLoop._enter_defense
  → game.enter_defense_phase(round_id):
      timeline.record(PHASE_CHANGE, phase=defense)
      health_poller.on_attack_exit()
      _rotate_flags(round_id)           # plant + FLAG_PLANTED
      _spawn_defense_kickoff(round_id):
        for each (team, defense-capable challenge):
          timeline.record(DEFENSE_KICKOFF)
          asyncio.create_task(handler(team_id, …))
```

Agent recipe (per challenge's `build_defense_prompt`):

1. SSH to own prod as `arena_agent`.
2. Edit `/srv/challenge/<svc>/` (writable copy of the baked source).
3. Call MCP `restart_service(service)` → `{job_id}`; the framework
   delegates to the challenge's `RestartHandler`, which executes
   via whatever mechanism `VulboxTarget` provides (docker exec
   today; SSH / k8s exec / VM agent if `target.meta` is swapped).
4. Probe own port until reachable; call `report_deploy_complete`.

The restart emits `SERVICE_RESTART_STARTED` →
`COMPLETED|FAILED`, then one `SERVICE_HEALTH_CHECK` per `CheckResult`
leaf. Leaf names are challenge-defined; the bundled `nautilus_ico`
uses `process` / `tcp` / `protocol` / `checker` and others can pick
whatever shape fits (web → `http` + `checker`, db → `sql` + `checker`, …).
`DEPLOY_COMPLETE` is agent-triggered; `duration_ms` is host-computed
from the `DEFENSE_KICKOFF` timestamp (agent-self-reported values were
unreliable in live runs).

During ATTACK, `HealthPoller` fires the same chain on a per-challenge
`health_interval_secs` cadence so the panel reflects ongoing service
health, not just post-restart snapshots.

## Container layout (per challenge)

Baked at image build, materialized by `entrypoint.sh`:

| Path | Owner | Mode | Notes |
|---|---|---|---|
| `/opt/challenge_src/<svc>/` | `root` | 555 | read-only template, baked into the image |
| `/srv/challenge/<svc>/` | `arena_agent` | 755 | writable per-container copy (agent's edit surface) |
| `/arena/{restart,checker,entrypoint}.sh` | `root` | 555 | framework scripts; agent-readable, not writable |

`arena_agent` is the SSH user; `PermitRootLogin no`. Some services run
as a separate runtime user (e.g. ico drops privs to a non-`arena_agent`
user mid-fork) — that's owned by the challenge image.

`<svc>` is the in-image service slug chosen by the challenge author
(e.g. `ico`); it does not need to match either the snake-case
directory or kebab-case `Challenge.name`. See
[CHALLENGE_AUTHORING_SOP.md](../CHALLENGE_AUTHORING_SOP.md) for the
contributor-facing version of this layout.

## Event types

| Category | Events |
|---|---|
| Round / phase | `ROUND_START`, `PHASE_CHANGE`, `ROUND_STOP`, `GAME_END` |
| Deploy + flags | `SERVICE_DEPLOYED`, `FLAG_PLANTED`, `FLAG_SUBMITTED` |
| Agent lifecycle | `AGENT_INSTALLED`, `AGENT_READY`, `AGENT_PROMPT_SENT`, `AGENT_COMPLETED`, `AGENT_TIMEOUT`, `AGENT_FAILED` |
| Round kickoff | `DEFENSE_KICKOFF`, `ATTACK_KICKOFF`, `DEPLOY_COMPLETE` |
| Defense flow | `SERVICE_RESTART_STARTED`, `SERVICE_RESTART_COMPLETED`, `SERVICE_RESTART_FAILED`, `SERVICE_HEALTH_CHECK` |
| Pre-game smoke | `DRY_RUN_START`, `DRY_RUN_OK`, `DRY_RUN_FAILED` |

## MCP tools

All resolve team via `X-Team-Token`; bad token returns generic
`Unauthorized` and emits nothing on the timeline (no token oracle).

| Tool | Returns |
|---|---|
| `submit_flag(flag)` | `{accepted, reason, detail}` — submitting during DEFENSE rejects with `reason=WRONG_PHASE` *before* the FlagStore is consulted, so the team's `DUPLICATE` slot isn't burned. |
| `report_ready()` | `{ok: true}` |
| `report_deploy_complete(service)` | `{ok: true}` |
| `restart_service(service)` | `{job_id}` — async; status arrives via the restart event chain above. |

## Configuration

`config/<game>.yaml` sections: `topology`, `teams`, `nodes`,
`networks`, `agents`, `challenges`, `round_config`. Two ready-made
configs ship: `game.yaml` (notes smoke test) and `game_ico.yaml`
(DEF CON 2025 ico, full attack + defense). Per-team API keys via
`.env`: `ANTHROPIC_API_KEY_TEAM{1,2}` (plus matching `_BASE_URL` if
proxied).
