"""Secret-safe dsh dev-setup readiness classification."""

from __future__ import annotations

import pytest

from gamemgr.config import GameConfig
from gamemgr.events import EventType
from gamemgr.game_mgr import GameMgr
from models.topology import Node, Team, TopologyGraph
from orchestrator.orchestrator import (
    DevSetupFailure,
    Orchestrator,
    classify_dev_setup_failure,
)


def _graph(*, proxy: bool = True) -> TopologyGraph:
    env = {"HTTPS_PROXY": "http://proxy-user:secret@api-router:3128"} if proxy else {}
    return TopologyGraph(
        teams=[Team(id="team1", dev="team1_dev", prod="team1_prod")],
        nodes=[
            Node(id="team1_dev", image="agent", networks=[], env=env),
            Node(id="team1_prod", image="challenge", networks=[]),
        ],
        networks=[],
    )


def _orchestrator(containers) -> Orchestrator:
    return Orchestrator(
        graph=_graph(),
        containers=containers,
        network=object(),
        compose=object(),
    )


@pytest.mark.parametrize(
    ("output", "proxy_configured", "exception_type", "expected"),
    [
        ("403 Forbidden [IP: 10.1.0.4 3128]", True, None,
         "configured_proxy_http_403"),
        ("403 Forbidden", False, None, "command_error"),
        ("407 Proxy Authentication Required", True, None, "auth_error"),
        ("HTTP 401 Unauthorized", True, None, "auth_error"),
        ("operation timed out", True, None, "timeout"),
        ("", True, "TimeoutError", "timeout"),
        ("", True, "RuntimeError", "exception"),
    ],
)
def test_dev_setup_failure_classification_is_closed_and_proxy_aware(
    output, proxy_configured, exception_type, expected
):
    assert classify_dev_setup_failure(
        output,
        proxy_configured=proxy_configured,
        exception_type=exception_type,
    ) == expected


def test_failed_setup_carries_no_raw_output_or_proxy_credentials():
    secret = "proxy-password-must-not-leak"

    class Containers:
        def exec(self, _dev_id, _command):
            return 100, (
                "E: Failed to fetch http://archive.ubuntu.com 403 Forbidden "
                f"Proxy-Authorization: Basic {secret}"
            )

    graph = _graph()
    # Put the secret in the configured URL too; neither source may reach the
    # structured result or exception string.
    dev = graph.nodes[0]
    object.__setattr__(
        dev,
        "env",
        {"HTTPS_PROXY": f"http://user:{secret}@api-router:3128"},
    )
    orch = Orchestrator(
        graph=graph,
        containers=Containers(),
        network=object(),
        compose=object(),
    )

    with pytest.raises(DevSetupFailure) as caught:
        orch._run_setup_script("team1_dev")

    result = caught.value.result
    assert result.status == "configured_proxy_http_403"
    assert result.proxy_configured is True
    assert result.rc == 100
    assert secret not in str(caught.value)
    assert secret not in str(result.as_payload())


def test_successful_setup_records_safe_timing():
    class Containers:
        def exec(self, _dev_id, _command):
            return 0, "all good"

    result = _orchestrator(Containers())._run_setup_script("team1_dev")
    assert result.status == "ok"
    assert result.proxy_configured is True
    assert result.rc == 0
    assert result.duration_secs >= 0


def test_setup_complete_persists_only_structured_dev_setup_results():
    orch = _orchestrator(object())
    orch.dev_setup_results = {
        "team1_dev": {
            "status": "configured_proxy_http_403",
            "duration_secs": 0.123,
            "proxy_configured": True,
            "rc": 100,
            "exception_type": None,
        }
    }
    game = GameMgr(
        config=GameConfig(topology=_graph(), challenges=[]),
        orchestrator=orch,
    )
    game._setup_started_ts = 100.0
    game.record_setup_complete()
    event = next(
        item for item in game.timeline.history()
        if item.type == EventType.SETUP_COMPLETE.value
    )
    assert event.payload["duration_secs"] is not None
    assert event.payload["dev_setup"] == orch.dev_setup_results
    assert "output" not in str(event.payload).lower()
