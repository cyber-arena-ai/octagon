"""provision_facility_ssh: one facility ed25519 key authorized for root on EVERY
prod (APPENDED, to coexist with the per-team agent pivot which overwrites), with
the privkey dropped into each facility container. Offline — exec_sync is faked,
ssh-keygen runs for real into a tempdir."""

from __future__ import annotations

import agents._provisioning as prov
from agents._provisioning import ContainerHandle


class _FakeTransport:
    def __init__(self):
        self.files: dict[tuple[str, str], bytes] = {}

    def write_file(self, container, path, data):
        self.files[(container.name, path)] = data


def _run(monkeypatch, **kw):
    calls: list[tuple[str, list[str]]] = []

    def fake_exec_sync(client, transport, container, cmd, check=False):
        calls.append((container.name, list(cmd)))
        return (0, "")

    monkeypatch.setattr(prov, "exec_sync", fake_exec_sync)
    transport = _FakeTransport()
    prods = [ContainerHandle(container_id="", name="team1_prod", networks=[]),
             ContainerHandle(container_id="", name="team2_prod", networks=[])]
    facs = [ContainerHandle(container_id="", name="flag_service", networks=[]),
            ContainerHandle(container_id="", name="health_poller", networks=[])]
    pubkey = prov.provision_facility_ssh(
        client=None, transport=transport, prod_containers=prods,
        facility_containers=facs, **kw,
    )
    return calls, transport, pubkey


def test_authorizes_key_on_every_prod_by_append(monkeypatch):
    calls, transport, pubkey = _run(monkeypatch)
    assert pubkey.startswith("ssh-ed25519")

    for prod in ("team1_prod", "team2_prod"):
        append = [c for name, c in calls
                  if name == prod and c[0] == "sh" and "authorized_keys" in c[-1]]
        assert append, f"no authorized_keys append on {prod}"
        cmd = append[0][-1]
        # idempotent append, NOT overwrite (preserves the agent pivot key)
        assert ">>" in cmd and "grep -qF" in cmd
        # root login enabled + sshd reloaded on each prod
        assert any(name == prod and c[0] == "sed"
                   and "PermitRootLogin prohibit-password" in " ".join(c)
                   for name, c in calls)
        assert (prod, ["sh", "-c", "kill -HUP 1 2>/dev/null || true"]) in \
            [(n, c) for n, c in calls]


def test_privkey_dropped_into_each_facility_container(monkeypatch):
    calls, transport, _ = _run(monkeypatch)
    for fac in ("flag_service", "health_poller"):
        assert (fac, "/keys/facility") in transport.files
        assert transport.files[(fac, "/keys/facility")].startswith(b"-----BEGIN")
        # chmod 600 on the key
        assert any(name == fac and c == ["chmod", "600", "/keys/facility"]
                   for name, c in calls)
    # privkey never written to a prod box
    assert not any(name.endswith("_prod") and "facility" in path
                   for (name, path) in transport.files)


def test_custom_key_path(monkeypatch):
    calls, transport, _ = _run(monkeypatch, key_path="/secrets/fac_key")
    assert ("flag_service", "/secrets/fac_key") in transport.files
    assert any(c == ["mkdir", "-p", "/secrets"] for _, c in calls)


# ── GameMgr.provision_facilities (resolves handles, calls provision_facility_ssh) ──

import agents._provisioning as _ap
import agents.transport_docker as _td
from gamemgr.config import (
    ChallengeConfig,
    FlagServiceConfig,
    GameConfig,
    HealthPollerConfig,
    RoundConfig,
    _from_dict,
)
from gamemgr.game_mgr import GameMgr
from models.topology import Team, TopologyGraph


class _Containers:
    _client = object()


class _StubOrch:
    network = None
    containers = _Containers()


def _gm(*, fs_c="flag_service", hp_c="health_poller"):
    teams = [Team(id="team1", dev="d1", prod="p1", token="t1"),
             Team(id="team2", dev="d2", prod="p2", token="t2")]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[ChallengeConfig(name="notes", type="notes", port=5000,
                                    deploy_on=["team1"])],
        rounds=RoundConfig(), agents={},
        flag_service=FlagServiceConfig(base_url="http://fs", container=fs_c),
        health_poller=HealthPollerConfig(base_url="http://hp", container=hp_c),
    )
    return GameMgr(config=cfg, orchestrator=_StubOrch())


def test_provision_facilities_resolves_prods_and_facilities(monkeypatch):
    captured: dict = {}

    def fake_pf(client, transport, prods, facs, **kw):
        captured["prods"] = sorted(h.name for h in prods)
        captured["facs"] = [h.name for h in facs]

    monkeypatch.setattr(_ap, "provision_facility_ssh", fake_pf)
    monkeypatch.setattr(_td, "DockerExecBackend", lambda **k: object())
    g = _gm()
    monkeypatch.setattr(g, "handle_for",
                        lambda n: ContainerHandle(container_id="", name=n, networks=[]))
    g.provision_facilities()
    assert captured["facs"] == ["flag_service", "health_poller"]
    assert captured["prods"] == ["p1", "p2"]


def test_provision_facilities_noop_without_containers(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(_ap, "provision_facility_ssh",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    _gm(fs_c="", hp_c="").provision_facilities()
    assert called["n"] == 0


def test_config_parse_facility_container():
    cfg = _from_dict({
        "teams": [{"id": "team1", "dev": "d", "prod": "p"}],
        "flag_service": {"base_url": "http://fs", "container": "flag_service"},
        "health_poller": {"base_url": "http://hp", "container": "health_poller"},
    })
    assert cfg.flag_service.container == "flag_service"
    assert cfg.health_poller.container == "health_poller"
