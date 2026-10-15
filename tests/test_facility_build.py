"""v3 per-game facility image build: challenges declare facility-side deps via
flag_/health_facility_setup.sh; build_facility_images layers the DEPLOYED
challenges' scripts onto the lean base, per facility. Offline (fake docker)."""

from __future__ import annotations

import types
from pathlib import Path

import facility.build as fb
from challenges.registry import build_challenge
from gamemgr.config import ChallengeConfig, GameConfig
from models.topology import TopologyGraph


def test_challenge_without_script_returns_none():
    # notes is stdlib facility-side — ships no setup scripts.
    notes = build_challenge("notes")
    assert notes.flag_facility_setup_script is None
    assert notes.health_facility_setup_script is None


def test_scripted_challenge_exposes_paths(tmp_path):
    # The property auto-detects a conventional file next to the challenge module.
    fake_mod = tmp_path / "fake_challenge.py"
    fake_mod.write_text("x = 1\n")
    (tmp_path / "flag_facility_setup.sh").write_text("pip install foo\n")

    import importlib.util
    spec = importlib.util.spec_from_file_location("fake_challenge", fake_mod)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from challenges.interface import Challenge

    class _Ch(Challenge):
        name = "fake"
        vulbox = functionality_test = flag_handler = docs = None  # unused here

    _Ch.__module__ = "fake_challenge"
    import sys
    sys.modules["fake_challenge"] = mod
    ch = _Ch()
    assert ch.flag_facility_setup_script == tmp_path / "flag_facility_setup.sh"
    assert ch.health_facility_setup_script is None


def _fake_challenge(flag_script: Path | None, health_script: Path | None):
    return types.SimpleNamespace(
        flag_facility_setup_script=flag_script,
        health_facility_setup_script=health_script,
    )


def test_build_layers_only_deployed_scripts_per_facility(tmp_path, monkeypatch):
    fs_script = tmp_path / "btxflag.sh"; fs_script.write_text("pip install nclib\n")
    hp_script = tmp_path / "btxhealth.sh"; hp_script.write_text("pip install nclib\n")
    fakes = {
        "btx": _fake_challenge(fs_script, hp_script),   # both facilities
        "notes": _fake_challenge(None, None),           # neither
    }
    monkeypatch.setattr(fb, "build_challenge", lambda n: fakes[n])

    captured: dict[str, tuple[str, list[str]]] = {}

    class _Images:
        def build(self, *, path, tag, **kw):
            df = (Path(path) / "Dockerfile").read_text()
            files = sorted(p.name for p in Path(path).iterdir())
            assert kw.get("pull") is False  # use the local (amd64) base, don't pull
            captured[tag] = (df, files)
            return (None, [])

    client = types.SimpleNamespace(images=_Images())
    cfg = GameConfig(
        topology=TopologyGraph(teams=[], nodes=[], networks=[]),
        challenges=[ChallengeConfig("btx", "btx", 9000, ["team1"]),
                    ChallengeConfig("notes", "notes", 5000, ["team1"])],
    )
    built = fb.build_facility_images(cfg, client=client)
    # No ns -> a single-slot runner, which cannot race itself, so `latest`.
    assert built == ["cyberarena/flag-service:latest",
                     "cyberarena/health-poller:latest"]

    flag_df, flag_files = captured["cyberarena/flag-service:latest"]
    assert "FROM cyberarena/flag-service-base" in flag_df
    assert "btx.sh" in flag_files and "RUN bash /tmp/facility-setup/btx.sh" in flag_df
    assert "notes.sh" not in flag_files            # notes ships no script

    health_df, health_files = captured["cyberarena/health-poller:latest"]
    assert "FROM cyberarena/health-poller-base" in health_df
    assert "btx.sh" in health_files


# --- the shared-tag race (orche#154) ---------------------------------------

def _capturing_client():
    """Fake docker client recording tag -> the challenge packages copied in."""
    captured: dict[str, list[str]] = {}

    class _Images:
        def build(self, *, path, tag, **kw):
            df = (Path(path) / "Dockerfile").read_text()
            captured[tag] = [ln.split("/app/challenges/")[1].rstrip("/")
                             for ln in df.splitlines()
                             if ln.startswith("COPY") and "/app/challenges/" in ln]
            return (None, [])

    return types.SimpleNamespace(images=_Images()), captured


