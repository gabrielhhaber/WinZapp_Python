"""Tests for WebSocketClient.on_connection_update()'s "close" branch during
our own deliberate shutdown.

The bug: real_exit() -> _perform_shutdown() -> _stop_wpp_server() posts
/close-session itself and then polls for the flush, all while the WebSocket
is deliberately left connected (see _perform_shutdown()'s own comment on
_shutting_down, which already exists specifically to stop this exact kind of
self-inflicted event from being misread -- but was only ever wired into
_set_wa_connected(), not into the destructive logout-detection branch here).

Baileys/WPPConnect reports a session WE closed ourselves via /close-session
the same way it reports a real phone-side logout: connection.update fires
with state="close" and loggedOut=True or statusCode=401. Before this fix,
on_connection_update() could not tell the two apart, so an entirely ordinary
quit (Ctrl+Alt+Shift+Q or any other exit path) could wipe the token, drop
`paired`, and run clear_local_data() -- all persisted to disk before the
process finished exiting. The next launch then found an empty token and
showed "Your device has been disconnected" for a device that was never
actually disconnected. This is the exact "closed WinZapp, relaunched, told
the device was disconnected" report.

WebSocketClient is exercised as a plain function bound onto a small stub (no
real socketio/wx.App needed) -- same approach as
tests/test_qrcode_auto_repair_dialog.py uses for on_qrcode_update.
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


class _FakeMainWindow:
    # Bound for real (not re-implemented) so this test exercises the exact
    # same helper on_connection_update()/on_wpp_status_find() call in
    # production -- a hand-rolled stub method here would silently stop
    # catching a future regression where the two drift apart again.
    _self_inflicted_teardown_expected = MainWindow._self_inflicted_teardown_expected

    def __init__(self, *, shutting_down=False, wpp_updating=False,
                 recovery_restart_active=False, restarting_wpp_session=False,
                 paired=True, pairing_in_progress=False,
                 messages_set_completed=True, wa_connected=True):
        self.settings = {"privateinfo": {"paired": paired}}
        self._shutting_down = shutting_down
        self._wpp_updating = wpp_updating
        self._recovery_restart_active = recovery_restart_active
        self._restarting_wpp_session = restarting_wpp_session
        self._pairing_in_progress = pairing_in_progress
        self.messages_set_completed = messages_set_completed
        self._wa_connected = wa_connected
        self.error_sound = _FakeSound()
        self.app_name = "WinZapp"
        self.wa_connected_calls = []

    def _set_wa_connected(self, connected, reason, *a, **kw):
        self.wa_connected_calls.append((connected, reason))

    def output(self, text, interrupt=False):
        pass

    def _set_status(self, text):
        pass


class _Stub:
    on_connection_update = WebSocketClient.on_connection_update
    on_wpp_status_find = WebSocketClient.on_wpp_status_find

    def __init__(self, main_window):
        self.main_window = main_window
        self.i18n = _FakeI18n()
        self.logout_calls = 0
        self.pairing_failed_calls = 0
        self._logout_handled = False

    def _handle_logout(self):
        self.logout_calls += 1

    def _handle_pairing_failed(self):
        self.pairing_failed_calls += 1


@pytest.fixture(autouse=True)
def _synchronous_call_after(monkeypatch):
    monkeypatch.setattr("core.websocket_client.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("core.websocket_client.wx.MessageBox", lambda *a, **kw: None)


def _close_event(*, logged_out=True, status_code=None):
    data = {"state": "close"}
    if logged_out:
        data["loggedOut"] = True
    if status_code is not None:
        data["statusCode"] = status_code
    return {"data": data}


class TestCloseDuringOwnShutdownIsNeverALogout:
    def test_logged_out_flag_during_shutdown_does_not_wipe(self):
        mw = _FakeMainWindow(shutting_down=True)
        s = _Stub(mw)

        s.on_connection_update(_close_event(logged_out=True))

        assert s.logout_calls == 0
        assert s.pairing_failed_calls == 0

    def test_statuscode_401_during_shutdown_does_not_wipe(self):
        mw = _FakeMainWindow(shutting_down=True)
        s = _Stub(mw)

        s.on_connection_update(_close_event(logged_out=False, status_code=401))

        assert s.logout_calls == 0

    def test_failed_pairing_during_shutdown_is_also_ignored(self):
        """A close mid-shutdown must never fire ANY destructive branch, not
        just the logout one."""
        mw = _FakeMainWindow(shutting_down=True, pairing_in_progress=True,
                             messages_set_completed=False)
        s = _Stub(mw)

        s.on_connection_update(_close_event(logged_out=False, status_code=None))

        assert s.pairing_failed_calls == 0
        assert s.logout_calls == 0

    def test_status_find_during_shutdown_also_does_not_wipe(self):
        mw = _FakeMainWindow(shutting_down=True, wa_connected=True, paired=True)
        s = _Stub(mw)

        s.on_wpp_status_find({"status": "notLogged", "session": None})

        assert s.logout_calls == 0


class TestCloseDuringOwnUpdateIsAlsoNeverALogout:
    """The gap found by re-reviewing every _stop_wpp_server() caller, not
    just the one already fixed: _update_wpp_server() sets _wpp_updating
    (not _shutting_down) and calls _stop_wpp_server() directly to replace a
    running WPPConnect Server build. Before _self_inflicted_teardown_expected()
    consolidated both flags, a close/notLogged signal arriving during an
    ordinary auto-update was indistinguishable from the shutdown case this
    file already covers, and would have wiped the token exactly the same way.
    """

    def test_logged_out_flag_during_update_does_not_wipe(self):
        mw = _FakeMainWindow(wpp_updating=True)
        s = _Stub(mw)

        s.on_connection_update(_close_event(logged_out=True))

        assert s.logout_calls == 0

    def test_status_find_during_update_does_not_wipe(self):
        mw = _FakeMainWindow(wpp_updating=True, wa_connected=True, paired=True)
        s = _Stub(mw)

        s.on_wpp_status_find({"status": "notLogged", "session": None})

        assert s.logout_calls == 0


class TestCloseDuringRecoveryRestartIsAlsoNeverALogout:
    """The gap found by re-reviewing every close-session-triggering method,
    not just the two already fixed: _restart_session_once() (called from
    _run_recovery_attempts(), the power-resume zombie-session recovery
    cycle) sets _recovery_restart_active (not _shutting_down or
    _wpp_updating) and posts /close-session on this account's own session
    directly. Previously guarded only inside check_wa_connection_http()'s
    own CLOSED auto-start branch -- not here -- so an ordinary sleep/wake
    cycle could wipe a perfectly good session's token the exact same way as
    the shutdown and update cases above."""

    def test_logged_out_flag_during_recovery_restart_does_not_wipe(self):
        mw = _FakeMainWindow(recovery_restart_active=True)
        s = _Stub(mw)

        s.on_connection_update(_close_event(logged_out=True))

        assert s.logout_calls == 0

    def test_status_find_during_recovery_restart_does_not_wipe(self):
        mw = _FakeMainWindow(recovery_restart_active=True, wa_connected=True, paired=True)
        s = _Stub(mw)

        s.on_wpp_status_find({"status": "notLogged", "session": None})

        assert s.logout_calls == 0


class TestCloseDuringInPlaceSessionRestartIsAlsoNeverALogout:
    """The gap found by re-checking every OTHER close-session-triggering
    method one more time, after the shutdown/update/recovery-restart cases
    above were already fixed: _restart_wpp_session() (the detached-
    Puppeteer-page in-place restart -- a DIFFERENT wake-from-sleep trigger
    than the recovery-restart case) sets _restarting_wpp_session and posts
    /close-session on this account's own session directly. That function's
    own docstring documents a real incident from exactly this mechanism,
    but the fix that shipped then only covered check_wa_connection_http()'s
    slower strike-based path -- this immediate, single-event close branch
    stayed completely unguarded against it."""

    def test_logged_out_flag_during_restart_does_not_wipe(self):
        mw = _FakeMainWindow(restarting_wpp_session=True)
        s = _Stub(mw)

        s.on_connection_update(_close_event(logged_out=True))

        assert s.logout_calls == 0

    def test_status_find_during_restart_does_not_wipe(self):
        mw = _FakeMainWindow(restarting_wpp_session=True, wa_connected=True, paired=True)
        s = _Stub(mw)

        s.on_wpp_status_find({"status": "notLogged", "session": None})

        assert s.logout_calls == 0


class TestCloseOutsideShutdownStillDetectsARealLogout:
    """The guard must not swallow a genuine logout that happens to arrive
    while _shutting_down is False (the overwhelmingly common case: the app
    is just running normally and WhatsApp actually unlinks the device)."""

    def test_logged_out_flag_not_shutting_down_still_wipes(self):
        mw = _FakeMainWindow(shutting_down=False)
        s = _Stub(mw)

        s.on_connection_update(_close_event(logged_out=True))

        assert s.logout_calls == 1

    def test_statuscode_401_not_shutting_down_still_wipes(self):
        mw = _FakeMainWindow(shutting_down=False)
        s = _Stub(mw)

        s.on_connection_update(_close_event(logged_out=False, status_code=401))

        assert s.logout_calls == 1

    def test_failed_pairing_not_shutting_down_still_detected(self):
        mw = _FakeMainWindow(shutting_down=False, pairing_in_progress=True,
                             messages_set_completed=False)
        s = _Stub(mw)

        s.on_connection_update(_close_event(logged_out=False, status_code=None))

        assert s.pairing_failed_calls == 1
        assert s.logout_calls == 0

    def test_status_find_not_shutting_down_still_detected(self):
        mw = _FakeMainWindow(shutting_down=False, wa_connected=True, paired=True)
        s = _Stub(mw)

        s.on_wpp_status_find({"status": "notLogged", "session": None})

        assert s.logout_calls == 1

    def test_an_ordinary_temporary_close_never_wipes_either_way(self):
        """Not a logout, not a failed pairing -- just a normal reconnect
        blip. Must never touch credentials, shutting down or not."""
        mw = _FakeMainWindow(shutting_down=False, pairing_in_progress=False)
        s = _Stub(mw)

        s.on_connection_update(_close_event(logged_out=False, status_code=None))

        assert s.logout_calls == 0
        assert s.pairing_failed_calls == 0
