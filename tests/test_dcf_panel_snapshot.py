"""Panel snapshot writer — embeds events.jsonl into a copy of panel.html.

The output is a self-contained file that re-renders the live panel
offline. These tests just verify the embedding mechanics — the panel
JS itself is exercised by opening the snapshot in a browser, not here.
"""

from __future__ import annotations

import json

from gamemgr.panel_snapshot import write_snapshot


def _read_var_from_snapshot(html: str, var: str) -> list[dict]:
    """Parse a `window.<var> = [...];` payload back out of a snapshot.
    The payload is single-line JSON (json.dumps escapes any newline), so
    the statement's own `;\\n` is an unambiguous terminator."""
    needle = f"window.{var} = "
    start = html.index(needle) + len(needle)
    end = html.index(";\n", start)
    raw = html[start:end].replace("<\\/", "</")  # reverse the </ → <\/ escape
    return json.loads(raw)


def _read_events_from_snapshot(html: str) -> list[dict]:
    return _read_var_from_snapshot(html, "INITIAL_EVENTS")


def test_write_snapshot_creates_panel_html_with_embedded_events(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        '{"id": "a", "ts": 1.0, "type": "ROUND_START", "team_id": null, "round_id": 1, "phase": "INIT", "payload": {}}\n'
        '{"id": "b", "ts": 2.0, "type": "PHASE_CHANGE", "team_id": null, "round_id": 1, "phase": "DEFENSE", "payload": {}}\n'
    )

    out = write_snapshot(tmp_path)
    assert out == tmp_path / "panel.html"
    assert out.is_file()

    html = out.read_text()
    # The boot block must be intact (we only inject one <script> tag).
    assert "window.INITIAL_EVENTS" in html
    assert "if (Array.isArray(window.INITIAL_EVENTS))" in html

    events = _read_events_from_snapshot(html)
    assert len(events) == 2
    assert events[0]["type"] == "ROUND_START"
    assert events[1]["type"] == "PHASE_CHANGE"


def test_write_snapshot_embeds_transcripts(tmp_path):
    """The offline snapshot inlines parsed agent transcripts (window.
    INITIAL_TRANSCRIPTS) so it shows the same card stack as the live
    panel — not just the timeline."""
    (tmp_path / "events.jsonl").write_text(
        '{"id": "a", "ts": 1.0, "type": "ROUND_START", "team_id": null, "round_id": 1, "phase": "INIT", "payload": {}}\n'
    )
    stream = tmp_path / "artifacts" / "team1" / "round-1" / "stream-defense.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text(
        '{"type":"system","subtype":"init","model":"m","tools":["Bash"]}\n'
        '{"type":"assistant","message":{"usage":{"input_tokens":5,"output_tokens":1},'
        '"content":[{"type":"text","text":"hi"}]}}\n'
    )

    html = write_snapshot(tmp_path).read_text()
    transcripts = _read_var_from_snapshot(html, "INITIAL_TRANSCRIPTS")
    # transcripts are merged one-per-team (phase "merged"), matching /game/agents.
    assert len(transcripts) == 1
    assert (transcripts[0]["team_id"], transcripts[0]["phase"], transcripts[0]["chunks"]) == ("team1", "merged", 1)
    assert [e["kind"] for e in transcripts[0]["events"]] == ["system", "text"]


def test_write_snapshot_embeds_metrics(tmp_path):
    """The offline snapshot inlines artifacts/metrics.json (window.INITIAL_METRICS)
    so the 'Timeline metrics' section renders offline too — the live panel gets it
    from /game/metrics, which isn't reachable once the run is over."""
    (tmp_path / "events.jsonl").write_text(
        '{"id":"a","ts":1.0,"type":"ROUND_START","team_id":null,"round_id":1,"phase":"INIT","payload":{}}\n'
    )
    art = tmp_path / "artifacts"
    art.mkdir(parents=True)
    metrics = {"team1": {"provider": "anthropic",
                         "summary": {"mean_tokens_per_s": 12.3, "n_tool_calls": 4}}}
    (art / "metrics.json").write_text(json.dumps(metrics))

    html = write_snapshot(tmp_path).read_text()
    assert "window.INITIAL_METRICS" in html
    assert "renderMetrics(window.INITIAL_METRICS)" in html   # boot block wired to render offline
    embedded = _read_var_from_snapshot(html, "INITIAL_METRICS")
    assert embedded["team1"]["provider"] == "anthropic"


def test_write_snapshot_metrics_absent_embeds_empty(tmp_path):
    """No metrics.json → INITIAL_METRICS is an empty object (section stays hidden),
    not a crash."""
    (tmp_path / "events.jsonl").write_text(
        '{"id":"a","ts":1.0,"type":"ROUND_START","team_id":null,"round_id":1,"phase":"INIT","payload":{}}\n'
    )
    html = write_snapshot(tmp_path).read_text()
    assert _read_var_from_snapshot(html, "INITIAL_METRICS") == {}


def test_write_snapshot_embeds_empty_transcripts_when_no_artifacts(tmp_path):
    (tmp_path / "events.jsonl").write_text("")
    html = write_snapshot(tmp_path).read_text()
    assert _read_var_from_snapshot(html, "INITIAL_TRANSCRIPTS") == []


def test_write_snapshot_handles_missing_events_file(tmp_path):
    """Missing events.jsonl is logged and snapshot still writes
    (with empty INITIAL_EVENTS) — never let an empty run break the
    teardown path that writes this file."""
    # No events.jsonl in tmp_path
    out = write_snapshot(tmp_path)
    assert out.is_file()
    events = _read_events_from_snapshot(out.read_text())
    assert events == []


def test_write_snapshot_skips_malformed_lines(tmp_path):
    """One bad line in events.jsonl shouldn't kill the snapshot —
    skip and keep going."""
    (tmp_path / "events.jsonl").write_text(
        '{"id": "a", "ts": 1.0, "type": "ROUND_START", "team_id": null, "round_id": 1, "phase": "INIT", "payload": {}}\n'
        "this is not json\n"
        '{"id": "b", "ts": 2.0, "type": "ROUND_STOP", "team_id": null, "round_id": 1, "phase": "ATTACK", "payload": {}}\n'
    )
    out = write_snapshot(tmp_path)
    events = _read_events_from_snapshot(out.read_text())
    assert [e["type"] for e in events] == ["ROUND_START", "ROUND_STOP"]


def test_write_snapshot_escapes_script_close_tag(tmp_path):
    """Payload strings containing literal `</script>` would otherwise
    terminate the inline <script> early and break the snapshot."""
    (tmp_path / "events.jsonl").write_text(
        '{"id": "a", "ts": 1.0, "type": "FLAG_SUBMITTED", "team_id": "team1", "round_id": 1, "phase": "ATTACK", '
        '"payload": {"reason": "got </script> in payload"}}\n'
    )
    out = write_snapshot(tmp_path)
    html = out.read_text()
    # Count: original panel.html closing </script> tags must be intact,
    # the payload-embedded one must have been escaped to <\/script>.
    assert "got <\\/script> in payload" in html, (
        "</script> in event payload must be escaped to <\\/script>"
    )
    # Round-trip parse still works.
    events = _read_events_from_snapshot(html)
    assert events[0]["payload"]["reason"] == "got </script> in payload"
