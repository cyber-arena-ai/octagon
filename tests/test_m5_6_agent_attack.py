"""M5.6 — end-to-end live test: real Claude agent SSH-pivots through its own
prod and steals an opposing flag, submits via the FlagMgr HTTP endpoint.

Env-gated like the M4 hello-world smoke (set API_ROUTER_KEY to enable).
Costs ~10-30 cents per run depending on how many rounds the LLM takes.

Verifies the M5.6 plumbing end-to-end:
  1. ClaudeAgentManager.install() provisions SSH (key on prod, key + ssh
     config on dev, sshd running on prod)
  2. agent.env carries CYBERARENA_TEAM_TOKEN / OWN_PROD / OPPOSING_PRODS /
     SUBMIT_URL
  3. The attack-mode system prompt reaches the LLM
  4. The LLM autonomously: ssh's into own prod, scrapes the opposing
     notes service, picks out a FLAG{...}, curls the submit endpoint
  5. POST /game/flag/submit accepts the X-Team-Token, FlagStore records
     the submission, FLAG_SUBMITTED event lands on the timeline with
     accepted=true
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import docker
import pytest
import uvicorn

from agents.claude import ClaudeAgentManager
from gamemgr.api import build_app
from gamemgr.config import load as load_game_config
from gamemgr.game_mgr import GameMgr
from orchestrator.orchestrator import Orchestrator
from orchestrator.handle import ContainerHandle

CONFIG = Path(__file__).parent.parent / "config" / "legacy" / "game.yaml"
SUBMIT_PORT = 8000
WAIT_FOR_SUBMISSION_S = 240   # 4 min budget — sonnet typically lands ~60s
POLL_INTERVAL_S = 5


def _have_real_router_key() -> bool:
    key = os.environ.get("API_ROUTER_KEY", "")
    return bool(key) and "placeholder" not in key


@pytest.mark.skipif(
    not _have_real_router_key(),
    reason="set API_ROUTER_KEY in .env to run the live attack smoke",
)
def test_live_agent_attacks_and_submits():
    """Stand up arena + uvicorn + agent; assert at least one ACCEPTED."""
    client = docker.from_env()
    client.ping()

    cfg = load_game_config(CONFIG)
    orch = Orchestrator(graph=cfg.topology)
    game = GameMgr(config=cfg, orchestrator=orch)

    # Server starts; will be told to exit at end-of-test.
    shutdown_event = asyncio.Event()
    app = build_app(game, shutdown_event)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=SUBMIT_PORT, log_level="warning"))

    server_thread = threading.Thread(target=server.run, name="m5_6-uvicorn", daemon=True)

    orch.setup()
    try:
        game.deploy_challenges()
        for (team_id, _svc), challenge in game._challenge_by_owner.items():
            target = game.target_for(team_id, challenge)
            for _ in range(50):
                if challenge.functionality_test.run(target).passed:
                    break
                time.sleep(0.1)
        game.plant_initial_flags()

        server_thread.start()
        # Tiny wait for uvicorn to bind 8000 (otherwise the agent's first
        # curl may fail and burn a turn).
        for _ in range(30):
            time.sleep(0.1)
            if server.started:
                break

        # Install team2's attack agent (attacks team1). cli/model/api_key
        # come from game.yaml's agents block, runtime context (token, own/
        # opposing prods, submit URL, attack prompt) is layered on by
        # agent_config_for.
        agent_cfg = game.agent_config_for("team2")
        dev_handle = _handle(client, "team2_dev")
        mgr = ClaudeAgentManager(client=client)

        # Bridge sync install() callback → async GameMgr.record_agent_installed.
        # No WS subscribers active in this test, so a per-call asyncio.run
        # is safe; in main.py this gets replaced by run_coroutine_threadsafe
        # against the uvicorn loop.
        def _on_installed(team_id):
            asyncio.run(game.record_agent_installed(team_id))

        mgr.install(dev_handle, agent_cfg, on_installed=_on_installed)

        # Poll for a successful submission. The agent is autonomous — once
        # install() returned the supervisor-bash kicked claude off, and the
        # test just watches the FlagStore for evidence of a successful loop.
        deadline = time.monotonic() + WAIT_FOR_SUBMISSION_S
        success = False
        last_log_at = 0.0
        while time.monotonic() < deadline:
            submissions = game.flags.submissions.get("team2", set())
            accepted_events = [
                e for e in game.timeline.history()
                if e.type == "FLAG_SUBMITTED" and e.payload.get("accepted")
            ]
            if accepted_events:
                success = True
                break
            if time.monotonic() - last_log_at > 30:
                print(
                    f"  ...waiting ({int(time.monotonic() - (deadline - WAIT_FOR_SUBMISSION_S))}s) "
                    f"submissions={len(submissions)} flag_events={len([e for e in game.timeline.history() if e.type == 'FLAG_SUBMITTED'])}"
                )
                last_log_at = time.monotonic()
            time.sleep(POLL_INTERVAL_S)

        # Collect diagnostics whether or not it succeeded.
        logs = mgr.fetch_logs(dev_handle)
        all_flag_events = [e for e in game.timeline.history() if e.type == "FLAG_SUBMITTED"]
        print("\n── flag events on timeline:")
        for e in all_flag_events:
            print(f"   {e.payload}")
        print("── agent stderr tail (last 2k):")
        print(logs.stderr[-2000:])
        print("── agent stdout tail (last 2k):")
        print(logs.stdout[-2000:])

        try:
            mgr.kill(dev_handle)
        except Exception:
            pass

        assert success, (
            f"no ACCEPTED submission within {WAIT_FOR_SUBMISSION_S}s; "
            f"submissions['team2']={game.flags.submissions.get('team2', set())}; "
            f"flag events: {[e.payload for e in all_flag_events]}"
        )
        # Sanity: at least one event payload references the right victim/service.
        winning = [
            e for e in all_flag_events
            if e.payload.get("accepted")
            and e.payload.get("submitter_team_id") == "team2"
            and e.payload.get("victim_team_id") == "team1"
            and e.payload.get("service") == "notes"
        ]
        assert winning, f"no event matched team2-stole-team1-notes shape: {all_flag_events}"

        # Lifecycle: AGENT_INSTALLED is the only deterministic host-emitted
        # event under v1 (AGENT_LAUNCHED retired with the supervisor;
        # AGENT_READY is LLM-dependent and intentionally not asserted).
        installed = [
            e for e in game.timeline.history()
            if e.type == "AGENT_INSTALLED" and e.team_id == "team2"
        ]
        assert installed, (
            f"AGENT_INSTALLED missing for team2; "
            f"got: {[e.type for e in game.timeline.history() if e.team_id == 'team2']}"
        )
    finally:
        try:
            server.should_exit = True
            server_thread.join(timeout=5)
        except Exception:
            pass
        game.teardown_challenges()
        orch.teardown()


def _handle(client, name: str) -> ContainerHandle:
    c = client.containers.get(name)
    networks = list(c.attrs["NetworkSettings"]["Networks"].keys())
    return ContainerHandle(container_id=c.id, name=name, networks=networks)
