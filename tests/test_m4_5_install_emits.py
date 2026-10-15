"""M4.5 — install() callback wiring.

In the persistent-memory v1 model `install()` is one-shot (no
supervisor launches). The single `on_installed` callback fires at the
end of install(). The M4.5-era `on_launched` was retired alongside the
supervisor (there is no separate launch step to bracket).

What we pin:
  ①on_installed fires on the happy path.
  ②if install raises before the callback site, it does NOT fire.
  ③empty team_id → callback skipped (defensive for non-multi-tenant tests).

No docker, no real CLI — ClaudeAgentManager runs against a mock
docker client satisfying the small subset of exec_run calls install()
makes.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from agents.claude import (
    ClaudeAgentManager,
    EXIT_CODE_PATH,
    PID_PATH,
    WRAPPER_PID_PATH,
)
from models.agent import AgentConfig
from orchestrator.handle import ContainerHandle


def _make_mock_client(pid: int = 12345, wrapper_pid: int = 12344) -> MagicMock:
    """Build a docker client mock that fakes the small subset of behavior
    ClaudeAgentManager touches during install: exec_run for shell commands
    and pid/wrapper-pid file reads."""
    client = MagicMock()

    # State the supervisor would have written.
    files: dict[str, bytes] = {
        PID_PATH: str(pid).encode(),
        WRAPPER_PID_PATH: str(wrapper_pid).encode(),
    }

    def exec_run(cmd, **kwargs):
        result = MagicMock()
        result.exit_code = 0
        # Reads come through `sh -c "cat <path> 2>/dev/null || true"`
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "sh" and cmd[1] == "-c":
            script = cmd[2]
            for path, blob in files.items():
                if path in script and "cat" in script:
                    result.output = blob
                    return result
            # Clear-stale-state sh: matches PID_PATH but isn't a read.
            result.output = b""
            return result
        result.output = b""
        return result

    container = MagicMock()
    container.exec_run = exec_run
    container.id = "mocked-container-id"
    client.containers.get.return_value = container
    return client


def _handle(name: str = "team1_dev") -> ContainerHandle:
    return ContainerHandle(container_id="mocked-id", name=name, networks=["team1_net"])


def test_install_emits_installed_on_success():
    """Happy path: on_installed fires exactly once at the end of install()."""
    client = _make_mock_client(pid=4321, wrapper_pid=4320)
    mgr = ClaudeAgentManager(client=client)
    cfg = AgentConfig(cli="sleep 60", team_id="team1")

    fired: list[str] = []

    def on_installed(team_id):
        fired.append(team_id)

    mgr.install(_handle(), cfg, on_installed=on_installed)

    assert fired == ["team1"], f"expected one INSTALLED callback for team1; got: {fired}"


def test_install_fault_injection_skips_callback():
    """If something inside install raises before reaching the callback
    site, on_installed does NOT fire — callers can safely treat a raised
    install as "agent never came up"."""
    client = _make_mock_client()
    mgr = ClaudeAgentManager(client=client)
    cfg = AgentConfig(cli="sleep 60", team_id="team2")

    # Inject failure into _build_env_file — runs before the callback site.
    def boom(_cfg):
        raise RuntimeError("simulated install failure")

    mgr._build_env_file = boom  # type: ignore[assignment]

    fired: list[str] = []
    with pytest.raises(RuntimeError, match="simulated install failure"):
        mgr.install(
            _handle("team2_dev"),
            cfg,
            on_installed=lambda t: fired.append(t),
        )

    assert fired == [], (
        "install raised before reaching the callback site; "
        f"on_installed should not have fired. got: {fired}"
    )


def test_install_skips_callback_when_team_id_blank():
    """Defensive: empty team_id (legacy AgentConfig from M4 tests) means
    the agent isn't multi-tenant — no event lands on the bus. on_installed
    must NOT fire with empty team_id, or downstream code that keys on
    team_id will choke."""
    client = _make_mock_client()
    mgr = ClaudeAgentManager(client=client)
    cfg = AgentConfig(cli="sleep 60")  # team_id="" by default

    fired: list[str] = []
    mgr.install(_handle(), cfg, on_installed=lambda t: fired.append(t))
    assert fired == [], f"on_installed must be skipped when team_id=''; got: {fired}"
