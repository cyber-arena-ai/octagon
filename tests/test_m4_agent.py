"""M4 integration test — agent install / status / kill.

Uses `cli: sleep 60` as a stand-in for the real Claude CLI:
  * no API key required, no token spend
  * still exercises the supervisor-bash launch, pid/wrapper-pid recording,
    `kill -- -<pgid>` killpg path, and exit_code observation

Also covers the env-var expansion in gamemgr.config (positive, missing-var
fail-fast, cycle detection) as fast unit tests that don't need docker.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import docker
import pytest

from agents.claude import (
    EXIT_CODE_PATH,
    PID_PATH,
    WRAPPER_PID_PATH,
    ClaudeAgentManager,
)
from agents.registry import build as build_agent_manager
from gamemgr.config import _expand_env_vars, load as load_game_config
from models.agent import AgentConfig, AgentStatus
from orchestrator.orchestrator import Orchestrator
from orchestrator.handle import ContainerHandle


# ── unit tests: ${VAR} expansion ───────────────────────────────────────


def test_expand_env_vars_three_branches(monkeypatch):
    """Three behaviors in one test:
      * `${FOO}` → substituted (recurses into list values)
      * missing var → ValueError naming the var
      * cyclic reference → ValueError mentioning 'cyclic'
    """
    monkeypatch.setenv("FOO", "bar")
    out = _expand_env_vars({"k": "prefix-${FOO}-suffix", "lst": ["${FOO}"]})
    assert out == {"k": "prefix-bar-suffix", "lst": ["bar"]}

    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    with pytest.raises(ValueError, match="DOES_NOT_EXIST"):
        _expand_env_vars({"k": "${DOES_NOT_EXIST}"})

    monkeypatch.setenv("A", "${B}")
    monkeypatch.setenv("B", "${A}")
    with pytest.raises(ValueError, match="cyclic"):
        _expand_env_vars({"k": "${A}"})


def test_load_game_config_parses_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY_TEAM1", "sk-fake-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY_TEAM2", "sk-fake-2")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.test")

    # Two teams: sync mode (the default) requires an opponent; this test only
    # cares about team1's agent block, so team2 is a bare agent-less entry.
    yaml_text = """
teams:
  - id: team1
    dev: team1_dev
    prod: team1_prod
    agent:
      cli: claude --foo
      model: claude-sonnet-4-6
      api_key: ${ANTHROPIC_API_KEY_TEAM1}
      base_url: ${ANTHROPIC_BASE_URL}
  - id: team2
    dev: team2_dev
    prod: team2_prod
