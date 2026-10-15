"""new_run_dir namespacing — two jobs starting the SAME second on one host must
get DISTINCT run dirs when they carry different docker namespaces (single-host
concurrency). Without this they share runs/<ts>_<challenge>/ and clobber each
other's compose/*.yaml, events, and artifacts."""

from __future__ import annotations

import re

from gamemgr.events import new_run_dir


def test_distinct_dir_per_namespace_same_second(tmp_path):
    # Same label, back-to-back (same UTC second) but different ns → different dirs.
    a = new_run_dir(tmp_path, label="nautilus-ico", ns="s0")
    b = new_run_dir(tmp_path, label="nautilus-ico", ns="s1")
    assert a != b
    assert a.name.endswith("_s0_nautilus-ico")
    assert b.name.endswith("_s1_nautilus-ico")
    assert a.is_dir() and b.is_dir()


def test_serial_run_dir_unchanged(tmp_path):
    # No ns (serial, the default) → name is exactly <ts>_<label>, as before —
    # no namespace segment injected.
    p = new_run_dir(tmp_path, label="nautilus-ico")
    assert re.fullmatch(r"\d{8}T\d{6}Z_nautilus-ico", p.name)


def test_ns_without_label(tmp_path):
    p = new_run_dir(tmp_path, ns="s0")
    assert re.fullmatch(r"\d{8}T\d{6}Z_s0", p.name)


def test_collision_uniquifies_same_second(tmp_path, monkeypatch):
    # Two SERIAL runs (ns="") of the same challenge in the same UTC second must
    # NOT collapse into one dir — the earlier bug (mkdir exist_ok reused the dir).
    # The first keeps the clean name; later ones get -2/-3/… suffixes.
    monkeypatch.setattr("gamemgr.events.time.strftime",
                        lambda *a, **k: "20260101T000000Z")
    a = new_run_dir(tmp_path, label="nautilus-ico")
    b = new_run_dir(tmp_path, label="nautilus-ico")
    c = new_run_dir(tmp_path, label="nautilus-ico")
    assert a.name == "20260101T000000Z_nautilus-ico"
    assert b.name == "20260101T000000Z_nautilus-ico-2"
    assert c.name == "20260101T000000Z_nautilus-ico-3"
    assert len({a, b, c}) == 3 and a.is_dir() and b.is_dir() and c.is_dir()


def test_collision_uniquifies_no_suffix(tmp_path, monkeypatch):
    # Even with neither ns nor label (any non-namespaced caller), same-second
    # runs stay distinct.
    monkeypatch.setattr("gamemgr.events.time.strftime",
                        lambda *a, **k: "20260101T000000Z")
    a = new_run_dir(tmp_path)
    b = new_run_dir(tmp_path)
    assert a.name == "20260101T000000Z" and b.name == "20260101T000000Z-2"
