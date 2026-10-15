"""Focused source_in_dev prompt and provisioning contract coverage.

These tests intentionally target the current L1+L2 prompt API and the real
prod-container source copy.  They do not preserve the historical host-tree
helper or the removed ``defendable`` prompt argument.
"""

from __future__ import annotations

import io
import tarfile

import pytest

from agents import _provisioning
from agents.prompts import build_async_prompt
from gamemgr.config import (
    ChallengeConfig,
    GameConfig,
    RoundConfig,
    _challenge_from_dict,
)
from gamemgr.game_mgr import GameMgr
from models.agent import AgentConfig
from models.topology import Team, TopologyGraph
from orchestrator.handle import ContainerHandle


class _Transport:
    def __init__(self) -> None:
        self.writes: dict[str, bytes] = {}
        self.trees = 0

    def write_file(self, handle, remote_path: str, content: bytes) -> None:
        self.writes[remote_path] = content

    def write_tree(self, handle, dest_dir: str, tar_stream) -> None:
        """Unpack into the same `writes` map the per-file path used.

        The source tree now ships as ONE archive instead of a write per file,
        so the fake has to expand it to keep asserting on paths and contents —
        which is the part that matters (what was staged, what was excluded).
        `trees` counts the calls so a test can pin the "one archive, not one
        call per file" property that the batching exists for.
        """
        import tarfile as _tarfile

        self.trees += 1
        with _tarfile.open(fileobj=tar_stream, mode="r:*") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                handle_f = archive.extractfile(member)
                if handle_f is None:
                    continue
                self.writes[f"{dest_dir}/{member.name}"] = handle_f.read()


class _FakeExec:
    exit_code = 0
    output = b""


class _FakeContainer:
    def __init__(self, archive: bytes | None = None) -> None:
        self.archive = archive
        self.archive_paths: list[str] = []
        self.exec_calls: list[list[str] | str] = []

    def get_archive(self, path):
        self.archive_paths.append(path)
        if self.archive is None:
            raise KeyError(path)
        return iter([self.archive]), {"name": "challenge"}

    def exec_run(self, cmd, **kwargs):
        self.exec_calls.append(cmd)
        return _FakeExec()


class _FakeClient:
    def __init__(self, containers: dict[str, _FakeContainer]) -> None:
        self._containers = containers

    @property
    def containers(self):
        return self

    def get(self, name):
        return self._containers[name]


def _handle() -> ContainerHandle:
    return ContainerHandle(container_id="dev", name="team1_dev", networks=[])


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_provision_dev_source_from_prod_copies_only_safe_source_members():
    archive = _tar_bytes(
        {
            "challenge/marimo/__init__.py": b"# package\n",
            "challenge/app.py": b"import marimo\n",
            "./challenge/worker.py": b"run()\n",
            "challenge/checker.sh": b"secret harness\n",
            "challenge/restart.sh": b"secret harness\n",
            "challenge/scoring.sh": b"secret scoring\n",
            "challenge/readflag": b"binary",
            "challenge/nested/flag.txt": b"FLAG{secret}",
            "challenge/.git/config": b"repository metadata",
            "challenge/__pycache__/x.pyc": b"bytecode",
            "challenge/../escape.py": b"must not escape",
            "outside/rogue.py": b"wrong archive root",
        }
    )
    prod = _FakeContainer(archive)
    dev = _FakeContainer()
    transport = _Transport()

    dest = _provisioning.provision_dev_source_from_prod(
        _FakeClient({"team1_prod": prod, "team1_dev": dev}),
        transport,
        _handle(),
        service_name="marimo-rce",
        prod_container_name="team1_prod",
    )

    assert dest == "/srv/challenge/marimo-rce"
    assert prod.archive_paths == ["/srv/challenge"]
    assert transport.writes == {
        "/srv/challenge/marimo-rce/marimo/__init__.py": b"# package\n",
        "/srv/challenge/marimo-rce/app.py": b"import marimo\n",
        "/srv/challenge/marimo-rce/worker.py": b"run()\n",
    }
    assert ["mkdir", "-p", dest] in dev.exec_calls
    # The whole tree ships as ONE archive. Staging it a file at a time cost a
    # round trip plus an in-container mkdir EACH, which put a 34-minute stall in
    # front of ROUND_START on a 45k-file challenge — unbounded, because that
    # time is spent before the round starts and `round_secs` never sees it.
    assert transport.trees == 1


def test_provision_dev_source_from_prod_missing_tree_raises():
    client = _FakeClient(
        {"team1_prod": _FakeContainer(), "team1_dev": _FakeContainer()}
    )

    with pytest.raises(FileNotFoundError, match="marimo-rce"):
        _provisioning.provision_dev_source_from_prod(
            client,
            _Transport(),
            _handle(),
            service_name="marimo-rce",
            prod_container_name="team1_prod",
        )


