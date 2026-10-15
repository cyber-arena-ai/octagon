# Agent Prompt Composition

What text the competing agent actually receives, and where each part comes from
in code. The agent's context is **four layers**, but only two are ours to write
per challenge:

| Layer | What | Who provides it |
|---|---|---|
| **S0** | The CLI's own system prompt (claude/codex/opencode) | the vendor CLI — we don't touch it |
| **S1** | The **MCP tool schema** — every tool's name, arguments, and return shape | auto-injected by the MCP server (`gamemgr/mcp_server.py`) when the CLI connects; the model reads it directly |
| **L1** | The **shared briefing** — game rules, strategy, tool roster, the SSH/patch mechanic | framework, identical for every challenge (`agents/prompts/_l1.py`) |
| **L2** | The **per-challenge briefing** — service brief, source map, connection facts, hint | the challenge (`challenges/<pkg>/prompts.py::build_advanced_prompt`) |

The prompt we hand the agent (its single opening user turn, re-sent verbatim on
any supervised restart) is **L1 + L2**. Tool mechanics live in S1, so neither L1
nor L2 restates a tool's arguments or returns — they only *name* the tool.

## The formula (async mode — the only mode that ships)

```
AgentConfig.system_prompt = build_async_prompt(
    cfg, service_name=..., total_rounds=..., round_min=..., hint=...)

build_async_prompt  =  render_tools( build_l1(cfg, total_rounds, round_min)
                                     + "\n\n"
                                     + mod.build_advanced_prompt(cfg, service_name, hint),
                                     cfg )
```

- `build_l1` (`agents/prompts/_l1.py`) returns the shared briefing, byte-identical
  across challenges except the four config slots it fills by `str.replace`:
  `{team_id}`, `{total_rounds}`, `{round_min}`, `{own_prod_host}`.
- `mod` is the challenge's adjacent prompt module. The challenge registry maps
  `service_name` (== `Challenge.name`) to its owning package and the assembler
  imports `challenges.<pkg>.prompts`; there is no manual prompt map. Its
  `build_advanced_prompt(cfg, *, service_name, hint)` returns the L2 body
  (SERVICE / SOURCE MAP / CONNECTION FACTS / ending), filling `{opposing_host}`
  and `{service_name}`. The ending is the challenge's `Hint:` line when
  `hint=True`, else the fixed no-hint sentence.
- `render_tools` (`agents/prompts/_helpers.py`) rewrites every canonical tool
  name to the running provider's form, so L1 and L2 can write tool names in one
  canonical backticked style (`` `submit_flag` ``) and stay provider-agnostic:

  | provider | `submit_flag` renders as |
  |---|---|
  | claude (default) | `mcp__cyberarena__submit_flag` |
  | qwen | `mcp__cyberarena__submit_flag` (qwen always fully-qualifies → same as claude) |
  | codex | `submit_flag` (bare) |
  | nexau | `submit_flag` (bare) |
  | openhands | `submit_flag` (bare) |
  | opencode | `cyberarena_submit_flag` (single `_`) |
  | grok | `cyberarena__submit_flag` (double `__`) |

  The form is dictated by each CLI's own MCP client, not by us — it is whatever
  name that CLI actually exposes to its model, so a prompt that used the wrong
  one would tell the model to call a tool that does not exist. Anything
  unrecognized falls through to the claude form.

## Config + difficulty plumbing (`gamemgr/game_mgr.py`)

`async_agent_config_for(team_id)` picks the team's deployed challenge, then
`_system_prompt_for(cfg, service_name)` supplies the run parameters and calls
`build_async_prompt`:

- `total_rounds` and `round_min` come from `RoundConfig` (`round_secs // 60`);
- `hint` comes from the challenge's game.yaml **`ChallengeConfig.level`** —
  `level: hard` → `hint=False` (no-hint sentence), anything else → the `Hint:`
  line.

**Setting it in a game.yaml.** `level` is a per-challenge field (default
`"easy"`), a sibling of `deploy_on` under `challenges:`. Omit it (or `easy`)
for the hinted briefing; set `hard` to drop the hint so the agent must find the
vulnerability unaided:

```yaml
challenges:
  - name: 8-bit-oracle
    type: 8-bit-oracle
    port: 17280
    deploy_on: [team1, team2]
    level: hard          # default "easy" (hinted); "hard" = no hint
```

The `config/v3_example/*.yaml` configs carry this as a commented-out line for
reference.

**`source_in_dev` (per-challenge bool, default `false`).** A sibling of `level`
under `challenges:`, and likewise a prompt-consumed signal — it changes no
framework wiring on its own. Default `false` keeps today's behavior: the
vulnerable-service source lives only on prod (baked into the challenge image)
and the agent reaches it over the SSH pivot. Set `true` to declare that the
agent should work on a local copy of the source inside its own dev container so
its local filesystem tools apply (the source is expected to be provisioned /
copied in per the challenge's prompt, e.g. pulled from prod over the pivot):

```yaml
challenges:
  - name: 8-bit-oracle
    type: 8-bit-oracle
    port: 17280
    deploy_on: [team1, team2]
    source_in_dev: true    # default false: source only on prod, reached over SSH
```

Current state: the config field is parsed (`ChallengeConfig.source_in_dev`,
`gamemgr/config.py`); consuming it in the briefing is pending — read `c.source_in_dev`
in `_system_prompt_for` alongside `hint` and thread it into `build_async_prompt`.
The `config/v3_example/*.yaml` configs carry it as a commented-out line for reference.

The per-team `AgentConfig` fields spliced into L1/L2 (`team_id`,
`own_prod_host`, `opposing_prod_hosts`, `team_token`, `submit_url`,
`mcp_endpoint`, `provider`/`model`) are set here before the prompt is built.

## Async-only

There is no separate ATTACK / DEFENSE prompt and no `WRONG_PHASE` gate: attack
and defense run simultaneously for the whole game. The retired sync per-phase
config builders (`attack_agent_config_for` / `defense_agent_config_for`) delegate
to the same `_system_prompt_for`, so there is exactly one prompt path.

## Adding a new challenge's prompt

1. Write `challenges/<pkg>/prompts.py` exposing
   `build_advanced_prompt(cfg, *, service_name, hint=True)` that returns the L2
   body — SERVICE, SOURCE MAP, CONNECTION FACTS, and a one-line `Hint:` —
   following the standard in [CHALLENGE_AUTHORING_SOP.md](CHALLENGE_AUTHORING_SOP.md)
   (no vulnerability class, hotspot, exploit, patch, flag location, or equivalent
   answer material in either hint variant). Keep tool names canonical and
   backticked; `render_tools` handles providers.
2. That's it — the registry derives the prompt module from the discovered
   `Challenge` package, L1 is shared, and S1 (tool mechanics) is injected
   automatically.
