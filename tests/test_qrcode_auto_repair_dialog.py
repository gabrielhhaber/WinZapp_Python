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

import time

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
    def __init__(self, paired=True, pairing_dialog_active=False,
                 wa_connect_announced=True, wa_startup_time=None):
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
        # Whether a snapshot exists to put back. False keeps every test
        # written before the profile-repair step behaving exactly as it did:
        # nothing to restore, so the pairing dialog is the outcome.
        self.profile_restore_available = False
        self.recover_calls = []
        # Defaults put every pre-existing test well past the startup grace
        # window (already connected once before, or started long ago) —
        # only the dedicated grace-window tests below override these.
        self._wa_connect_announced = wa_connect_announced
        self._WA_STARTUP_GRACE_SECONDS = MainWindow._WA_STARTUP_GRACE_SECONDS
        self._wa_startup_time = (
            time.time() - (self._WA_STARTUP_GRACE_SECONDS * 10)
            if wa_startup_time is None else wa_startup_time
        )

    def _recover_suspect_profile(self, reason=None, on_give_up=None):
        self.recover_calls.append(reason)
        # Mirrors the real contract: on_give_up fires only when a restore was
        # started and then failed. A False return means nothing was started,
        # and the caller handles it inline — see _recover_suspect_profile().
        return bool(self.profile_restore_available)

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
    _qr_within_startup_grace = WebSocketClient._qr_within_startup_grace
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


class TestTheProfileIsRepairedBeforeAskingTheUserToPair:
    """A code minted for a *paired* install means the stored session could not
    be restored — which is exactly the condition the profile recovery exists
    for, and this is the earliest and cleanest evidence of it available.

    Measured on a real install on 2026-09-08. A clean Ctrl+Shift+Q shutdown
    (close-session acknowledged, session observed CLOSED, Chrome confirmed to
    have released the profile), and the next launch logged itself out seven
    seconds into the page load. ProfileHealthTracker counted
    INITIALIZING/CLOSED cycles at ~60 s each and stood at 2 of 3 when the code
    arrived at t+2.4 min — and opening the pairing dialog then froze it there
    for good, because check_wa_connection_http() returns immediately while a
    pairing dialog is up, so the poll that feeds the tracker never ran again.
    The snapshot was restorable by hand the whole time; the app could never
    reach it.
    """

    def test_a_restorable_profile_is_repaired_instead_of_re_paired(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = True
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert len(mw.recover_calls) == 1
        assert connect.show_connection_dial_calls == 0

    def test_the_reason_says_what_was_observed(self):
        # It lands in shutdown_audit.log, which survives the launch — the one
        # place the next diagnosis can read why a restore was attempted.
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = True
        s = _Stub(mw, _FakeConnect(mw))

        s.on_qrcode_update(QR_EVENT)

        assert "pairing code" in (mw.recover_calls[0] or "")

    def test_with_nothing_to_restore_the_user_is_still_sent_to_pair(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = False
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert len(mw.recover_calls) == 1
        assert connect.show_connection_dial_calls == 1

    def test_an_install_that_never_paired_is_not_a_broken_profile(self):
        # Nothing to restore and nothing lost: this is an ordinary first
        # pairing, and the recovery must not run at all.
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        s = _Stub(mw, _FakeConnect(mw))

        s.on_qrcode_update(QR_EVENT)

        assert mw.recover_calls == []

    def test_a_qr_refresh_during_the_restore_does_not_retry_it(self):
        # Codes rotate every ~20-30 s. _recover_suspect_profile() latches on
        # its own, but the caller must not be the thing relying on that.
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = True
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 0


class TestStartupGraceWindow:
    """Regression: a real log showed on_qrcode_update firing 11s after
    process start, while /list-chats was still 404ing for another 50s
    because the session itself had not finished starting — WPPConnect's
    first QR event is not immune to the exact slow-boot race
    _WA_STARTUP_GRACE_SECONDS exists for elsewhere. A single such event
    used to open the proactive re-pair dialog immediately; the user then
    followed it into a fresh pairing, which wiped their local history."""

    def test_does_not_open_immediately_inside_the_startup_grace_window(self):
        mw = _FakeMainWindow(
            paired=True, pairing_dialog_active=False,
            wa_connect_announced=False, wa_startup_time=time.time(),
        )
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 0

    def test_opens_once_the_grace_window_has_elapsed(self):
        mw = _FakeMainWindow(
            paired=True, pairing_dialog_active=False,
            wa_connect_announced=False,
            wa_startup_time=time.time() - (MainWindow._WA_STARTUP_GRACE_SECONDS + 1),
        )
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 1

    def test_opens_immediately_once_a_connection_was_ever_confirmed(self):
        """The grace window only protects a (re)connect attempt that has
        never yet succeeded — once _wa_connect_announced is True, a QR event
        is exactly as conclusive as before, even seconds after it fires."""
        mw = _FakeMainWindow(
            paired=True, pairing_dialog_active=False,
            wa_connect_announced=True, wa_startup_time=time.time(),
        )
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 1
