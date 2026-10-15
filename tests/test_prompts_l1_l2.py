"""Async prompt assembly (L1 shared + per-challenge body).

Pins build_async_prompt = L1 + the challenge's build_advanced_prompt, with tool
names rendered per provider, slots filled, and the hint on/off toggle. Also the
GameMgr plumbing (round_config + challenge `level` -> hint). Offline, no docker.
"""

from __future__ import annotations

import pytest

from agents.prompts import build_async_prompt
from agents.prompts._helpers import mcp_tool, render_tools
from gamemgr.config import ChallengeConfig, GameConfig, RoundConfig
from gamemgr.game_mgr import GameMgr
from models.agent import AgentConfig
from models.topology import Team, TopologyGraph

# A hinted challenge (has source + a Hint line) and the attack-only one (no hint).
HINTED = "faust-treasury"
NOHINT = "notes"

_SLOTS = ["{opposing_host}", "{service_name}", "{team_id}", "{total_rounds}",
          "{round_min}", "{own_prod_host}", "{source_dir}"]


def _cfg(provider: str = "") -> AgentConfig:
    return AgentConfig(cli="x", provider=provider, model="m", api_key="k", base_url="u",
                       team_id="team1", team_token="tok", own_prod_host="team1_prod",
                       opposing_prod_hosts=("team2_prod",))


def _p(service=HINTED, provider="", hint=True):
    return build_async_prompt(_cfg(provider), service_name=service,
                              total_rounds=5, round_min=5, hint=hint)


def test_assembles_l1_plus_l2():
    p = _p()
    assert "THE GAME" in p and "HOW TO PLAY IT WELL" in p and "YOUR TOOLS" in p   # L1
    assert "SERVICE" in p and "CONNECTION FACTS" in p                              # L2
    assert p.index("THE GAME") < p.index("SERVICE")                                # L1 before L2


def test_slots_filled_and_config_values_present():
    p = build_async_prompt(_cfg(), service_name=HINTED, total_rounds=7, round_min=9, hint=True)
    for s in _SLOTS:
        assert s not in p, f"leftover slot {s}"
    assert "team1" in p                       # {team_id}
    assert "7 rounds of 9 minutes" in p       # {total_rounds}/{round_min}
    assert "team1_prod" in p                   # {own_prod_host}
    assert "team2_prod" in p                    # {opposing_host}


@pytest.mark.parametrize("provider,expected", [
    ("", "mcp__cyberarena__submit_flag"),          # default == claude
    ("claude", "mcp__cyberarena__submit_flag"),
    ("codex", "submit_flag"),
    ("opencode", "cyberarena_submit_flag"),
])
def test_provider_tool_naming(provider, expected):
    p = _p(provider=provider)
    assert f"`{expected}`" in p
    if provider in ("claude", ""):
        # every tool rendered — no bare canonical name survives for claude
        assert "`submit_flag`" not in p
        assert "`get_game_state`" not in p


def test_hint_toggle():
    easy = _p(hint=True)
    hard = _p(hint=False)
    assert "Hint:" in easy and "There is no hint" not in easy
    assert "There is no hint" in hard and "Hint:" not in hard


def test_attack_only_challenge_is_always_no_hint():
    # notes ships no hint; hint=True must still yield the no-hint sentence.
    assert "There is no hint" in _p(service=NOHINT, hint=True)


def test_unknown_challenge_raises():
    with pytest.raises(KeyError):
        build_async_prompt(_cfg(), service_name="does-not-exist",
                           total_rounds=5, round_min=5)


def test_render_tools_is_provider_correct():
    text = "call `submit_flag` then `get_game_state`"
    assert render_tools(text, _cfg("codex")) == text                        # bare: no-op
    assert "mcp__cyberarena__submit_flag" in render_tools(text, _cfg("claude"))
    assert "cyberarena_get_game_state" in render_tools(text, _cfg("opencode"))


# ── GameMgr plumbing: round_config + challenge level -> hint ────────────────

def _make_game(*, level="easy", service=HINTED, total_rounds=5, round_secs=300):
    teams = [Team(id="team1", dev="team1_dev", prod="team1_prod", token="t1"),
             Team(id="team2", dev="team2_dev", prod="team2_prod", token="t2")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name=service, type=service, port=6789,
                                    deploy_on=["team1", "team2"], level=level)],
        rounds=RoundConfig(mode="async", round_secs=round_secs, total_rounds=total_rounds),
        agents={tid: _cfg() for tid in ("team1", "team2")},
    )

    class _Stub:
        network = None
    return GameMgr(config=cfg, orchestrator=_Stub())


def test_gamemgr_async_config_assembles_and_plumbs_rounds():
    game = _make_game(total_rounds=5, round_secs=300)
    p = game.async_agent_config_for("team1").system_prompt
    assert "THE GAME" in p and "SERVICE" in p          # L1 + L2 via the GameMgr path
    assert "5 rounds of 5 minutes" in p                # round_secs 300 -> 5 min
    assert "Hint:" in p                                 # easy -> hint on


def test_gamemgr_level_hard_drops_hint():
    game = _make_game(level="hard")
    p = game.async_agent_config_for("team1").system_prompt
    assert "There is no hint" in p and "Hint:" not in p


# ── security_scanner: the flag ALONE describes the scanner in L1 (no per-config blurb) ──


def _l1_for(provider: str, scanner: bool) -> str:
    from agents.prompts._l1 import build_l1
    cfg = AgentConfig(cli="x", provider=provider, model="m", api_key="k", base_url="u",
                      team_id="team1", own_prod_host="team1_prod", security_scanner=scanner)
    return build_l1(cfg, total_rounds=5, round_min=5)


def test_l1_combines_capability_scoring_with_current_source_guidance():
    l1 = _l1_for("codex", scanner=False)

    assert "time to first ACCEPTED result" in l1
    assert "call `claim_capability`" in l1
    assert "Use the source location named by the prompt for THIS run" in l1
    assert "Do not assume a local\n  `/srv/challenge` source path exists" in l1
    assert "invoke its\n  scoring tool promptly" in l1


def test_scanner_tools_absent_by_default():
    l1 = _l1_for("codex", scanner=False)
    assert "trigger_security_scan" not in l1
    # the roster is otherwise intact
    assert "submit_flag" in l1 and "YOUR TOOLS" in l1


def test_scanner_flag_alone_injects_the_roster_line():
    """The point of gating in build_l1: `security_scanner: true` is enough on its own to
    both wire AND describe the tool, so an operator does not hand-copy a tool blurb into
    every config's append_system_prompt."""
    l1 = _l1_for("codex", scanner=True)
    assert "trigger_security_scan" in l1 and "get_security_scan" in l1
    # codex addresses MCP tools by bare name — no mcp__ prefix leaked in
    assert "mcp__" not in l1.split("trigger_security_scan")[0].rsplit("\n", 1)[-1]


def test_scanner_line_gated_to_codex_not_claude():
    """A claude team's adapter never emits the scanner block, so the briefing must not
    mention tools it does not have."""
    assert "trigger_security_scan" not in _l1_for("claude", scanner=True)
    assert "trigger_security_scan" not in _l1_for("opencode", scanner=True)
