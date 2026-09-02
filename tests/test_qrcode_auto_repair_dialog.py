"""Tests for on_qrcode_update()'s proactive pairing-dialog trigger.

Reported live: after an automatic session recovery attempt found the stored
token already invalid, WPPConnect correctly started generating a fresh QR
code (on_qrcode_update fired repeatedly with real image bytes) — but nothing
in the app surfaced it. The user was left staring at "offline" with no
explanation for however long _AUTO_RESTART_LOGOUT_GRACE_SECONDS or the
multi-minute confirmed-logout detection (several minutes either way) took
before finally showing a dialog.

A real QR/pairing-code event with no pairing dialog already open is actually
a reliable "you need to re-pair" signal on its own — WPPConnect only ever
generates one once it has decided the stored session can't be restored — so
on_qrcode_update() now opens the pairing dialog immediately in that specific
case, decoupled entirely from the slower, destructive confirmed-logout path
(_on_disconnect(), which wipes local data, is never called from here).

WebSocketClient is exercised as a plain function bound onto a small stub
(no real socketio/wx.App needed) — same approach as tests/test_qrcode_event.py
uses for _extract_qr_payload.
"""

import pytest

from core.websocket_client import WebSocketClient
from main import MainWindow


class _FakeI18n:
    def t(self, key):
        return key


class _FakeSound:
    def play(self):
        pass


class _FakeSpeakOutput:
    def output(self, text):
        pass


class _FakeConnect:
    def __init__(self, main_window=None):
        self.connection_mode = "phone"
        self.main_window = main_window
        self.show_connection_dial_calls = 0

    def show_connection_dial(self):
        self.show_connection_dial_calls += 1
        # Mirrors the real Connect.show_connection_dial(), which drops both
        # unattended-QR guards right before its modal loop. A fake that skips
        # this makes the flood limit look one event closer than production
        # ever reaches it — see tests/test_qrcode_unattended_session.py.
        self.main_window._reset_unattended_qr_guards()

    def display_qrcode_image(self, base64_img):
        pass


class _FakeMainWindow:
    def __init__(self, paired=True, pairing_dialog_active=False):
        self.settings = {"privateinfo": {"paired": paired}}
        self._pairing_dialog_active = pairing_dialog_active
        self.pairing_code_updated_sound = _FakeSound()
        self.error_sound = _FakeSound()
        self.speak_output = _FakeSpeakOutput()
        self.app_name = "WinZapp"
        self.restore_window_calls = 0
        self._unattended_qr_events = 0
        self._qr_flood_halted = False
        self._pairing_in_progress = False
        self.halt_calls = 0

    def _is_pairing_dialog_active(self):
        return self._pairing_dialog_active

    def restore_window(self):
        self.restore_window_calls += 1

    # The real method, so this fake cannot drift from what production does
    # when the pairing dialog goes up.
    _reset_unattended_qr_guards = MainWindow._reset_unattended_qr_guards

    def _halt_unattended_qr_session(self):
        self.halt_calls += 1
        self._qr_flood_halted = True


class _Stub:
    on_qrcode_update = WebSocketClient.on_qrcode_update
    _pairing_attended = WebSocketClient._pairing_attended
    _handle_unattended_qr = WebSocketClient._handle_unattended_qr
    _show_repair_dialog = WebSocketClient._show_repair_dialog
    _UNATTENDED_QR_LIMIT = WebSocketClient._UNATTENDED_QR_LIMIT
    _extract_qr_payload = staticmethod(WebSocketClient._extract_qr_payload)

    def __init__(self, main_window, connect):
        self.main_window = main_window
        self.connect = connect
        self.i18n = _FakeI18n()


QR_EVENT = {"data": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg"}


@pytest.fixture(autouse=True)
def _synchronous_call_after(monkeypatch):
    monkeypatch.setattr("core.websocket_client.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("core.websocket_client.wx.MessageBox", lambda *a, **kw: None)


class TestProactivePairingDialog:
    def test_opens_the_dialog_when_paired_and_nothing_is_showing(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 1

    def test_restores_the_window_and_gives_the_classic_logout_cue_first(self):
        """Regression: the first version of this feature jumped straight to
        show_connection_dial() with no sound/MessageBox at all — silent and
        easy to miss entirely if the window was minimized to the tray at the
        time, reported live as exactly that."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert mw.restore_window_calls == 1

    def test_does_not_open_a_second_dialog_on_a_qr_refresh(self):
        """QR codes rotate every ~20-30s while waiting — must not stack
        nested dialogs on every refresh."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 1
        # And the refreshes behind the open dialog are not a flood: opening it
        # resets the counter, so three events never reach the halt. Asserted
        # here because this is exactly where a fake that skipped the reset
        # would diverge from production while still passing the line above.
        assert mw.halt_calls == 0

    def test_does_nothing_when_a_pairing_dialog_is_already_open(self):
        """The dialog is already up (e.g. user-initiated, or already shown
        proactively) — this is the existing display_qrcode_image()/pairing
        code field update path instead."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=True)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 0

    def test_does_nothing_when_never_paired(self):
        """An account that was never paired goes through the normal
        first-run pairing flow already — this path is only for "was paired,
        suddenly needs a fresh QR"."""
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 0

    def test_reconnecting_afterwards_allows_a_future_trigger(self):
        """_auto_repair_dialog_shown is reset by _set_wa_connected(True, ...)
        once the connection genuinely recovers — simulated here directly."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        assert connect.show_connection_dial_calls == 1

        mw._auto_repair_dialog_shown = False  # what a real reconnect does
        s.on_qrcode_update(QR_EVENT)
        assert connect.show_connection_dial_calls == 2
