"""Per-harness agent images: `AgentManager.IMAGE` ↔ `dockerfiles/agent/<p>/Dockerfile`.

The agent image is layered — `cyberarena/agent-base` (tooling, no CLI) plus one
`cyberarena/agent-<provider>` per harness, `FROM` the base. These tests pin the
three places that must agree: the adapters' `IMAGE`, the Dockerfile directories,
and the config-load warning that catches a dev node pointed at the wrong image.
Offline; no Docker."""

from __future__ import annotations

import logging
from pathlib import Path

from agents import registry
from agents.manager import AgentManager
from gamemgr import config as gcfg
from models.agent import AgentConfig

ROOT = Path(__file__).parents[1]
AGENT_DIR = ROOT / "dockerfiles" / "agent"


def test_every_declared_image_has_a_dockerfile_and_vice_versa():
    declared = registry.agent_images()
    on_disk = {
        f"{registry.AGENT_IMAGE_PREFIX}{d.name}": f"{registry.AGENT_DOCKERFILES_DIR}/{d.name}"
        for d in AGENT_DIR.iterdir() if d.is_dir()
    }
    assert declared == on_disk
    for ctx in declared.values():
        assert (ROOT / ctx / "Dockerfile").is_file(), ctx


def test_harness_dockerfiles_inherit_the_base():
    for image, ctx in registry.agent_images().items():
        text = (ROOT / ctx / "Dockerfile").read_text()
        if image == AgentManager.IMAGE:
            assert "FROM ubuntu:" in text
            assert "npm install -g" not in text, "no harness CLI in the base"
        else:
            assert "ARG BASE_IMAGE=cyberarena/agent-base" in text, image
            assert "FROM ${BASE_IMAGE}" in text, image


def test_expected_image_follows_provider_resolution():
    assert registry.expected_image(AgentConfig(cli="claude")) == "cyberarena/agent-claude"
    assert registry.expected_image(AgentConfig(cli="codex exec", provider="codex")) == "cyberarena/agent-codex"
    # `dsh` and `deepseek` share one adapter, hence one image.
    assert registry.expected_image(AgentConfig(cli="dsh", provider="dsh")) == "cyberarena/agent-deepseek"
    assert registry.expected_image(AgentConfig(cli="dsh", provider="deepseek")) == "cyberarena/agent-deepseek"
    # No LLM CLI → the harness-less base. script must NOT inherit claude's image.
    assert registry.expected_image(AgentConfig(cli="python3 x.py", provider="script")) == AgentManager.IMAGE
    assert registry.expected_image(AgentConfig(cli="idle", provider="idle")) == AgentManager.IMAGE


def _raw(team_images: dict[str, tuple[str, str]]) -> dict:
    """{team_id: (provider, dev image)} → a minimal 2-team raw config."""
    teams, nodes = [], []
    for i, (tid, (provider, image)) in enumerate(team_images.items(), 1):
        teams.append({"id": tid, "dev": f"{tid}_dev", "prod": f"{tid}_prod",
                      "agent": {"provider": provider, "cli": provider}})
        nodes.append({"id": f"{tid}_dev", "image": image, "networks": []})
        nodes.append({"id": f"{tid}_prod", "image": "cyberarena/chal-x:v1", "networks": []})
    return {"teams": teams, "nodes": nodes, "networks": {}}


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_config_load_is_silent_when_dev_images_match(caplog):
    caplog.set_level(logging.WARNING, logger=gcfg.log.name)
    gcfg._from_dict(_raw({"team1": ("claude", "cyberarena/agent-claude"),
                          "team2": ("codex", "cyberarena/agent-codex")}))
    assert not [w for w in _warnings(caplog) if "dev node" in w]


def test_config_load_warns_on_provider_image_mismatch(caplog):
    caplog.set_level(logging.WARNING, logger=gcfg.log.name)
    gcfg._from_dict(_raw({"team1": ("claude", "cyberarena/agent-codex"),
                          "team2": ("codex", "cyberarena/agent-codex")}))
    ws = [w for w in _warnings(caplog) if "dev node" in w]
    assert len(ws) == 1
    assert "team1" in ws[0] and "cyberarena/agent-claude" in ws[0]


def test_config_load_points_retired_all_in_one_image_at_the_split(caplog):
    caplog.set_level(logging.WARNING, logger=gcfg.log.name)
    gcfg._from_dict(_raw({"team1": ("claude", "cyberarena/agent"),
                          "team2": ("script", "cyberarena/agent")}))
    ws = [w for w in _warnings(caplog) if "dev node" in w]
    assert len(ws) == 2
    assert all("no longer exists" in w for w in ws)
    assert "cyberarena/agent-claude" in ws[0] and "cyberarena/agent-base" in ws[1]


def test_stale_check_sources_include_base_for_harness_images():
    import main as cli
    baked = cli._baked_by()
    base = ("dockerfiles/agent/base/Dockerfile", "dockerfiles/agent/base")
    assert baked["cyberarena/agent-base"] == [base]
    assert baked["cyberarena/agent-codex"] == [
        base, ("dockerfiles/agent/codex/Dockerfile", "dockerfiles/agent/codex")]
    # Non-agent infra images keep the repo root as their build context.
    assert baked["cyberarena/mcp-gateway"] == [("dockerfiles/mcp-gateway/Dockerfile", ".")]
