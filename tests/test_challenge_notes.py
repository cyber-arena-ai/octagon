"""NotesChallenge unit tests — exercise the v2 abstract shape against a
stubbed `target.meta["exec_in_container"]` callable. No docker needed.

Wiring into the orchestrator (FlagServer / HealthChecker / etc.) lands
in Stages 3+; this test pins the per-challenge contract in isolation.
"""

from __future__ import annotations

import pytest

from challenges.interface import ObservationStatus, VulboxTarget
from challenges.notes import NotesChallenge


def _stub_exec(replies):
    """Build an exec_in_container that returns canned (rc, output) pairs
    in order. Each entry is matched in command order. Unknown calls return
    (rc=0, "")."""
    calls: list[tuple[str, str]] = []

    def fake(container_name: str, cmd: str) -> tuple[int, str]:
        calls.append((container_name, cmd))
        if replies:
            return replies.pop(0)
        return (0, "")

    return fake, calls


def _target(exec_fn):
    return VulboxTarget(
        host="team1_prod",
        ports={"service": 5000},
        meta={
            "team_id": "team1",
            "exec_in_container": exec_fn,
        },
    )


def test_notes_challenge_composition():
    ch = NotesChallenge()
    assert ch.name == "notes"
    assert ch.vulbox.exposed_ports == {"service": 5000}
    assert ch.functionality_test.required_ports == ("service",)
    assert ch.flag_handler.required_ports == ("service",)
    # Attack-only — no restart.
    assert ch.restart_handler is None


def test_functionality_test_all_pass():
    """process + tcp + protocol pass when pgrep + TCP + LIST all return OK. The
    old `checker` leaf (a bare alias of PROTOCOL) has been dropped."""
    exec_fn, _ = _stub_exec([
        (0, ""),                # PROCESS pgrep
        (0, ""),                # TCP socket-connect probe
        (0, "COUNT 0\n"),       # PROTOCOL LIST reply
    ])
    ch = NotesChallenge()
    result = ch.functionality_test.run(_target(exec_fn))
    assert result.name == "notes"
    assert result.passed is True
    levels = {c.name: c for c in result.children}
    assert set(levels) == {"process", "tcp", "protocol"}
    assert all(c.passed for c in result.children)


def test_functionality_test_protocol_failure_fails_result():
    """If LIST reply doesn't start with COUNT, PROTOCOL fails and so does the
    overall result."""
    exec_fn, _ = _stub_exec([
        (0, ""),                # PROCESS OK
        (0, ""),                # TCP OK
        (0, "GARBAGE\n"),       # PROTOCOL — bad reply
    ])
    ch = NotesChallenge()
    result = ch.functionality_test.run(_target(exec_fn))
    levels = {c.name: c for c in result.children}
    assert levels["protocol"].passed is False
    assert "checker" not in levels
    assert result.passed is False


def test_flag_handler_plant_parses_ok_id():
    """`PUT <flag>` reply is 'OK <id>'; FlagHandler.plant returns <id>."""
    exec_fn, calls = _stub_exec([(0, "OK 7\n")])
    ch = NotesChallenge()
    handle = ch.flag_handler.plant_one(_target(exec_fn), "FLAG{TESTABC}")
    assert handle == "7"
    # Verify the probe was actually sent (one docker-exec call).
    assert len(calls) == 1
    cname, cmd = calls[0]
    assert cname == "team1_prod"
    assert "PUT" in cmd or "FLAG{TESTABC}" in cmd  # flag baked into the python source


def test_flag_handler_plant_raises_on_failure():
    exec_fn, _ = _stub_exec([(1, "ERR\n")])
    ch = NotesChallenge()
    with pytest.raises(RuntimeError, match="plant_flag failed"):
        ch.flag_handler.plant_one(_target(exec_fn), "FLAG{X}")


def test_flag_handler_retrieve_round_trip():
    # retrieve() probes `true` (reachability) then the socket GET.
    exec_fn, _ = _stub_exec([(0, ""), (0, "FLAG{ROUNDTRIP}\n")])
    ch = NotesChallenge()
    obs = ch.flag_handler.retrieve(_target(exec_fn), "3")
    assert obs.status is ObservationStatus.PRESENT
    assert obs.value == "FLAG{ROUNDTRIP}"


def test_flag_handler_retrieve_not_found_on_err():
    # reachable, daemon replies ERR → the flag id is gone.
    exec_fn, _ = _stub_exec([(0, ""), (0, "ERR\n")])
    ch = NotesChallenge()
    assert ch.flag_handler.retrieve(_target(exec_fn), "999").status \
        is ObservationStatus.NOT_FOUND
