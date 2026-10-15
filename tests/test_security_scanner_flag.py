"""game.yaml `agent.security_scanner` -> the scanner is advertised to the model as an MCP
tool instead of sitting on PATH unannounced.

These tests exist because of a live counter-example in this repo: `challenges[].level` is
declared, documented, consumed by the game loop, and advertised in every sample config — yet
`_challenge_from_dict` never parses it, so writing `level: hard` silently does nothing. A
half-wired knob is the worst benchmark outcome: both arms end up identical and nothing says
so. So the whole path is pinned here, from the yaml key to the emitted config.toml.
"""

from __future__ import annotations

import pytest

from agents.codex import CodexAgentManager
from gamemgr.config import load
from models.agent import AgentConfig

_YAML = """
teams:
  - id: team1
    dev: team1_dev
    prod: team1_prod
    agent:
      cli: codex exec
      provider: codex
      model: gpt-5.6-sol
{extra}
  - id: team2
    dev: team2_dev
    prod: team2_prod
    agent:
      cli: codex exec
      provider: codex
      model: gpt-5.6-sol
nodes: []
networks: {{}}
"""


def _cfg_from_yaml(tmp_path, extra: str) -> AgentConfig:
    p = tmp_path / "game.yaml"
    p.write_text(_YAML.format(extra=extra))
    return load(p, env_file=None).agents["team1"]


def test_absent_key_defaults_to_off(tmp_path):
    assert _cfg_from_yaml(tmp_path, "").security_scanner is False


def test_true_reaches_the_agent_config(tmp_path):
    """The `level`-shaped failure: the loader must actually pass the key through."""
    assert _cfg_from_yaml(tmp_path, "      security_scanner: true").security_scanner is True


def test_false_stays_off(tmp_path):
    assert _cfg_from_yaml(tmp_path, "      security_scanner: false").security_scanner is False


def test_typo_is_rejected_not_dropped(tmp_path):
    """The agent block is the one block with a hard allow-list — a misspelled key must fail
    the load rather than silently leave the team in the control arm."""
    with pytest.raises(ValueError, match="security_scannerr|allowed keys"):
        _cfg_from_yaml(tmp_path, "      security_scannerr: true")


def test_flag_changes_the_emitted_codex_config(tmp_path):
    """End to end: yaml key -> AgentConfig -> the config.toml codex actually reads."""
    mgr = CodexAgentManager(transport=object(), client=None)
    off = mgr._build_config_toml(_cfg_from_yaml(tmp_path, ""))
    on = mgr._build_config_toml(
        _cfg_from_yaml(tmp_path, "      security_scanner: true"))
    assert "[mcp_servers.arena_scanner]" not in off
    assert "[mcp_servers.arena_scanner]" in on
    assert '"/usr/local/bin/codex-security-mcp"' in on


def test_scanner_server_forwards_the_credentials_it_needs(tmp_path):
    """Regression for a shipped-and-broken bug: codex hands a stdio MCP child ONLY
    HOME/LC_CTYPE/PATH (measured against 0.146.0), so without an explicit forward the
    server shells out to `codex-security` with no key and no router, and every tool call
    fails — an advertised tool that never works. `env_vars` forwards by NAME, so the
    principal token is not copied into config.toml."""
    mgr = CodexAgentManager(transport=object(), client=None)
    on = mgr._build_config_toml(
        _cfg_from_yaml(tmp_path, "      security_scanner: true"))
    scanner_block = on.split("[mcp_servers.arena_scanner]", 1)[1]
    assert "env_vars" in scanner_block
    assert "CODEX_API_KEY" in scanner_block   # the principal token, by name
    assert "CODEX_HOME" in scanner_block      # lets the wrapper read base_url from config.toml
    # forwarded by name, never inlined as a literal value
    assert "sk-" not in scanner_block
