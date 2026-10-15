"""provision_ssh_pivot grants the agent ROOT on its own prod box: installs the
pubkey for root (+ arena_agent), enables PermitRootLogin, reloads sshd, and
points the dev-side ssh config at root. Offline — exec_sync is faked."""

from __future__ import annotations

import agents._provisioning as prov
from agents._provisioning import ContainerHandle
from models.agent import AgentConfig


class _FakeTransport:
    def __init__(self):
        self.files: dict[tuple[str, str], bytes] = {}

    def write_file(self, container, path, data):
        self.files[(container.name, path)] = data


def _run(monkeypatch, *, arena_agent_present: bool):
    calls: list[list[str]] = []

    def fake_exec_sync(client, transport, container, cmd, check=False):
        calls.append(list(cmd))
        if cmd[:2] == ["id", "arena_agent"]:
            return (0 if arena_agent_present else 1, "")
        return (0, "")

    monkeypatch.setattr(prov, "exec_sync", fake_exec_sync)
    transport = _FakeTransport()
    cfg = AgentConfig(cli="claude -p", team_id="team1",
                      own_prod_host="team1_prod", own_prod_container_name="team1_prod")
    dev = ContainerHandle(container_id="", name="team1_dev", networks=[])
    prov.provision_ssh_pivot(client=None, transport=transport, dev_container=dev, cfg=cfg)
    return calls, transport


def _appends_to(calls, ak):
    """True if some `sh -c` call appends to authorized_keys path `ak` (idempotent
    grep||echo >>) rather than overwriting."""
    return any(c[0] == "sh" and ak in c[-1] and ">>" in c[-1] and "grep -qF" in c[-1]
               for c in calls)


def test_provisions_root_key_and_enables_root_login(monkeypatch):
    calls, transport = _run(monkeypatch, arena_agent_present=True)

    # Agent key APPENDED for root on its own prod (coexists with the facility key).
    assert _appends_to(calls, "/root/.ssh/authorized_keys")
    # ...and for arena_agent (present), so prompts naming that user still work.
    assert _appends_to(calls, "/home/arena_agent/.ssh/authorized_keys")
    # Root login flipped on, then sshd (PID 1) reloaded.
    assert any(c[0] == "sed" and "PermitRootLogin prohibit-password" in " ".join(c) for c in calls)
    assert ["sh", "-c", "kill -HUP 1 2>/dev/null || true"] in calls
    # Dev-side ssh config lands the agent as root by default.
    dev_cfg = transport.files[("team1_dev", "/root/.ssh/config")]
    assert b"User root" in dev_cfg


def test_root_grant_works_without_arena_agent(monkeypatch):
    """Legacy/external image without arena_agent: still gets root provisioned."""
    calls, transport = _run(monkeypatch, arena_agent_present=False)
    assert _appends_to(calls, "/root/.ssh/authorized_keys")
    # No arena_agent home touched.
    assert not _appends_to(calls, "/home/arena_agent/.ssh/authorized_keys")
    assert any(c[0] == "sed" and "PermitRootLogin" in " ".join(c) for c in calls)
