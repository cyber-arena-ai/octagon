"""Per-team X-Team-Token: config round-trip + REST submit auth.

Per-round rotation is now driven by GameMgr signaling the containerized
flag-service (tests/test_flag_service_wiring.py) which plants over SSH
(tests/test_flag_service.py); the former real-docker rotation/HTTP integration
here was for the removed in-process path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from gamemgr.api import build_app
from gamemgr.config import (
    ChallengeConfig,
    FlagServiceConfig,
    GameConfig,
    RoundConfig,
    load as load_game_config,
)
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph

CONFIG = Path(__file__).parent.parent / "config" / "legacy" / "game.yaml"


# ── config / dataclass round-trip ──────────────────────────────────────


def test_team_token_round_trips_through_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("API_ROUTER_BASE", "https://x")
    monkeypatch.setenv("API_ROUTER_KEY", "y")
    yaml_text = """
teams:
  - id: team1
    dev: t1d
    prod: t1p
    token: tok-1
  - id: team2
    dev: t2d
    prod: t2p
    # no token → defaults to ""
nodes: []
networks: {}
"""
    p = tmp_path / "game.yaml"
    p.write_text(yaml_text)
    cfg = load_game_config(p, env_file=None)
    by_id = {t.id: t for t in cfg.topology.teams}
    assert by_id["team1"].token == "tok-1"
    assert by_id["team2"].token == ""


def test_game_yaml_carries_real_tokens():
    cfg = load_game_config(CONFIG)
    tokens = {t.id: t.token for t in cfg.topology.teams}
    assert tokens == {"team1": "dev-team1-token", "team2": "dev-team2-token"}


# ── REST submit auth (offline; bad token rejected before any forward) ───


class _StubOrchestrator:
    network = None


def _http():
    teams = [Team(id="team1", dev="t1d", prod="t1p", token="dev-team1-token"),
             Team(id="team2", dev="t2d", prod="t2p", token="dev-team2-token")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000,
                                    deploy_on=["team1", "team2"])],
        rounds=RoundConfig(), agents={},
        flag_service=FlagServiceConfig(base_url="http://fs"),
    )
    g = GameMgr(config=cfg, orchestrator=_StubOrchestrator())
    return g, TestClient(build_app(g, asyncio.Event()))


def test_submit_wrong_token_rejected():
    g, tc = _http()
    before = sum(1 for e in g.timeline.history() if e.type == "FLAG_SUBMITTED")
    body = tc.post("/game/flag/submit", headers={"X-Team-Token": "not-real"},
                   json={"flag": "FLAG{x}"}).json()
    assert body["accepted"] is False and body["reason"] == "BAD_TOKEN"
    after = sum(1 for e in g.timeline.history() if e.type == "FLAG_SUBMITTED")
    assert after == before  # BAD_TOKEN emits no event (rejected before forward)


def test_submit_without_token_rejected():
    _, tc = _http()
    body = tc.post("/game/flag/submit", json={"flag": "FLAG{x}"}).json()
    assert body["accepted"] is False and body["reason"] == "BAD_TOKEN"
