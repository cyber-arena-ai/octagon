"""Unit tests for gamemgr.timeline.Timeline.

Timeline replaces the legacy EventBus pub-sub model. These tests pin its
core invariants:
  * record() is synchronous and append-only
  * sinks see every recorded event in order
  * stream queues receive events via put_nowait (no back-pressure)
  * a failing sink doesn't break sibling sinks or the stream fan-out
  * detach_stream stops further deliveries
  * close() flushes file sinks
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from gamemgr.events import EventType
from gamemgr.timeline import Timeline


def test_record_appends_to_history_with_correct_fields():
    tl = Timeline()
    ev = tl.record(
        EventType.FLAG_PLANTED,
        team_id="team1",
        round_id=3,
        phase="defense",
        payload={"service": "notes"},
    )
    assert ev.type == EventType.FLAG_PLANTED.value
    assert ev.team_id == "team1"
    assert ev.round_id == 3
    assert ev.phase == "defense"
    assert ev.payload == {"service": "notes"}
    assert tl.history() == [ev]


def test_record_preserves_insertion_order():
    tl = Timeline()
    e1 = tl.record(EventType.ROUND_START, round_id=1)
    e2 = tl.record(EventType.PHASE_CHANGE, round_id=1, phase="defense")
    e3 = tl.record(EventType.PHASE_CHANGE, round_id=1, phase="attack")
    assert tl.history() == [e1, e2, e3]


def test_sync_sink_invoked_in_order():
    tl = Timeline()
    seen: list[str] = []
    tl.attach_sink(lambda ev: seen.append(ev.type))
    tl.record(EventType.ROUND_START, round_id=1)
    tl.record(EventType.ROUND_STOP, round_id=1)
    assert seen == [EventType.ROUND_START.value, EventType.ROUND_STOP.value]


def test_failing_sink_does_not_break_others_or_stream(caplog):
    tl = Timeline()

    def boom(_):
        raise RuntimeError("sink exploded")

    survivor_calls: list[str] = []
    tl.attach_sink(boom)
    tl.attach_sink(lambda ev: survivor_calls.append(ev.type))

    async def _check():
        q = tl.attach_stream()
        tl.record(EventType.FLAG_PLANTED, team_id="t1")
        ev = await asyncio.wait_for(q.get(), timeout=0.1)
        assert ev.type == EventType.FLAG_PLANTED.value

    asyncio.run(_check())
    assert survivor_calls == [EventType.FLAG_PLANTED.value]


def test_failing_sink_logs_recoverable_event_content(caplog):
    """Finding 4: a sink failure (e.g. the events.jsonl file sink erroring) must
    leave a RECOVERABLE record — the event's own content — in the log, not just a
    bare 'dropping' line with no idea what was lost."""
    import logging

    tl = Timeline()

    def boom(_):
        raise RuntimeError("disk full")

    tl.attach_sink(boom)
    with caplog.at_level(logging.ERROR, logger="gamemgr.timeline"):
        tl.record(EventType.FLAG_PLANTED, team_id="t1", payload={"service": "notes"})

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "FLAG_PLANTED" in joined          # know WHICH event was lost
    assert "notes" in joined                 # and its content, so it's recoverable


def test_stream_receives_events_via_put_nowait():
    tl = Timeline()

    async def _run():
        q = tl.attach_stream()
        tl.record(EventType.AGENT_INSTALLED, team_id="t1")
        tl.record(EventType.AGENT_READY, team_id="t1")
        e1 = await asyncio.wait_for(q.get(), timeout=0.1)
        e2 = await asyncio.wait_for(q.get(), timeout=0.1)
        assert e1.type == EventType.AGENT_INSTALLED.value
        assert e2.type == EventType.AGENT_READY.value

    asyncio.run(_run())


def test_detach_stream_stops_further_delivery():
    tl = Timeline()

    async def _run():
        q = tl.attach_stream()
        tl.record(EventType.ROUND_START, round_id=1)
        first = await asyncio.wait_for(q.get(), timeout=0.1)
        assert first.type == EventType.ROUND_START.value

        tl.detach_stream(q)
        tl.record(EventType.ROUND_STOP, round_id=1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.05)

    asyncio.run(_run())


def test_multiple_streams_each_get_every_event():
    tl = Timeline()

    async def _run():
        q1 = tl.attach_stream()
        q2 = tl.attach_stream()
        tl.record(EventType.FLAG_PLANTED, team_id="t1")
        e1 = await asyncio.wait_for(q1.get(), timeout=0.1)
        e2 = await asyncio.wait_for(q2.get(), timeout=0.1)
        assert e1.id == e2.id  # same event instance fanned out

    asyncio.run(_run())


def test_bounded_stream_drops_events_when_full(caplog):
    tl = Timeline()

    async def _run():
        q = tl.attach_stream(maxsize=1)
        tl.record(EventType.ROUND_START, round_id=1)
        # second record should drop (queue full) without raising
        tl.record(EventType.ROUND_STOP, round_id=1)
        got = await asyncio.wait_for(q.get(), timeout=0.1)
        assert got.type == EventType.ROUND_START.value
        # nothing else queued
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.05)

    asyncio.run(_run())
    assert any("stream queue full" in rec.message for rec in caplog.records)


def test_file_sink_writes_jsonl(tmp_path: Path):
    tl = Timeline()
    log_path = tmp_path / "subdir" / "events.jsonl"  # subdir auto-created
    tl.attach_file_sink(log_path)
    tl.record(EventType.ROUND_START, round_id=1)
    tl.record(EventType.PHASE_CHANGE, round_id=1, phase="defense")
    tl.close()

    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    rec0 = json.loads(lines[0])
    rec1 = json.loads(lines[1])
    assert rec0["type"] == EventType.ROUND_START.value
    assert rec1["type"] == EventType.PHASE_CHANGE.value
    assert rec1["phase"] == "defense"


def test_history_returns_snapshot_not_live_reference():
    tl = Timeline()
    tl.record(EventType.ROUND_START, round_id=1)
    snap = tl.history()
    tl.record(EventType.ROUND_STOP, round_id=1)
    assert len(snap) == 1  # snapshot wasn't mutated by the later record
    assert len(tl.history()) == 2
