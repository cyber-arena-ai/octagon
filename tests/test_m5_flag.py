"""FlagStore unit tests — plant + submit verification (ACCEPTED / OWN /
DUPLICATE / UNKNOWN) and replant invalidation.

FlagStore is the core the containerized flag-service runs; the in-process
GameMgr flag path was removed in v3, so the former real-docker integration
tests here moved to tests/test_flag_service.py (the flag-service container) +
the live isolated run.
"""

from __future__ import annotations

import re

from challenges.interface import Flag
from flag import FlagStore, SubmitResult, new_flag
from flag.store import FlagRecord

FLAG_RE = re.compile(r"FLAG\{[A-Z0-9]{32}\}")


def _plant(fs, team, service, value, store="default", round_id=1):
    """Plant one flag into one store via the group-commit API."""
    fs.commit_group(team, service,
                    {store: FlagRecord(flag=Flag(value=value), handle="h", round_id=round_id)})


def test_new_flag_shape():
    for _ in range(20):
        f = new_flag()
        assert FLAG_RE.fullmatch(f), f"bad shape: {f!r}"


def test_submit_accepts_then_rejects_duplicate():
    fs = FlagStore()
    _plant(fs, "team1", "notes", "FLAG{A}")
    r1 = fs.submit("team2", "FLAG{A}", 1)
    assert r1 == SubmitResult(accepted=True, reason="ACCEPTED",
                              victim_team_id="team1", service="notes",
                              store="default")
    r2 = fs.submit("team2", "FLAG{A}", 1)
    assert r2.accepted is False and r2.reason == "DUPLICATE"


def test_submit_rejects_own_flag():
    fs = FlagStore()
    _plant(fs, "team1", "notes", "FLAG{B}")
    r = fs.submit("team1", "FLAG{B}", 1)
    assert r.accepted is False and r.reason == "OWN"


def test_submit_rejects_unknown_flag():
    fs = FlagStore()
    _plant(fs, "team1", "notes", "FLAG{C}")
    r = fs.submit("team2", "FLAG{NEVER_PLANTED}", 1)
    assert r.accepted is False and r.reason == "UNKNOWN"


def test_replant_invalidates_old_flag():
    """Re-planting on the same (team, service) drops the prior flag from the
    inverted index so it can no longer be submitted — the foundation for
    per-round rotation."""
    fs = FlagStore()
    _plant(fs, "team1", "notes", "FLAG{OLD}")
    _plant(fs, "team1", "notes", "FLAG{NEW}")
    assert fs.submit("team2", "FLAG{OLD}", 1).reason == "UNKNOWN"
    assert fs.submit("team2", "FLAG{NEW}", 1).reason == "ACCEPTED"


def test_submit_different_victims_independent():
    fs = FlagStore()
    _plant(fs, "team1", "notes", "FLAG{A}")
    _plant(fs, "team2", "notes", "FLAG{B}")
    # team3 stealing both is fine — different (victim, service) keys
    assert fs.submit("team3", "FLAG{A}", 1).reason == "ACCEPTED"
    assert fs.submit("team3", "FLAG{B}", 1).reason == "ACCEPTED"
