"""Tests for MainWindow._wait_for_pairing_startup_settled() (Connect class,
client/ui/dialogs/connect.py).

Reported: "the QR code never shows up" / "the pairing code never shows up so
I could not type it on the phone". The connection dialog's Quit/close handlers
used to call _close_active_session() and exit immediately, no matter how
recently the user had clicked "Conectar com QR code" or "Continuar" — killing
Chrome mid-launch, before WhatsApp Web had ever finished loading enough to
produce a QR/pairing code. A first-time Chrome launch here routinely takes
10-25s; someone who clicked the button and gave up a few seconds later was
closing a session that was seconds away from actually showing something.

_wait_for_pairing_startup_settled() gives that in-flight attempt a short,
bounded grace window before the close proceeds — narrowing the race, not
adding an unconditional delay: an attempt that never started, or one that
already produced its QR/code, returns immediately.

Connect is a plain class (no wx.Dialog needed for this method), so it is
exercised directly against a stub main_window.
"""

import threading

import pytest

import ui.dialogs.connect as connect_module
from ui.dialogs.connect import Connect


class _FakeEvent:
    """Stand-in for WebSocketClient._phone_code_event (a real threading.Event
    would also work, but this lets tests control is_set()/wait() precisely)."""

    def __init__(self, already_set=False):
        self._set = already_set
        self.wait_calls = []

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self._set

    def set(self):
        self._set = True


class _FakeWs:
    def __init__(self, already_set=False):
        self._phone_code_event = _FakeEvent(already_set)


class _FakeMainWindow:
    def __init__(self, pairing_in_progress=True, token="sess:hash", ws=None):
        self._pairing_in_progress = pairing_in_progress
        self.token = token
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.wpp_api_key = "api-key"
        self.ws = ws
        self.settings = {"general": {"language": "pt-BR"}}
        self.spoken = []

    def output(self, text, **kwargs):
        self.spoken.append(text)


def _clock(values):
    """A monotonic() stand-in that walks a fixed sequence, then holds."""
    seq = list(values)

    def _now():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _now


class _Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class TestNoPairingInFlight:
    def test_returns_immediately_when_nothing_is_in_progress(self, monkeypatch):
        calls = []
        monkeypatch.setattr(connect_module, "api_get", lambda *a, **kw: calls.append(1))
        mw = _FakeMainWindow(pairing_in_progress=False)
        c = Connect(mw)
        c.connection_mode = "qrcode"

        c._wait_for_pairing_startup_settled()

        assert calls == []


class TestPhoneMode:
    def test_returns_immediately_if_the_code_already_arrived(self):
        ws = _FakeWs(already_set=True)
        mw = _FakeMainWindow(ws=ws)
        c = Connect(mw)
        c.connection_mode = "phone"

        c._wait_for_pairing_startup_settled()

        assert ws._phone_code_event.wait_calls == []

    def test_waits_on_the_real_phone_code_event_when_not_yet_set(self):
        ws = _FakeWs(already_set=False)
        mw = _FakeMainWindow(ws=ws)
        c = Connect(mw)
        c.connection_mode = "phone"

        c._wait_for_pairing_startup_settled()

        assert len(ws._phone_code_event.wait_calls) == 1
        assert ws._phone_code_event.wait_calls[0] > 0

    def test_no_ws_is_a_no_op(self):
        mw = _FakeMainWindow(ws=None)
        c = Connect(mw)
        c.connection_mode = "phone"

        c._wait_for_pairing_startup_settled()  # must not raise


