"""M2 integration test: deploy challenge, run round loop, assert events."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import docker
import pytest

from gamemgr.config import GameConfig, RoundConfig, load as load_game_config
from gamemgr.game_mgr import GameMgr
from orchestrator.orchestrator import Orchestrator

CONFIG = Path(__file__).parent.parent / "config" / "legacy" / "game.yaml"


@pytest.fixture(scope="module")
def client():
    try:
        c = docker.from_env()
        c.ping()
    except Exception as e:
        pytest.skip(f"docker daemon unavailable: {e}")
    return c


@pytest.fixture(scope="module")
def cfg():
    return load_game_config(CONFIG)


def test_config_loads_challenges_and_rounds(cfg):
    assert len(cfg.challenges) == 1
    notes = cfg.challenges[0]
    assert notes.name == "notes"
    assert notes.type == "notes"
    assert notes.port == 5000
    assert set(notes.deploy_on) == {"team1", "team2"}
    assert cfg.rounds.total_rounds == 3


def test_full_lifecycle(client, cfg, tmp_path):
    fast_rounds = RoundConfig(defense_secs=1, attack_secs=1, total_rounds=2)
    fast_cfg = GameConfig(topology=cfg.topology, challenges=cfg.challenges, rounds=fast_rounds)

    orch = Orchestrator(graph=fast_cfg.topology)
    game = GameMgr(config=fast_cfg, orchestrator=orch)

    try:
        orch.setup()
        game.deploy_challenges()

        # notes should respond on both prod containers (poll up to ~5s)
        for (team_id, svc_name), challenge in game._challenge_by_owner.items():
            target = game.target_for(team_id, challenge)
            alive = False
            for _ in range(50):
                if challenge.functionality_test.run(target).passed:
                    alive = True
                    break
                time.sleep(0.1)
            assert alive, f"{svc_name} not alive on {target.host}"

        run_dir = tmp_path / "run01"
        asyncio.run(game.start(run_dir=run_dir))

        types = [e.type for e in game.timeline.history()]
        assert "SERVICE_DEPLOYED" in types
        assert "ROUND_START" in types
        assert "PHASE_CHANGE" in types
        assert "ROUND_STOP" in types
        # 2 rounds * (ROUND_START + 2 PHASE_CHANGE + ROUND_STOP) + 2 SERVICE_DEPLOYED = 10 events
        assert len(game.timeline.history()) >= 10

        events_file = run_dir / "events.jsonl"
        assert events_file.exists()
        lines = events_file.read_text().strip().splitlines()
        assert len(lines) >= 10
        for ln in lines:
            ev = json.loads(ln)
            assert "type" in ev and "ts" in ev and "round_id" in ev

    finally:
        game.teardown_challenges()
        orch.teardown()