"""reversaar FlagHandler: retrieve() is a pure read-only STRUCTURED observation;
repair() is the separated credential re-assert. An audit may call retrieve()
repeatedly, so it must never write; only repair (invoked on AUTH_REJECTED) writes.

Part A drives the REAL `_client.login()` against mocked HTTP responses to pin the
auth classification to reversaar's actual protocol (main.c): 401 → AuthRejected,
200+Session cookie → token, everything else → ClientError. Faking AuthRejected
directly would hide a mis-mapping that silently breaks the #29 self-heal.
"""

from __future__ import annotations

import httpx
import pytest

from challenges.interface import ObservationStatus, RepairResult, VulboxTarget
from challenges.reversaar import _client, flag_handler as fh


# ── Part A: real _client.login() protocol classification ──────────────────────

def _login_via(handler):
    c = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        return _client.login(c, "1.2.3.4", "vault", "pw")
    finally:
        c.close()


def test_login_401_raises_auth_rejected():
    # Stored-hash mismatch → api_login err(401) (main.c:183).
    with pytest.raises(_client.AuthRejected):
        _login_via(lambda req: httpx.Response(401, text="Unauthorized"))


def test_login_200_with_session_cookie_returns_token():
    def h(req):
        return httpx.Response(200, headers=[("set-cookie", "Session=tok123; SameSite=strict")])
    assert _login_via(h) == "tok123"


def test_login_200_without_cookie_is_client_error_not_auth_rejected():
    err = None
    try:
        _login_via(lambda req: httpx.Response(200, json={"ok": True}))
    except _client.ClientError as e:
        err = e
    assert err is not None and not isinstance(err, _client.AuthRejected)


@pytest.mark.parametrize("code", [400, 403, 500])
def test_login_other_non200_is_client_error_not_auth_rejected(code):
    err = None
    try:
        _login_via(lambda req: httpx.Response(code, text="nope"))
    except _client.ClientError as e:
        err = e
    assert err is not None and not isinstance(err, _client.AuthRejected)


def test_login_transport_error_propagates_as_httpx_error():
    def boom(req):
        raise httpx.ConnectError("unreachable")
    with pytest.raises(httpx.HTTPError):
        _login_via(boom)


# ── Part B: retrieve() is read-only; structured status matrix ─────────────────

FLAG = "FLAG{reversaar-test}"


class _Rec:
    """Records every exec_in_container call so we can assert retrieve() never
    writes (only repair does)."""

    def __init__(self):
        self.calls = []

    def __call__(self, host, cmd):
        self.calls.append((host, cmd))
        return (0, "")


def _target(rec):
    return VulboxTarget(host="1.2.3.4", ports={"service": 7331},
                        meta={"team_id": "team1", "exec_in_container": rec})


def _handle():
    return fh._pack({"user": fh.FLAG_USER, "idx": 3, "flag": FLAG})


class _DummyCtx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def _patch_client(monkeypatch, *, login, get_array):
    monkeypatch.setattr(_client, "new_client", lambda: _DummyCtx())
    monkeypatch.setattr(_client, "login", login)
    monkeypatch.setattr(_client, "get_array", get_array)


def test_retrieve_present_and_zero_writes(monkeypatch):
    rec = _Rec()
    _patch_client(monkeypatch,
                  login=lambda c, ip, u, p: "tok",
                  get_array=lambda c, ip, t, idx: FLAG.encode())
    h = fh.ReversaarFlagHandler()
    obs = h.retrieve(_target(rec), _handle(), expected=FLAG)
    assert obs.status is ObservationStatus.PRESENT and obs.value == FLAG
    # read path made NO container writes
    assert rec.calls == []
    # expected defaults to the flag packed in the handle — still read-only
    assert h.retrieve(_target(rec), _handle()).value == FLAG
    assert rec.calls == []


def test_retrieve_auth_rejected_no_write(monkeypatch):
    rec = _Rec()

    def login(c, ip, u, p):
        raise _client.AuthRejected("401")

    _patch_client(monkeypatch, login=login,
                  get_array=lambda *a: pytest.fail("get_array should not run"))
    h = fh.ReversaarFlagHandler()
    obs = h.retrieve(_target(rec), _handle(), expected=FLAG)
    assert obs.status is ObservationStatus.AUTH_REJECTED
    assert obs.value is None
    assert rec.calls == []                       # observation never repairs


def test_retrieve_error_on_transport(monkeypatch):
    rec = _Rec()

    def login(c, ip, u, p):
        raise httpx.ConnectError("unreachable")

    _patch_client(monkeypatch, login=login, get_array=lambda *a: b"")
    h = fh.ReversaarFlagHandler()
    obs = h.retrieve(_target(rec), _handle(), expected=FLAG)
    assert obs.status is ObservationStatus.ERROR
    assert rec.calls == []


def test_retrieve_not_found_when_flag_absent(monkeypatch):
    rec = _Rec()
    _patch_client(monkeypatch,
                  login=lambda *a: "tok",
                  get_array=lambda c, ip, t, idx: b"some other blob")
    h = fh.ReversaarFlagHandler()
    obs = h.retrieve(_target(rec), _handle(), expected=FLAG)
    assert obs.status is ObservationStatus.NOT_FOUND
    assert rec.calls == []


def test_retrieve_bad_handle_is_error(monkeypatch):
    rec = _Rec()
    _patch_client(monkeypatch, login=lambda *a: "tok", get_array=lambda *a: b"")
    h = fh.ReversaarFlagHandler()
    assert h.retrieve(_target(rec), "!!!not-base64!!!", expected=FLAG).status is ObservationStatus.ERROR


# ── Part C: repair() is the only writer ───────────────────────────────────────

def test_repair_reasserts_password_and_returns_repaired():
    rec = _Rec()
    h = fh.ReversaarFlagHandler()
    assert h.repair(_target(rec), _handle()) is RepairResult.REPAIRED
    assert len(rec.calls) == 1
    _host, cmd = rec.calls[0]
    assert "password" in cmd            # re-writes vault's password file
    assert ": > " not in cmd            # but does NOT clear/truncate the array


def test_repair_failure_returns_failed_no_raise():
    def boom(host, cmd):
        raise RuntimeError("docker exec died")
    h = fh.ReversaarFlagHandler()
    assert h.repair(_target(boom), _handle()) is RepairResult.FAILED
