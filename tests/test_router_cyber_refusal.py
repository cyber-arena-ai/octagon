"""Harvest-side attribution for OpenAI cyber-policy refusals.

The api-router tags a mid-stream `code:cyber_policy` refusal with
`refusal_category:"cyber"` (see test_apirouter_debuggability); persist_router_usage
then attributes each such llm_error row to a team and emits a first-class
AGENT_REFUSED — the same event the Anthropic path emits — instead of letting it
masquerade as a generic AGENT_FAILED. This pins the attribution used for that.
"""

from gamemgr.game_mgr import _attribute_team, _model_family


def test_model_family():
    assert _model_family("claude-opus-4-8") == "claude"
    assert _model_family("gpt-5.6-sol") == "openai"
    assert _model_family("gpt-5.3-codex") == "openai"
    assert _model_family("o3-mini") == "openai"
    assert _model_family("deepseek-v4") is None
    assert _model_family(None) is None


def test_attribute_team_principal_first():
    # a distinct per-team principal token is the most reliable signal
    row = {"principal": "team2", "model": "gpt-5.6-sol", "client_ip": "10.0.0.9"}
    assert _attribute_team(row, {"team1", "team2"}, {"openai": "team2"}, {}) == ("team2", "principal")


def test_attribute_team_falls_back_to_model_family():
    # passthrough run: principal isn't a known team id → split by model family
    # (this is the codex/gpt-5.6-sol cyber-refusal path — must land on team2)
    row = {"principal": "shared-token", "model": "gpt-5.6-sol", "client_ip": "x"}
    assert _attribute_team(row, {"team1", "team2"}, {"openai": "team2", "claude": "team1"}, {}) \
        == ("team2", "model-family")


def test_attribute_team_falls_back_to_client_ip_then_unknown():
    row = {"principal": None, "model": "mystery", "client_ip": "10.0.0.5"}
    assert _attribute_team(row, {"team1", "team2"}, {}, {"10.0.0.5": "team1"}) == ("team1", "client_ip")
    assert _attribute_team({"model": "mystery"}, {"team1"}, {}, {}) == ("unknown", "none")
