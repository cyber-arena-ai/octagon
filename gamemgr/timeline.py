"""Timeline — append-only event stream for panel / file observation.

Timeline is intentionally *not* a control-flow mechanism: it has no
subscribers that drive internal logic. Anything that needs to act on
an event calls the relevant method on GameMgr / Orchestrator /
HealthPoller directly.

Two sink kinds:
  * sync sinks — plain `Callable[[Event], None]`; invoked synchronously
    inside `record()`. Used by FileSink so the on-disk JSONL preserves
    a single monotonic timeline.
  * stream queues — `asyncio.Queue[Event]` returned by `attach_stream()`;
    `record()` does a non-blocking `put_nowait`. Used by WebSocket
    consumers in api.py. A slow client can never back-pressure the game.

`record()` is synchronous and returns immediately. Callers don't await.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Callable, TextIO

from .events import Event, EventType, new_event

log = logging.getLogger(__name__)

Sink = Callable[[Event], None]


class Timeline:
    def __init__(self) -> None:
        self._history: list[Event] = []
        self._sinks: list[Sink] = []
        self._streams: list[asyncio.Queue[Event]] = []
        self._files: list[TextIO] = []  # opened by attach_file_sink; closed by close()

    def record(
        self,
        type_: str | EventType,
        *,
        team_id: str | None = None,
        round_id: int = 0,
        phase: str = "",
        payload: dict | None = None,
    ) -> Event:
        """Append one event to history + fan out to sinks + stream queues.

        Synchronous and non-blocking. A sink raising is logged and ignored;
        a stream queue being full (only possible when caller asked for a
        bounded queue) drops the event with a warning rather than blocking
        the publisher.
        """
        ev = new_event(
            type_,
            team_id=team_id,
            round_id=round_id,
            phase=phase,
            payload=payload,
        )
        self._history.append(ev)
        for sink in self._sinks:
            try:
                sink(ev)
            except Exception:
                # Log the event itself so a sink failure (e.g. the events.jsonl
                # file sink erroring) leaves a RECOVERABLE record in the orche log,
                # not a bare "dropping" line with no idea what was lost (Finding 4).
                # The log is runner-local, same as events.jsonl, so this exposes
                # nothing events.jsonl wouldn't.
                try:
                    detail = json.dumps(asdict(ev))
                except Exception:
                    detail = f"{ev.type} id={getattr(ev, 'id', '?')}"
                # Bound the line: a pathologically large payload shouldn't blow up
                # the log (and this only fires on a genuine sink failure anyway).
                log.exception("timeline sink failed; event not persisted by it: %s",
                              detail[:4000])
        for q in self._streams:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                log.warning(
                    "timeline stream queue full (maxsize=%d); dropping event %s",
                    q.maxsize, ev.type,
                )
        return ev

    def attach_sink(self, sink: Sink) -> None:
        """Register a sync callback invoked on every record()."""
        self._sinks.append(sink)

    def attach_file_sink(self, path: Path) -> None:
        """Open `path` in append mode and write one JSON line per event.

        File is line-buffered; closed on `Timeline.close()`. Parent dir
        is created if missing.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a", buffering=1)
        self._files.append(f)
        self._sinks.append(
            lambda ev: f.write(json.dumps(asdict(ev)) + "\n")
        )

    def attach_stream(self, *, maxsize: int = 0) -> asyncio.Queue[Event]:
        """Return a new queue that receives every subsequent record().

        `maxsize=0` (default) means unbounded — a slow consumer accumulates
        memory but never loses events or blocks. Set a bound when the
        consumer is known-untrusted (e.g. external WebSocket).
        """
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._streams.append(q)
        return q

    def detach_stream(self, q: asyncio.Queue[Event]) -> None:
        try:
            self._streams.remove(q)
        except ValueError:
            pass

    def history(self) -> list[Event]:
        return list(self._history)

    def close(self) -> None:
        """Flush + close any file sinks opened via attach_file_sink()."""
        for f in self._files:
            try:
                f.flush()
                f.close()
            except Exception:
                pass
        self._files.clear()
