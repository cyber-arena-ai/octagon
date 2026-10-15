"""`main._missing_base_images` — start's pre-flight that fails fast (with a
'run build_images.sh' hint) when infra/base images aren't built yet.

`start` builds the per-challenge images (cyberarena/chal-*) and the per-game
facility images (cyberarena/flag-service / -health-poller) itself, so those must
NOT be flagged as missing. The infra images (agent, mcp-gateway, api-router-cfg)
and the facility BASE images must be present. Offline (fake docker client)."""

from __future__ import annotations

import types
from pathlib import Path

import docker

import main as cli
from gamemgr.config import GameConfig
from models.topology import Node, TopologyGraph


def _cfg(node_images: list[str]) -> GameConfig:
    nodes = [Node(id=f"n{i}", image=img, networks=[]) for i, img in enumerate(node_images)]
    return GameConfig(topology=TopologyGraph(teams=[], nodes=nodes, networks=[]))


def _fake_docker(present: set[str], monkeypatch):
    class _Images:
        def get(self, ref):
            if ref in present:
                return object()
            raise docker.errors.ImageNotFound(ref)

    monkeypatch.setattr(docker, "from_env", lambda: types.SimpleNamespace(images=_Images()))


def test_excludes_challenge_and_per_game_facility_images(monkeypatch):
    # chal-* and the per-game facility tags are built by start → never required,
    # so even though they're absent locally they are NOT reported missing.
    _fake_docker({"cyberarena/agent", "cyberarena/mcp-gateway",
                          "cyberarena/api-router-cfg",
                          "cyberarena/flag-service-base",
                          "cyberarena/health-poller-base"}, monkeypatch)
    cfg = _cfg([
        "cyberarena/agent", "cyberarena/mcp-gateway", "cyberarena/api-router-cfg",
        "cyberarena/flag-service", "cyberarena/health-poller",
        "cyberarena/chal-nautilus-ico:abc123",
    ])
    assert cli._missing_base_images(cfg) == []


def test_reports_missing_infra_and_facility_bases(monkeypatch):
    # agent present; mcp-gateway + the facility bases absent → reported (sorted).
    _fake_docker({"cyberarena/agent"}, monkeypatch)
    cfg = _cfg([
        "cyberarena/agent", "cyberarena/mcp-gateway",
        "cyberarena/flag-service", "cyberarena/health-poller",
        "cyberarena/chal-notes",
    ])
    assert cli._missing_base_images(cfg) == [
        "cyberarena/flag-service-base",
        "cyberarena/health-poller-base",
        "cyberarena/mcp-gateway",
    ]


def test_no_facility_nodes_means_no_base_requirement(monkeypatch):
    # A config without the per-game facility nodes shouldn't require the bases.
    _fake_docker({"cyberarena/agent"}, monkeypatch)
    cfg = _cfg(["cyberarena/agent", "cyberarena/chal-notes"])
    assert cli._missing_base_images(cfg) == []


# --- `main._stale_base_images` — advisory freshness warning ------------------

def _fake_docker_created(created: dict[str, str], monkeypatch):
    class _Images:
        def get(self, ref):
            if ref in created:
                return types.SimpleNamespace(attrs={"Created": created[ref]})
            raise docker.errors.ImageNotFound(ref)

    monkeypatch.setattr(docker, "from_env", lambda: types.SimpleNamespace(images=_Images()))


def _pin_source_commit(monkeypatch, epoch: int):
    import subprocess
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=f"{epoch}\n"),
    )


def test_flags_only_never_rebuilt_images_older_than_their_sources(monkeypatch):
    # One config covering every selection rule: a stale base (via its per-game
    # image), a fresh base, a stale infra image matched by exact name, and a
    # chal-* that has no entry and so is never flagged however ancient.
    _fake_docker_created({
        "cyberarena/flag-service-base": "2026-08-21T01:24:02.52088836-07:00",
        "cyberarena/health-poller-base": "2026-09-04T00:00:00.0-07:00",
        "cyberarena/mcp-gateway": "2026-06-26T01:19:59.947878531-07:00",
        "cyberarena/chal-notes": "2020-01-01T00:00:00.0-07:00",
    }, monkeypatch)
    _pin_source_commit(monkeypatch, 1788480000)  # 2026-09-03 in the images' offset
    cfg = _cfg(["cyberarena/flag-service", "cyberarena/health-poller",
                "cyberarena/mcp-gateway", "cyberarena/chal-notes"])
    assert [r for r, _ in cli._stale_base_images(cfg)] == [
        "cyberarena/flag-service-base",
        "cyberarena/mcp-gateway",
    ]


def test_unresolvable_source_commit_does_not_raise(monkeypatch):
    # No .git, or no commit for these paths: degrade to "can't tell", never break
    # a run that would otherwise work.
    import subprocess
    _fake_docker_created(
        {"cyberarena/flag-service-base": "2020-01-01T00:00:00.0-07:00"}, monkeypatch)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=128, stdout=""),
    )
    cfg = _cfg(["cyberarena/flag-service"])
    assert cli._stale_base_images(cfg) == []
