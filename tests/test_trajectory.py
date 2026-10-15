"""Tests for merging per-chunk agent streams into one trajectory per team
(gamemgr.trajectory): the normalized-card merge (one entry per team, with a
chunk_boundary between chunks) and the frozen artifacts/agents.json snapshot."""

from __future__ import annotations

import json
from pathlib import Path

from gamemgr import trajectory
from gamemgr.transcripts import collect_transcripts

_SYS = '{"type":"system","model":"claude-x"}'
_MSG = ('{"type":"assistant","message":{"usage":{"input_tokens":5,"output_tokens":2},'
        '"content":[{"type":"text","text":"hi"},'
        '{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}')


def _async_run(tmp_path: Path, chunks: dict[int, str]) -> Path:
    """A run dir with team1 async chunks: {n: file_contents}."""
    d = tmp_path / "runs" / "r" / "artifacts" / "team1"
    d.mkdir(parents=True)
    for n, content in chunks.items():
        (d / f"stream-async-{n}.jsonl").write_text(content)
    return tmp_path / "runs" / "r"


def test_merge_by_team_one_entry_per_team_with_boundary_card(tmp_path):
    run = _async_run(tmp_path, {0: _SYS + "\n" + _MSG + "\n", 1: _MSG + "\n"})
    merged = trajectory.merge_by_team(collect_transcripts(run))
    assert len(merged) == 1
    entry = merged[0]
    assert entry["team_id"] == "team1" and entry["phase"] == "merged" and entry["chunks"] == 2
    kinds = [e["kind"] for e in entry["events"]]
    assert kinds.count("chunk_boundary") == 1
    assert kinds[0] != "chunk_boundary"          # boundary sits between chunks
    # stats re-aggregated over the merged events (two tool_use across the chunks)
    assert entry["stats"]["tools"] == 2
    assert "Bash" in entry["stats"]["tools_breakdown"]


def test_merge_single_chunk_has_no_boundary(tmp_path):
    run = _async_run(tmp_path, {0: _MSG + "\n"})
    merged = trajectory.merge_by_team(collect_transcripts(run))
    assert merged[0]["chunks"] == 1
    assert all(e["kind"] != "chunk_boundary" for e in merged[0]["events"])


def test_merge_groups_multiple_teams(tmp_path):
    d = tmp_path / "runs" / "r" / "artifacts"
    (d / "team1").mkdir(parents=True)
    (d / "team2").mkdir(parents=True)
    (d / "team1" / "stream-async-0.jsonl").write_text(_MSG + "\n")
    (d / "team2" / "stream-async-0.jsonl").write_text(_MSG + "\n")
    merged = trajectory.merge_by_team(collect_transcripts(tmp_path / "runs" / "r"))
    assert {m["team_id"] for m in merged} == {"team1", "team2"}
    assert all(m["chunks"] == 1 for m in merged)


def test_write_cards_snapshot_matches_endpoint_shape(tmp_path):
    run = _async_run(tmp_path, {0: _SYS + "\n" + _MSG + "\n", 1: _MSG + "\n"})
    out = trajectory.write_cards_snapshot(run)
    assert out == run / "artifacts" / "agents.json"
    payload = json.loads(out.read_text())
    assert payload["run"] == "r"
    assert len(payload["transcripts"]) == 1
    assert payload["transcripts"][0]["phase"] == "merged"
