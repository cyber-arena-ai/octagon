"""v3: the agent's mcp_endpoint points at the mcp-gateway when `mcp_gateway.agent_url`
is set (on the agent net, no relay), else the host MCP base. Offline."""

from __future__ import annotations

from gamemgr.config import (
    ChallengeConfig,
    GameConfig,
    MCPGatewayConfig,
    RoundConfig,
    _from_dict,
)
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


class _StubOrchestrator:
    network = None


def _game(*, agent_url=""):
    teams = [Team(id="team1", dev="d1", prod="p1", token="tok1")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000,
                                    deploy_on=["team1"])],
        rounds=RoundConfig(), agents={},
        mcp_gateway=MCPGatewayConfig(agent_url=agent_url),
    )
    return GameMgr(config=cfg, orchestrator=_StubOrchestrator())


def test_config_parse_mcp_gateway():
    cfg = _from_dict({
        "teams": [{"id": "team1", "dev": "d", "prod": "p"}],
        "mcp_gateway": {"agent_url": "http://mcp-gateway:8080"},
    })
    assert cfg.mcp_gateway.agent_url == "http://mcp-gateway:8080"


def test_mcp_gateway_defaults_empty():
    cfg = _from_dict({"teams": [{"id": "team1", "dev": "d", "prod": "p"}]})
    assert cfg.mcp_gateway.agent_url == ""


def test_endpoint_points_at_gateway_when_set():
    g = _game(agent_url="http://mcp-gateway:8080")
    assert g._agent_mcp_endpoint("http://host.docker.internal:8000") == \
        "http://mcp-gateway:8080/mcp/"


def test_endpoint_falls_back_to_host_when_unset():
    g = _game(agent_url="")
    assert g._agent_mcp_endpoint("http://host.docker.internal:8000") == \
        "http://host.docker.internal:8000/mcp/"
