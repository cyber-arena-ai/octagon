"""One continuous agent trajectory per team.

The agent writes one `stream-*.jsonl` per chunk (async: one per restart; sync:
one per phase). Consumers (the live panel, an external frontend) want a single
coherent trajectory per team, not a pile of per-chunk files. `merge_by_team()`
collapses `collect_transcripts()`'s per-chunk NORMALIZED cards into one entry per
team — what `/game/agents` serves live and what `write_cards_snapshot()` freezes
into `artifacts/agents.json` for a finished job. A synthetic `chunk_boundary`
card sits between chunks so an agent restart stays visible without breaking the
"one trajectory" reading.

The RAW per-chunk `stream-*.jsonl` files are left untouched in the run artifacts
(the download) for deep-dives; only this normalized/merged view is unified.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from pathlib import Path

from .transcripts import collect_transcripts

log = logging.getLogger(__name__)


def merge_by_team(transcripts: list[dict]) -> list[dict]:
    """Collapse per-chunk transcripts (from `collect_transcripts`, already sorted
    by team/round/phase) into one continuous trajectory per team: concatenate
    each team's chunk `events`, insert a `chunk_boundary` card between chunks, and
    re-aggregate the stats. One dict per team, JSON-ready for `/game/agents`."""
    by_team: "OrderedDict[str, list[dict]]" = OrderedDict()
    for t in transcripts:
        by_team.setdefault(t["team_id"], []).append(t)

    sum_keys = ("events", "errors", "tokens_in", "tokens_cached", "tokens_out")
    out: list[dict] = []
    for team, chunks in by_team.items():
        events: list[dict] = []
        combined = {k: 0 for k in sum_keys}
        for i, ch in enumerate(chunks):
            if i > 0:
                events.append(_boundary_card(i, ch["phase"]))
            events.extend(ch["events"])
            for k in sum_keys:
                combined[k] += ch["stats"].get(k, 0)
        # Recompute the tool tally over the merged events so `tools` /
        # `tools_breakdown` (shown in the panel) reflect the whole trajectory.
        tool_counts: dict[str, int] = {}
        for e in events:
            if e.get("kind") == "tool_use" and e.get("tool_name"):
                tool_counts[e["tool_name"]] = tool_counts.get(e["tool_name"], 0) + 1
        combined["tools"] = sum(tool_counts.values())
        combined["tools_breakdown"] = ", ".join(
            f"{n}×{name}" for name, n in sorted(tool_counts.items(), key=lambda kv: -kv[1])
        ) or "(none)"
        out.append({
            "team_id": team,
            "round_id": None,
            "phase": "merged",
            "chunks": len(chunks),
            "stats": combined,
            "events": events,
        })
    return out


def _boundary_card(index: int, phase: str) -> dict:
    """A divider card marking an agent restart / next chunk (panel renders it as
    a separator; shape matches AgentEvent so existing renderers don't choke)."""
    return {
        "kind": "chunk_boundary",
        "title": f"— agent restart · chunk {index} ({phase}) —",
        "body": "",
        "tool_name": None,
        "is_error": False,
        "tokens_in": None,
        "tokens_out": None,
        "tokens_cached": None,
    }


def write_cards_snapshot(run_dir: Path | str) -> Path:
    """Freeze the merged per-team trajectory to `artifacts/agents.json` so a
    finished job serves the same thing `/game/agents` served live (the live
    GameMgr is gone once the run ends). Same shape as the `/game/agents` body."""
    run_dir = Path(run_dir)
    out = run_dir / "artifacts" / "agents.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run": run_dir.name,
        "transcripts": merge_by_team(collect_transcripts(run_dir)),
    }
    out.write_text(json.dumps(payload))
    return out
