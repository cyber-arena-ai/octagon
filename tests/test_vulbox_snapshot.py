"""Patched-vulbox snapshot (opt-in): commit + save each prod container at end of
run, plus the config flag that gates it. Offline — uses a fake docker client."""

from __future__ import annotations

import json

import pytest

from gamemgr.config import ChallengeConfig, _from_dict
from models.topology import Team
from orchestrator.vulbox_snapshot import snapshot_patched_vulboxes


# ── fake docker client ──────────────────────────────────────────────────────

class _FakeImage:
    def __init__(self, tags, save_error=False):
        self.tags = tags
        self.id = "sha256:deadbeef"
        self._save_error = save_error

    def save(self, named=False):
        if self._save_error:
            raise RuntimeError("save boom")
        yield b"TAR"
        yield b"DATA"


class _FakeContainer:
    def __init__(self, save_error=False):
        self.image = _FakeImage(["cyberarena/chal-pyload-rce:v1"])
        self.committed = None
        self._save_error = save_error

    def commit(self, repository, tag):
        self.committed = (repository, tag)
        return _FakeImage([f"{repository}:{tag}"], save_error=self._save_error)


class _FakeImages:
    def __init__(self, remove_error=False):
        self.removed = []
        self._remove_error = remove_error

    def remove(self, image, force=False):
        if self._remove_error:
            raise RuntimeError("remove boom")
        self.removed.append(image)


class _FakeClient:
    def __init__(self, containers: dict, remove_error=False):
        self.images = _FakeImages(remove_error=remove_error)

        class _C:
            def get(_self, name):
                if name not in containers:
                    raise KeyError(name)  # mirrors docker NotFound
                return containers[name]

        self.containers = _C()


_TEAM = Team(id="team1", dev="team1_dev", prod="team1_prod", token="t")
_CHAL = ChallengeConfig(name="pyload-rce", type="pyload-rce", port=8000, deploy_on=["team1"])


# ── helper ──────────────────────────────────────────────────────────────────

def test_snapshot_commits_saves_and_manifests(tmp_path):
    cont = _FakeContainer()
    client = _FakeClient({"team1_prod": cont})

    manifest = snapshot_patched_vulboxes(
        client, run_dir=tmp_path, teams=[_TEAM], challenges=[_CHAL], ns="")

    # committed under a per-challenge repo
    assert cont.committed[0] == "cyberarena/patched-pyload-rce"
    assert len(manifest) == 1
    e = manifest[0]
    assert e["team"] == "team1"
    assert e["challenge"] == "pyload-rce"
    assert e["base_image"] == "cyberarena/chal-pyload-rce:v1"
    assert e["committed_image"].startswith("cyberarena/patched-pyload-rce:")

    # tar saved into the run dir, manifest written
    tar = tmp_path / "patched-vulbox" / "team1-pyload-rce.tar"
    assert tar.is_file() and tar.read_bytes() == b"TARDATA"
    assert e["size_bytes"] == 7
    mani = json.loads((tmp_path / "patched-vulbox" / "manifest.json").read_text())
    assert mani["snapshots"][0]["challenge"] == "pyload-rce"


def test_snapshot_removes_temp_committed_image(tmp_path):
    cont = _FakeContainer()
    client = _FakeClient({"team1_prod": cont})
    snapshot_patched_vulboxes(
        client, run_dir=tmp_path, teams=[_TEAM], challenges=[_CHAL], ns="")
    # the tar is the deliverable; the committed image must not leak
    assert client.images.removed == ["sha256:deadbeef"]


def test_snapshot_removes_image_even_if_save_fails(tmp_path):
    cont = _FakeContainer(save_error=True)
    client = _FakeClient({"team1_prod": cont})
    manifest = snapshot_patched_vulboxes(
        client, run_dir=tmp_path, teams=[_TEAM], challenges=[_CHAL], ns="")
    assert manifest == []                              # save failed → not counted
    assert client.images.removed == ["sha256:deadbeef"]  # but temp image cleaned up (finally)
    # the truncated tar must not be left behind
    assert not (tmp_path / "patched-vulbox" / "team1-pyload-rce.tar").exists()


def test_snapshot_remove_failure_only_warns(tmp_path):
    cont = _FakeContainer()
    client = _FakeClient({"team1_prod": cont}, remove_error=True)
    manifest = snapshot_patched_vulboxes(
        client, run_dir=tmp_path, teams=[_TEAM], challenges=[_CHAL], ns="")
    # a remove failure must not override the snapshot's success
    assert len(manifest) == 1
    assert (tmp_path / "patched-vulbox" / "team1-pyload-rce.tar").read_bytes() == b"TARDATA"


def test_snapshot_is_best_effort_on_failure(tmp_path):
    # Container not found → skipped, no raise, empty manifest still written.
    client = _FakeClient({})   # .get() raises
    manifest = snapshot_patched_vulboxes(
        client, run_dir=tmp_path, teams=[_TEAM], challenges=[_CHAL], ns="")
    assert manifest == []
    assert (tmp_path / "patched-vulbox" / "manifest.json").is_file()


def test_snapshot_ns_qualifies_container_name(tmp_path):
    cont = _FakeContainer()
    client = _FakeClient({"exp1-team1_prod": cont})   # qualify("team1_prod", "exp1")
    manifest = snapshot_patched_vulboxes(
        client, run_dir=tmp_path, teams=[_TEAM], challenges=[_CHAL], ns="exp1")
    assert len(manifest) == 1
    assert manifest[0]["container"] == "exp1-team1_prod"


# ── config flag ─────────────────────────────────────────────────────────────

def _teams():
    return {"teams": [{"id": "team1", "dev": "d", "prod": "p"}]}


def test_config_snapshot_flag_defaults_false():
    assert _from_dict(_teams()).snapshot_patched_vulbox is False


def test_config_snapshot_flag_true():
    cfg = _from_dict({**_teams(), "snapshot_patched_vulbox": True})
    assert cfg.snapshot_patched_vulbox is True


def test_config_snapshot_flag_non_bool_rejected():
    with pytest.raises(ValueError, match="snapshot_patched_vulbox"):
        _from_dict({**_teams(), "snapshot_patched_vulbox": "yes"})