nodes: []
networks: {}
"""
    p = tmp_path / "game.yaml"
    p.write_text(yaml_text)
    cfg = load_game_config(p, env_file=None)

    assert "team1" in cfg.agents
    assert cfg.agents["team1"].cli == "claude --foo"
    assert cfg.agents["team1"].api_key == "sk-fake-1"
    assert cfg.agents["team1"].base_url == "https://example.test"


def _write_agent_yaml(tmp_path, agent_block: str) -> Path:
    p = tmp_path / "game.yaml"
    # team2 is a bare agent-less entry so sync mode (the default) has the two
    # teams it requires; these tests only exercise team1's agent block.
    p.write_text(
        "teams:\n"
        "  - id: team1\n"
        "    dev: team1_dev\n"
        "    prod: team1_prod\n"
        "    agent:\n"
        f"{agent_block}"
        "  - id: team2\n"
        "    dev: team2_dev\n"
        "    prod: team2_prod\n"
        "nodes: []\n"
        "networks: {}\n"
    )
    return p


def test_unknown_agent_key_is_rejected(tmp_path):
    # A misspelled/unknown key would otherwise be silently dropped by
    # ag.get(); config load must fail loud so the typo surfaces.
    p = _write_agent_yaml(
        tmp_path,
        "      cli: claude\n"
        "      append_sytem_prompt: oops\n",  # typo'd append_system_prompt
    )
    with pytest.raises(ValueError, match="unknown agent key"):
        load_game_config(p, env_file=None)


def test_extra_cli_flags_warn(tmp_path, caplog):
    # Flags past the leading token are ignored by the adapter; load warns
    # so the edit isn't a silent no-op.
    p = _write_agent_yaml(tmp_path, "      cli: claude -p --output-format text\n")
    with caplog.at_level("WARNING"):
        load_game_config(p, env_file=None)
    assert any("have NO effect" in r.message for r in caplog.records)


def test_canonical_cli_does_not_warn(tmp_path, caplog):
    p = _write_agent_yaml(tmp_path, "      cli: claude\n")
    with caplog.at_level("WARNING"):
        load_game_config(p, env_file=None)
    assert not any("have NO effect" in r.message for r in caplog.records)


def test_unknown_leading_token_does_not_warn(tmp_path, caplog):
    # `cli: sleep 60` is a supported test stand-in (registry falls back to
    # ClaudeAgentManager). Its tokens aren't "ignored claude flags", so it
    # must NOT trigger the ignored-flags warning.
    p = _write_agent_yaml(tmp_path, "      cli: sleep 60\n")
    with caplog.at_level("WARNING"):
        load_game_config(p, env_file=None)
    assert not any("have NO effect" in r.message for r in caplog.records)


# ── integration: install / status / kill on a real container ───────────


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
    """Bring up the full topology so we get a real team1_dev container."""
    cfg = load_game_config(Path(__file__).parent.parent / "config" / "legacy" / "game.yaml")
    orch = Orchestrator(graph=cfg.topology)
    orch.setup()
    yield orch
    orch.teardown()


@pytest.fixture
def dev_handle(client, arena):
    name = "team1_dev"
    c = client.containers.get(name)
    networks = list(arena.graph.node(name).networks)
    return ContainerHandle(container_id=c.id, name=name, networks=networks)


def _read_int(client, container_name: str, path: str) -> int | None:
    r = client.containers.get(container_name).exec_run(["sh", "-c", f"cat {path} 2>/dev/null || true"])
    out = (r.output or b"").decode(errors="replace").strip()
    if not out:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def test_registry_dispatches_sleep_to_claude_manager():
    mgr = build_agent_manager(AgentConfig(cli="sleep 60"))
    assert isinstance(mgr, ClaudeAgentManager)


@pytest.mark.skip(
    reason="M4 supervisor lifecycle replaced by persistent-memory v1 model "
    "(install() no longer launches a supervisor; lifecycle is now covered "
    "by run_phase per phase — see tests/test_dcf_run_phase.py)"
)
def test_install_status_kill_lifecycle(client, dev_handle):
    pass


def test_install_is_idempotent(client, dev_handle):
    """Re-installing must not raise. In v1 `install()` is one-shot setup
    (env, mcp.json, SSH pivot, workdir, npm install) — no supervisor is
    launched, so the M4-era 'fresh pid each install' assertion is gone.
    What we still check: install twice succeeds without error, and the
    written agent.env / workdir / etc. are present after both calls.
    """
    from agents.claude import ENV_PATH, WORKDIR

    mgr = ClaudeAgentManager(client=client)
    cfg = AgentConfig(cli="sleep 60")

    mgr.install(dev_handle, cfg)
    # workdir + env file exist after first install
    rc1, _ = client.containers.get(dev_handle.name).exec_run(["test", "-d", WORKDIR])
    rc2, _ = client.containers.get(dev_handle.name).exec_run(["test", "-f", ENV_PATH])
    assert rc1 == 0 and rc2 == 0

    mgr.install(dev_handle, cfg)
    # still present after second install — idempotent setup
    rc1, _ = client.containers.get(dev_handle.name).exec_run(["test", "-d", WORKDIR])
    rc2, _ = client.containers.get(dev_handle.name).exec_run(["test", "-f", ENV_PATH])
    assert rc1 == 0 and rc2 == 0


# ── live smoke test against the real router ────────────────────────────


HELLO_PROMPT = (
    "Create the file /tmp/hello.py containing exactly:\n"
    "    print('hello from claude')\n"
    "Then run it: `python3 /tmp/hello.py`\n"
    "Report only the program's output. Do nothing else."
)

HELLO_OUTPUT = "hello from claude"


def _have_real_router_key() -> bool:
    key = os.environ.get("API_ROUTER_KEY", "")
    return bool(key) and "placeholder" not in key


@pytest.mark.skipif(
    not _have_real_router_key(),
    reason="set API_ROUTER_KEY (and API_ROUTER_BASE) in .env to run the live smoke",
)
def test_live_hello_world(client, dev_handle):
    """Real-API smoke: Claude creates /tmp/hello.py, runs it, exits cleanly.

    Skipped unless API_ROUTER_KEY is set to a non-placeholder value (i.e. you
    have routed your .env at a real Anthropic-compatible endpoint). Costs a
    few cents per run.
    """
    mgr = ClaudeAgentManager(client=client)
    cfg = AgentConfig(
        cli=(
            "claude -p --dangerously-skip-permissions "
            "--output-format stream-json --verbose"
        ),
        model="claude-sonnet-4-6",
        api_key=os.environ["API_ROUTER_KEY"],
        base_url=os.environ.get("API_ROUTER_BASE", "https://api.anthropic.com"),
        system_prompt=HELLO_PROMPT,
    )

    try:
        mgr.install(dev_handle, cfg)

        # Poll up to 180s for claude to finish (npm install is baked into the
        # image, so we're only waiting on actual LLM turns + tool calls).
        deadline = time.monotonic() + 180
        final_state = None
        while time.monotonic() < deadline:
            s = mgr.status(dev_handle)
            if s.status in (AgentStatus.COMPLETED, AgentStatus.CRASHED, AgentStatus.TERMINATED):
                final_state = s
                break
            time.sleep(2)

        logs = mgr.fetch_logs(dev_handle)
        cat = client.containers.get(dev_handle.name).exec_run(
            ["sh", "-c", "cat /tmp/hello.py 2>/dev/null || true"]
        )
        hello_content = cat.output.decode(errors="replace")

        # Surface useful info in pytest output even on success.
        print("\n── claude exit:", final_state)
        print("── /tmp/hello.py contents:", repr(hello_content))
        print("── stderr tail (last 500 chars):", logs.stderr[-500:])

        assert final_state is not None, "claude did not exit within 180s"
        assert final_state.status == AgentStatus.COMPLETED, \
            f"expected COMPLETED, got {final_state}; stderr tail: {logs.stderr[-500:]!r}"
        assert "hello from claude" in hello_content, \
            f"/tmp/hello.py missing or wrong content: {hello_content!r}"
        # The stream-json transcript should include a tool_result with the
        # program's stdout. Search for the line we expect.
        assert HELLO_OUTPUT in logs.stdout, \
            f"agent stdout never mentions {HELLO_OUTPUT!r}; got: {logs.stdout[-1000:]!r}"
    finally:
        try:
            mgr.kill(dev_handle)
        except Exception:
            pass
        # Cleanup /tmp/hello.py so re-runs start fresh
        try:
            client.containers.get(dev_handle.name).exec_run(["rm", "-f", "/tmp/hello.py"])
        except Exception:
            pass