def test_provision_dev_source_from_prod_requires_docker_client():
    with pytest.raises(RuntimeError, match="docker client"):
        _provisioning.provision_dev_source_from_prod(
            None,
            _Transport(),
            _handle(),
            service_name="marimo-rce",
            prod_container_name="team1_prod",
        )


def test_team_source_services_selects_only_enabled_and_deployed():
    import main

    class Challenge:
        def __init__(self, name, deploy_on, source_in_dev):
            self.name = name
            self.deploy_on = deploy_on
            self.source_in_dev = source_in_dev

    class Game:
        class config:
            challenges = [
                Challenge("notes", ["team1"], True),
                Challenge("other", ["team2"], True),
                Challenge("both", ["team1", "team2"], True),
                Challenge("off", ["team1", "team2"], False),
            ]

    assert main._team_source_services(Game(), "team1") == ["notes", "both"]
    assert main._team_source_services(Game(), "team2") == ["other", "both"]


def _agent_config() -> AgentConfig:
    return AgentConfig(
        cli="codex",
        provider="codex",
        team_id="team1",
        own_prod_host="team1_prod",
        opposing_prod_hosts=("team2_prod",),
    )


def _stub_prompt_module(monkeypatch) -> None:
    class _PromptModule:
        @staticmethod
        def build_advanced_prompt(cfg, *, service_name, hint):
            return f"SERVICE {service_name}; hint={hint}"

    monkeypatch.setattr("agents.prompts._load_module", lambda _name: _PromptModule)


def _prompt(monkeypatch, *, solo_role: str = "", **kwargs) -> str:
    _stub_prompt_module(monkeypatch)
    return build_async_prompt(
        _agent_config(),
        service_name="notes",
        total_rounds=5,
        round_min=2,
        hint=False,
        solo_role=solo_role,
        **kwargs,
    )


def test_source_in_dev_default_off_is_prompt_byte_identical(monkeypatch):
    baseline = _prompt(monkeypatch)
    explicit_off = _prompt(monkeypatch, source_in_dev=False)

    assert explicit_off == baseline
    assert "SOURCE ON YOUR DEV BOX" not in explicit_off


@pytest.mark.parametrize("solo_role", ["", "attack", "defense"])
def test_source_in_dev_on_prompt_names_staging_path_for_every_role(
    monkeypatch, solo_role
):
    prompt = _prompt(monkeypatch, solo_role=solo_role, source_in_dev=True)

    assert "SOURCE ON YOUR DEV BOX" in prompt
    assert "/srv/challenge/notes/" in prompt


def _game(*, source_in_dev: bool) -> GameMgr:
    teams = [
        Team(id="team1", dev="team1_dev", prod="team1_prod", token="t1"),
        Team(id="team2", dev="team2_dev", prod="team2_prod", token="t2"),
    ]
    cfg = GameConfig(
        topology=TopologyGraph(teams=teams, nodes=[], networks=[]),
        challenges=[
            ChallengeConfig(
                name="notes",
                type="FILE_AT_PATH",
                port=5000,
                deploy_on=["team1", "team2"],
                source_in_dev=source_in_dev,
            )
        ],
        rounds=RoundConfig(mode="async", round_secs=120, total_rounds=5),
        agents={team.id: _agent_config() for team in teams},
    )

    class _Stub:
        network = None

    return GameMgr(config=cfg, orchestrator=_Stub())


def test_gamemgr_threads_source_in_dev_into_prompt(monkeypatch):
    _stub_prompt_module(monkeypatch)
    off = _game(source_in_dev=False)._system_prompt_for(_agent_config(), "notes")
    on = _game(source_in_dev=True)._system_prompt_for(_agent_config(), "notes")

    assert "SOURCE ON YOUR DEV BOX" not in off
    assert "SOURCE ON YOUR DEV BOX" in on
    assert "/srv/challenge/notes/" in on


def test_source_in_dev_config_defaults_off_accepts_bool_and_rejects_coercion():
    base = {
        "name": "notes",
        "type": "FILE_AT_PATH",
        "port": 5000,
        "deploy_on": ["team1"],
    }

    assert _challenge_from_dict(0, base).source_in_dev is False
    assert _challenge_from_dict(0, {**base, "source_in_dev": True}).source_in_dev is True
    for invalid in ("false", 0, 1, None):
        with pytest.raises(ValueError, match="source_in_dev must be a boolean"):
            _challenge_from_dict(0, {**base, "source_in_dev": invalid})
