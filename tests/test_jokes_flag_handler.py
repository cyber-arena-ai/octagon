"""faust-jokes FlagHandler: retrieve() must be a pure read-only STRUCTURED read —
it re-logs-in the PLANTED account and never POSTs /register (the old bug: a fresh
uuid account per read → unbounded account creation during repeated audits)."""

from __future__ import annotations

import base64
import json

from challenges.interface import ObservationStatus, VulboxTarget
from challenges.jokes import flag_handler as jh

FLAG = "FLAG{jokes-test}"
_READER = {"name": "planted-user", "password": "pw"}


def _handle(reader=_READER, flag=FLAG):
    return base64.b64encode(json.dumps(
        {"flag_id": "fid", "flag": flag, "reader": reader}).encode()).decode()


class _Resp:
    def __init__(self, status=200, text=""):
        self.status_code, self.text = status, text


class _Sess:
    """Fake requests.Session recording every POST URL."""

    def __init__(self, rec, *, login_ok=True, query_status=200, query_text=FLAG):
        self.rec, self.login_ok, self.query_status, self.query_text = (
            rec, login_ok, query_status, query_text)

    def post(self, url, data=None, timeout=None):
        self.rec.append(url)
        if url.endswith("/login"):
            return _Resp(200 if self.login_ok else 401)
        if url.endswith("/profile"):
            return _Resp(self.query_status, self.query_text)
        return _Resp(200)

    def close(self):
        pass


def _target():
    return VulboxTarget(host="1.2.3.4", ports={"service": 5000}, meta={"team_id": "t"})


def _wire(monkeypatch, sess):
    rec = sess.rec
    monkeypatch.setattr(jh._net, "base_url", lambda target: "http://x")
    monkeypatch.setattr(jh.requests, "Session", lambda: sess)
    # module-level requests.post is ONLY used by /register (plant). Fail if the
    # read path ever calls it.
    def _no_register(url, data=None, timeout=None):
        rec.append(url)
        raise AssertionError(f"read path must not POST {url}")
    monkeypatch.setattr(jh.requests, "post", _no_register)
    return rec


def test_retrieve_present_never_registers(monkeypatch):
    rec = []
    _wire(monkeypatch, _Sess(rec, query_text=f"...{FLAG}..."))
    o = jh.JokesFlagHandler().retrieve(_target(), _handle(), expected=FLAG)
    assert o.status is ObservationStatus.PRESENT and o.value == FLAG
    assert not any(u.endswith("/register") for u in rec)   # read-only
    assert rec == ["http://x/login", "http://x/profile"]


def test_retrieve_value_defaults_expected_to_handle_flag(monkeypatch):
    rec = []
    _wire(monkeypatch, _Sess(rec, query_text=FLAG))
    o = jh.JokesFlagHandler().retrieve(_target(), _handle())
    assert o.status is ObservationStatus.PRESENT and o.value == FLAG
    assert not any(u.endswith("/register") for u in rec)


def test_retrieve_not_found(monkeypatch):
    rec = []
    _wire(monkeypatch, _Sess(rec, query_text="no flag here"))
    o = jh.JokesFlagHandler().retrieve(_target(), _handle(), expected=FLAG)
    assert o.status is ObservationStatus.NOT_FOUND


def test_retrieve_login_failure_is_error_not_missing(monkeypatch):
    rec = []
    _wire(monkeypatch, _Sess(rec, login_ok=False))
    o = jh.JokesFlagHandler().retrieve(_target(), _handle(), expected=FLAG)
    assert o.status is ObservationStatus.ERROR


def test_retrieve_query_http_error_is_error(monkeypatch):
    rec = []
    _wire(monkeypatch, _Sess(rec, query_status=500))
    o = jh.JokesFlagHandler().retrieve(_target(), _handle(), expected=FLAG)
    assert o.status is ObservationStatus.ERROR


def test_retrieve_bad_handle_is_error(monkeypatch):
    _wire(monkeypatch, _Sess([]))
    o = jh.JokesFlagHandler().retrieve(_target(), "!!not-b64!!", expected=FLAG)
    assert o.status is ObservationStatus.ERROR


def test_plant_stores_reader_creds_in_handle(monkeypatch):
    # plant registers ONE account and must persist its creds in the handle.
    captured = {}

    def fake_register(url, data=None, timeout=None):
        assert url.endswith("/register")
        captured["creds"] = data
        return _Resp(200)

    monkeypatch.setattr(jh._net, "base_url", lambda target: "http://x")
    monkeypatch.setattr(jh.requests, "post", fake_register)
    monkeypatch.setattr(jh.requests, "Session", lambda: _Sess([], query_text=FLAG))

    handle = jh.JokesFlagHandler().plant_one(_target(), FLAG)
    st = json.loads(base64.b64decode(handle))
    assert st["flag"] == FLAG and st["flag_id"]
    assert st["reader"] == captured["creds"]          # planted creds stored for reuse
    assert set(st["reader"]) == {"name", "password"}