def _one_challenge_cfg(name: str, port: int, **kw):
    return GameConfig(
        topology=TopologyGraph(teams=[], nodes=kw.pop("nodes", []), networks=[]),
        challenges=[ChallengeConfig(name, name, port, ["team1"])],
        **kw,
    )


def _stub_registry(monkeypatch, tmp_path, names):
    """Fake challenges whose package dir is named for the challenge, so the
    Dockerfile's COPY line reveals which challenge's code went into the image."""
    fakes = {}
    for n in names:
        pkg = tmp_path / n
        pkg.mkdir(exist_ok=True)
        (pkg / "__init__.py").write_text("")
        f = _fake_challenge(None, None)
        fakes[n] = f
    monkeypatch.setattr(fb, "build_challenge", lambda n: fakes[n])
    monkeypatch.setattr(fb, "_package_dir", lambda ch: tmp_path / next(
        n for n, v in fakes.items() if v is ch))
    return fakes


def test_namespace_is_in_the_tag(tmp_path, monkeypatch):
    _stub_registry(monkeypatch, tmp_path, ["btx"])
    client, captured = _capturing_client()
    built = fb.build_facility_images(_one_challenge_cfg("btx", 9000), "s2",
                                     client=client)
    assert built == ["cyberarena/flag-service:s2", "cyberarena/health-poller:s2"]
    assert set(captured) == set(built)


def test_two_namespaces_do_not_overwrite_each_other(tmp_path, monkeypatch):
    """The bug itself: two games on one host, different challenges.

    On a shared tag the second build replaces the first, and whichever game
    starts its container second gets an image carrying the OTHER challenge's
    package -- its probe import then raises ModuleNotFoundError and
    /flags/configure 500s at setup. Namespaced, each keeps its own package.
    """
    _stub_registry(monkeypatch, tmp_path, ["btx", "notes"])
    client, captured = _capturing_client()
    fb.build_facility_images(_one_challenge_cfg("btx", 9000), "s0", client=client)
    fb.build_facility_images(_one_challenge_cfg("notes", 5000), "s1", client=client)

    assert captured["cyberarena/flag-service:s0"] == ["btx"]
    assert captured["cyberarena/flag-service:s1"] == ["notes"]


def test_facility_node_order_matches_the_builder():
    """main._FACILITY_NODE_ORDER is zipped against build_facility_images' return
    value, so the two orders must not drift."""
    import main
    assert [f for f, _ in main._FACILITY_NODE_ORDER] == [f for f, _ in fb._FACILITIES]


def test_repoint_facility_nodes_rewrites_only_the_facility_nodes():
    import main
    from gamemgr.config import FlagServiceConfig, HealthPollerConfig
    from models.topology import Node
    nodes = [
        Node(id="team1_prod", image="cyberarena/chal-btx:v1", networks=[]),
        Node(id="flag_service", image="cyberarena/flag-service", networks=[]),
        Node(id="health_poller", image="cyberarena/health-poller", networks=[]),
    ]
    cfg = _one_challenge_cfg(
        "btx", 9000, nodes=nodes,
        flag_service=FlagServiceConfig(container="flag_service"),
        health_poller=HealthPollerConfig(container="health_poller"),
    )
    same_list = cfg.topology.nodes          # orch holds this exact object

    main._repoint_facility_nodes(
        cfg, ["cyberarena/flag-service:s3", "cyberarena/health-poller:s3"])

    assert cfg.topology.nodes is same_list, "must mutate in place; orch holds it"
    by_id = {n.id: n.image for n in cfg.topology.nodes}
    assert by_id["flag_service"] == "cyberarena/flag-service:s3"
    assert by_id["health_poller"] == "cyberarena/health-poller:s3"
    assert by_id["team1_prod"] == "cyberarena/chal-btx:v1", "untouched"


def test_required_base_images_still_sees_bare_facility_names():
    """Ordering guard: _required_base_images runs BEFORE the repoint and
    exact-matches the bare names to decide which -base images are required. If
    the repoint ever moved earlier, this would stop requiring the bases."""
    import main
    from models.topology import Node
    cfg = _one_challenge_cfg("btx", 9000, nodes=[
        Node(id="flag_service", image="cyberarena/flag-service", networks=[]),
        Node(id="health_poller", image="cyberarena/health-poller", networks=[]),
    ])
    req = main._required_base_images(cfg)
    assert "cyberarena/flag-service-base" in req
    assert "cyberarena/health-poller-base" in req
