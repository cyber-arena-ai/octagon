"""game.yaml `agent.append_system_prompt` -> appended to the agent's staged
prompt via `AgentConfig.full_prompt`. CLI-agnostic (claude / codex / opencode all
read the one staged prompt file); opt-in, so empty leaves the prompt unchanged.
It's a plain string written to a file as bytes — no shell, no escaping.
"""

from __future__ import annotations

from gamemgr.config import load
from models.agent import AgentConfig

_YAML = """
teams:
  - id: team1
    dev: team1_dev
    prod: team1_prod
    agent:
      cli: claude -p
      model: claude-sonnet-4-6
{extra}
  - id: team2
    dev: team2_dev
    prod: team2_prod
nodes: []
networks: {{}}
"""


def _cfg_from_yaml(tmp_path, extra: str) -> AgentConfig:
    p = tmp_path / "game.yaml"
    p.write_text(_YAML.format(extra=extra))
    return load(p, env_file=None).agents["team1"]


def test_config_parses_append_system_prompt(tmp_path):
    cfg = _cfg_from_yaml(tmp_path, '      append_system_prompt: "Play aggressively."')
    assert cfg.append_system_prompt == "Play aggressively."


def test_config_defaults_append_system_prompt_empty(tmp_path):
    cfg = _cfg_from_yaml(tmp_path, "")  # field omitted
    assert cfg.append_system_prompt == ""


def test_full_prompt_appends_addendum():
    cfg = AgentConfig(cli="claude", system_prompt="BRIEFING", append_system_prompt="STRATEGY")
    assert cfg.full_prompt() == "BRIEFING\n\nSTRATEGY"


def test_full_prompt_unchanged_when_addendum_empty():
    cfg = AgentConfig(cli="claude", system_prompt="BRIEFING")
    assert cfg.full_prompt() == "BRIEFING"  # byte-for-byte the original prompt


def test_full_prompt_is_just_addendum_when_no_briefing():
    cfg = AgentConfig(cli="claude", append_system_prompt="STRATEGY")
    assert cfg.full_prompt() == "STRATEGY"


def test_full_prompt_carries_arbitrary_content_verbatim():
    # No shell anywhere (written to a file as bytes), so quotes / $ / backticks /
    # backslashes / newlines / unicode survive untouched and nothing executes.
    add = (
        'has "double" and \'single\', $VAR, `backticks`, a\\backslash,\n'
        "second line; rm -rf / (inert), 中文 canary 我已acknowledge"
    )
    cfg = AgentConfig(cli="claude", system_prompt="BRIEFING", append_system_prompt=add)
    assert cfg.full_prompt() == f"BRIEFING\n\n{add}"


def test_two_teams_get_independent_addenda(tmp_path):
    yaml = """
teams:
  - id: team1
    dev: team1_dev
    prod: team1_prod
    agent:
      cli: claude -p
      model: claude-sonnet-4-6
      append_system_prompt: "TEAM1 strategy"
  - id: team2
    dev: team2_dev
    prod: team2_prod
    agent:
      provider: codex
      cli: codex exec
      model: gpt-5.3-codex
      append_system_prompt: "TEAM2 different strategy"
nodes: []
networks: {}
"""
    (tmp_path / "game.yaml").write_text(yaml)
    agents = load(tmp_path / "game.yaml", env_file=None).agents
    assert agents["team1"].append_system_prompt == "TEAM1 strategy"
    assert agents["team2"].append_system_prompt == "TEAM2 different strategy"
    # each team's full_prompt carries only its own addendum
    a1 = AgentConfig(cli="x", system_prompt="B1", append_system_prompt=agents["team1"].append_system_prompt)
    a2 = AgentConfig(cli="x", system_prompt="B2", append_system_prompt=agents["team2"].append_system_prompt)
    assert a1.full_prompt() == "B1\n\nTEAM1 strategy"
    assert a2.full_prompt() == "B2\n\nTEAM2 different strategy"
