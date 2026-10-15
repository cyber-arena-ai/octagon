"""DEFCON: ArtifactStore path allocator.

Pins the layout reserved for forensic / replay / scoring milestones so
those landings don't have to re-decide paths. v1 fills 3 kinds
(api-logs, agent-logs, service-logs); the other 5 are reserved as
empty subdirs.

No docker needed — pure pathlib.
"""

from __future__ import annotations

import pytest

from gamemgr.artifacts import ArtifactKind, ArtifactStore


# ── kinds / enum invariants ────────────────────────────────────────────


# ── store construction: reserves layout up-front ───────────────────────


def test_store_creates_artifacts_root_and_readme(tmp_path):
    """At construction time, artifacts/ exists with a README — per-team
    subdirs appear lazily as artifacts land (no upfront reservation)."""
    store = ArtifactStore(tmp_path)

    root = tmp_path / "artifacts"
    assert root.is_dir()
    assert store.root == root
    assert (root / "README.md").is_file()
    # Layout is team-first now — no `<kind>/` subdirs at the top level.
    # No team dirs exist yet because nothing has been written.
    assert list(p.name for p in root.iterdir() if p.is_dir()) == []


# ── path_for: layout + lazy parent creation ────────────────────────────


def test_path_for_returns_expected_layout(tmp_path):
    """runs/<ts>/artifacts/<team_id>/round-<N>/<filename>.

    Kind is a closed-set tag for the caller but does not appear in the
    path — different kinds live side-by-side under the same round dir,
    disambiguated by filename convention.
    """
    store = ArtifactStore(tmp_path)
    p = store.path_for(
        ArtifactKind.API_LOG,
        team_id="team1",
        round_id=3,
        filename="stream-defense.jsonl",
    )
    assert p == tmp_path / "artifacts" / "team1" / "round-3" / "stream-defense.jsonl"


def test_path_for_groups_all_kinds_under_one_round_dir(tmp_path):
    """All artifact kinds for the same (team, round) share one directory."""
    store = ArtifactStore(tmp_path)
    api_p = store.path_for(ArtifactKind.API_LOG, team_id="team1", round_id=1, filename="stream-defense.jsonl")
    agent_p = store.path_for(ArtifactKind.AGENT_LOG, team_id="team1", round_id=1, filename="agent-defense.log")
    svc_p = store.path_for(ArtifactKind.SERVICE_LOG, team_id="team1", round_id=1, filename="docker.log")
    assert api_p.parent == agent_p.parent == svc_p.parent
    assert api_p.parent == tmp_path / "artifacts" / "team1" / "round-1"


def test_path_for_creates_parent_dirs_lazily(tmp_path):
    """Caller can write to the returned path without mkdir-ing first."""
    store = ArtifactStore(tmp_path)
    p = store.path_for(
        ArtifactKind.AGENT_LOG,
        team_id="team2",
        round_id=7,
        filename="stderr.log",
    )
    assert p.parent.is_dir()
    # And the file is writable right away.
    p.write_text("hi")
    assert p.read_text() == "hi"


def test_path_for_across_multiple_rounds_and_teams_isolates_directories(tmp_path):
    """Per-team-per-round subdirs are independent — writes to one don't
    surface in another."""
    store = ArtifactStore(tmp_path)
    a = store.path_for(ArtifactKind.SERVICE_LOG, team_id="team1", round_id=1, filename="x.log")
    b = store.path_for(ArtifactKind.SERVICE_LOG, team_id="team2", round_id=1, filename="x.log")
    c = store.path_for(ArtifactKind.SERVICE_LOG, team_id="team1", round_id=2, filename="x.log")
    a.write_text("A")
    b.write_text("B")
    c.write_text("C")
    assert a.read_text() == "A"
    assert b.read_text() == "B"
    assert c.read_text() == "C"


# ── escape rejection ───────────────────────────────────────────────────


def test_path_for_rejects_escape_attempts(tmp_path):
    """`..` and absolute paths must be refused — they'd silently bypass
    the artifacts root. Nested relative filenames (`sub/x.log`) are
    fine and stay under the round dir.
    """
    store = ArtifactStore(tmp_path)
    # Escape attempts → ValueError
    for bad in ("../etc/passwd", "foo/../../escape", "/etc/passwd"):
        with pytest.raises(ValueError):
            store.path_for(
                ArtifactKind.EXPLOIT,
                team_id="team1", round_id=1, filename=bad,
            )
    # Nested-but-contained filename is OK
    p = store.path_for(
        ArtifactKind.API_LOG,
        team_id="team1", round_id=1, filename="sub/stream.jsonl",
    )
    p.write_text("ok")
    assert p == tmp_path / "artifacts" / "team1" / "round-1" / "sub" / "stream.jsonl"
    # Non-enum kind → TypeError
    with pytest.raises(TypeError, match="ArtifactKind"):
        store.path_for(
            "api-log",  # type: ignore[arg-type]
            team_id="team1", round_id=1, filename="x.log",
        )
