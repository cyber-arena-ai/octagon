"""Facility port + adapter unit tests.

Pins the boundary contract the Local* adapters implement (and that a
future HTTP adapter must match): status passthrough, event passthrough
to a Timeline, and identity-addressed exec with a hang deadline.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from facility import (
    EventSink,
    ExecPort,
    ExecResult,
    Facility,
    FacilityContext,
    LocalEventSink,
    LocalExecPort,
    LocalStatusPort,
    StatusPort,
)
from gamemgr.events import EventType
from gamemgr.timeline import Timeline


# ── adapters satisfy their Protocols (structural) ──────────────────────


def test_local_adapters_satisfy_protocols():
    assert isinstance(LocalStatusPort(lambda: {}), StatusPort)
    assert isinstance(LocalEventSink(Timeline()), EventSink)
    assert isinstance(
        LocalExecPort(container_resolver=lambda t, r: "c", docker_exec=lambda c, cmd: (0, "")),
        ExecPort,
    )


# ── LocalStatusPort ────────────────────────────────────────────────────


def test_status_port_passes_through():
    port = LocalStatusPort(lambda: {"round": 3, "phase": "ATTACK"})
    assert port.status() == {"round": 3, "phase": "ATTACK"}


# ── LocalEventSink ─────────────────────────────────────────────────────


def test_event_sink_records_onto_timeline():
    tl = Timeline()
    sink = LocalEventSink(tl)
    ev = sink.record(
        EventType.FLAG_PLANTED, team_id="team1", round_id=2, payload={"service": "notes"}
    )
    hist = tl.history()
    assert len(hist) == 1
    assert hist[0] is ev
    assert hist[0].type == EventType.FLAG_PLANTED.value
    assert hist[0].team_id == "team1"
    assert hist[0].round_id == 2
    assert hist[0].payload == {"service": "notes"}


# ── LocalExecPort ──────────────────────────────────────────────────────


def test_exec_port_resolves_identity_then_runs():
    seen: list[tuple[str, str]] = []

    def resolver(team_id: str, role: str) -> str:
        assert (team_id, role) == ("team1", "prod")
        return "team1_prod"

    def docker_exec(container: str, cmd: str):
        seen.append((container, cmd))
        return 0, "output-text"

    port = LocalExecPort(container_resolver=resolver, docker_exec=docker_exec)
    result = port.exec("team1", "prod", "echo hi")

    assert result == ExecResult(exit_code=0, stdout="output-text", stderr="", timed_out=False)
    # Resolver output (not the identity) is what reaches the docker layer.
    assert seen == [("team1_prod", "echo hi")]


def test_exec_port_times_out_without_blocking_caller():
    def docker_exec(container: str, cmd: str):
        time.sleep(5)  # longer than the deadline below
        return 0, "never returned"

    port = LocalExecPort(container_resolver=lambda t, r: "c", docker_exec=docker_exec)
    start = time.monotonic()
    result = port.exec("team1", "prod", "sleep", timeout_s=0.1)
    elapsed = time.monotonic() - start

    assert result.timed_out is True
    assert result.exit_code == -1
    assert "timed out" in result.stderr
    # Caller released near the deadline, not after the full 5s sleep.
    assert elapsed < 2.0


def test_exec_port_propagates_docker_error():
    def docker_exec(container: str, cmd: str):
        raise RuntimeError("docker boom")

    port = LocalExecPort(container_resolver=lambda t, r: "c", docker_exec=docker_exec)
    with pytest.raises(RuntimeError, match="docker boom"):
        port.exec("team1", "prod", "whatever")


# ── FacilityContext ────────────────────────────────────────────────────


def test_facility_context_exec_optional():
    ctx = FacilityContext(status=LocalStatusPort(lambda: {}), events=LocalEventSink(Timeline()))
    assert ctx.exec is None


# ── Facility ABC ───────────────────────────────────────────────────────


class _NoopFacility(Facility):
    @property
    def name(self) -> str:
        return "noop"


def test_facility_default_lifecycle_is_noop():
    f = _NoopFacility()
    assert f.name == "noop"
    # start/stop default to awaitable no-ops.
    assert asyncio.run(f.start()) is None
    assert asyncio.run(f.stop()) is None


def test_concrete_facilities_expose_names():
    # Imported lazily so this test documents the wiring without pulling
    # docker at module import time.
    from facility import LocalStatusPort as _S
    from flag import FlagServer
    from health.poller import HealthPoller

    ctx = FacilityContext(status=_S(lambda: {}), events=LocalEventSink(Timeline()))
    fs = FlagServer(ctx, challenge_by_owner={}, target_factory=lambda *_: None)
    assert fs.name == "flag"

    hp = HealthPoller(checker=None, targets=[])
    assert hp.name == "health"
