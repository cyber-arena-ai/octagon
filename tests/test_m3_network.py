"""M3 integration test: cross-team network switching.

Asserts:
  - default (post-setup) state: prod containers reach each other via game_net
  - set_cross_team_network(False) blocks cross-team prod <-> prod
  - set_cross_team_network(True) restores it
  - idempotent in both directions
  - cross-team dev <-> anything is blocked in every phase (structural)
"""

from __future__ import annotations

import time
from pathlib import Path

import docker
import pytest

from gamemgr.config import load as load_game_config
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
def arena(client):
    cfg = load_game_config(CONFIG)
    orch = Orchestrator(graph=cfg.topology)
    game = GameMgr(config=cfg, orchestrator=orch)
    orch.setup()
    game.deploy_challenges()

    # wait until notes is alive on every prod container
    for (team_id, _svc), challenge in game._challenge_by_owner.items():
        target = game.target_for(team_id, challenge)
        for _ in range(50):
            if challenge.functionality_test.run(target).passed:
                break
            time.sleep(0.1)

    yield orch, game

    # leave game_net connected so teardown sees consistent state
    orch.network.set_cross_team_network(True)
    game.teardown_challenges()
    orch.teardown()


def _can_connect(client, source: str, target: str, port: int, timeout: float = 2.0) -> bool:
    """True iff `source` container can TCP-connect to `target:port`."""
    probe = (
        f"import socket,sys\n"
        f"s = socket.socket()\n"
        f"s.settimeout({timeout})\n"
        f"try:\n"
        f"    s.connect(('{target}', {port}))\n"
        f"    print('OK')\n"
        f"except Exception as e:\n"
        f"    print('FAIL', type(e).__name__)\n"
    )
    result = client.containers.get(source).exec_run(["python3", "-c", probe])
    out = (result.output or b"").decode(errors="replace").strip()
    return out.startswith("OK")


def _wait_reachable(client, source: str, target: str, port: int, tries: int = 30) -> bool:
    for _ in range(tries):
        if _can_connect(client, source, target, port):
            return True
        time.sleep(0.2)
    return False


def test_default_attached_prod_reachable(client, arena):
    orch, _ = arena
    orch.network.set_cross_team_network(True)  # ensure ATTACK state
    assert _wait_reachable(client, "team1_prod", "team2_prod", 5000)
    assert _wait_reachable(client, "team2_prod", "team1_prod", 5000)


def test_disconnect_blocks_cross_team_prod(client, arena):
    orch, _ = arena
    orch.network.set_cross_team_network(False)
    assert not _can_connect(client, "team1_prod", "team2_prod", 5000)
    assert not _can_connect(client, "team2_prod", "team1_prod", 5000)


def test_reconnect_restores_cross_team_prod(client, arena):
    orch, _ = arena
    orch.network.set_cross_team_network(False)
    orch.network.set_cross_team_network(True)
    assert _wait_reachable(client, "team1_prod", "team2_prod", 5000)


def test_idempotent_toggles(client, arena):
    """Calling set_cross_team_network with the same value twice must
    not raise, and the network state must be what the last call asked for."""
    orch, _ = arena
    orch.network.set_cross_team_network(False)
    orch.network.set_cross_team_network(False)
    assert not _can_connect(client, "team1_prod", "team2_prod", 5000)
    orch.network.set_cross_team_network(True)
    orch.network.set_cross_team_network(True)
    assert _wait_reachable(client, "team1_prod", "team2_prod", 5000)


def test_cross_team_dev_blocked_in_every_phase(client, arena):
    """dev must never reach the opponent's prod — same in ATTACK and DEFENSE."""
    orch, _ = arena
    for allow in (True, False):
        orch.network.set_cross_team_network(allow)
        assert not _can_connect(client, "team1_dev", "team2_prod", 5000), \
            f"team1_dev reached team2_prod with allow={allow}"
        assert not _can_connect(client, "team2_dev", "team1_prod", 5000), \
            f"team2_dev reached team1_prod with allow={allow}"


def test_intra_team_dev_to_prod_unaffected_by_phase(client, arena):
    """dev <-> own team prod stays reachable in every phase."""
    orch, _ = arena
    orch.network.set_cross_team_network(False)
    assert _wait_reachable(client, "team1_dev", "team1_prod", 5000)
    orch.network.set_cross_team_network(True)
    assert _wait_reachable(client, "team1_dev", "team1_prod", 5000)