class TestQrCodeMode:
    def test_returns_at_once_when_the_first_poll_already_has_a_qr(self, monkeypatch):
        polls = []

        def fake_get(url, headers=None, timeout=None):
            polls.append(url)
            return _Response(200, {"qrcode": "data:image/png;base64,abc"})

        monkeypatch.setattr(connect_module, "api_get", fake_get)
        monkeypatch.setattr(connect_module.time, "sleep", lambda _s: None)
        mw = _FakeMainWindow()
        c = Connect(mw)
        c.connection_mode = "qrcode"

        c._wait_for_pairing_startup_settled()

        assert len(polls) == 1

    def test_keeps_polling_until_the_qr_appears(self, monkeypatch):
        responses = iter([
            _Response(200, {"status": "INITIALIZING"}),
            _Response(200, {"status": "INITIALIZING"}),
            _Response(200, {"qrcode": "abc"}),
        ])
        monkeypatch.setattr(connect_module, "api_get", lambda *a, **kw: next(responses))
        monkeypatch.setattr(connect_module.time, "sleep", lambda _s: None)
        mw = _FakeMainWindow()
        c = Connect(mw)
        c.connection_mode = "qrcode"

        c._wait_for_pairing_startup_settled()  # must not raise StopIteration

    def test_a_connected_status_also_ends_the_wait(self, monkeypatch):
        monkeypatch.setattr(connect_module, "api_get",
                             lambda *a, **kw: _Response(200, {"status": "CONNECTED"}))
        monkeypatch.setattr(connect_module.time, "sleep", lambda _s: None)
        mw = _FakeMainWindow()
        c = Connect(mw)
        c.connection_mode = "qrcode"

        c._wait_for_pairing_startup_settled()

    def test_no_token_at_all_is_a_no_op(self, monkeypatch):
        calls = []
        monkeypatch.setattr(connect_module, "api_get", lambda *a, **kw: calls.append(1))
        mw = _FakeMainWindow(token="")
        c = Connect(mw)
        c.connection_mode = "qrcode"
        c._last_started_qr_token = ""

        c._wait_for_pairing_startup_settled()

        assert calls == []

    def test_is_bounded_when_no_qr_ever_arrives(self, monkeypatch):
        """Never blocks a genuine quit forever: a pairing attempt that keeps
        failing to produce anything must still let the close proceed."""
        monkeypatch.setattr(connect_module, "api_get",
                             lambda *a, **kw: _Response(200, {"status": "INITIALIZING"}))
        monkeypatch.setattr(connect_module.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            connect_module.time, "monotonic",
            _clock([0] + [i * Connect._PAIRING_STARTUP_POLL_SECONDS for i in range(1, 200)])
        )
        mw = _FakeMainWindow()
        c = Connect(mw)
        c.connection_mode = "qrcode"

        c._wait_for_pairing_startup_settled()  # returns instead of looping forever

    def test_an_exception_on_a_poll_does_not_abort_the_wait(self, monkeypatch):
        calls = {"n": 0}

        def flaky_get(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("boom")
            return _Response(200, {"qrcode": "abc"})

        monkeypatch.setattr(connect_module, "api_get", flaky_get)
        monkeypatch.setattr(connect_module.time, "sleep", lambda _s: None)
        mw = _FakeMainWindow()
        c = Connect(mw)
        c.connection_mode = "qrcode"

        c._wait_for_pairing_startup_settled()

        assert calls["n"] == 2


class TestUnknownMode:
    def test_an_unrecognized_mode_is_a_no_op(self, monkeypatch):
        calls = []
        monkeypatch.setattr(connect_module, "api_get", lambda *a, **kw: calls.append(1))
        mw = _FakeMainWindow()
        c = Connect(mw)
        c.connection_mode = None

        c._wait_for_pairing_startup_settled()

        assert calls == []


class TestWiredIntoTheQuitHandlers:
    """The gate is only useful if the handlers actually call it, before they
    tear down the state it reads."""

    def test_on_quit_from_connect_calls_it_before_clearing_pairing_state(self):
        import inspect
        source = inspect.getsource(Connect.on_quit_from_connect)
        wait_at = source.index("_wait_for_pairing_startup_settled")
        cleared_at = source.index("_pairing_in_progress = False")
        assert wait_at < cleared_at

    def test_on_dialog_close_calls_it_before_clearing_pairing_state(self):
        import inspect
        source = inspect.getsource(Connect.on_dialog_close)
        wait_at = source.index("_wait_for_pairing_startup_settled")
        cleared_at = source.index("_pairing_in_progress = False")
        assert wait_at < cleared_at
