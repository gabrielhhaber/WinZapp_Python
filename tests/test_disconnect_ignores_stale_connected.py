"""Arquivo > Desconectar (_on_disconnect()) played the "connected" sound and
started a fresh sync seconds after the user explicitly disconnected.

_on_disconnect() clears self.token and resets self._wa_connected = False so
the app looks freshly offline. But check_wa_connection_http() (or a pairing
callback) can already have an HTTP request in flight against the OLD,
still-valid token at the exact moment disconnect starts — its response can
land moments later still reporting CONNECTED, straight into
_set_wa_connected(True, ...). Nothing distinguished that stale report from a
genuine offline→online transition: was (already reset to False) != connected
(True) looked exactly like "we just came back online", so it replayed the
whole reconnect sequence — connected_sound, a forced WebSocket reconnect,
trigger_sync_if_needed() — against a session with no token left, which then
404'd on /api//list-chats (empty token, double slash) live in a user's log.

Fixed with a guard at the top of the `if connected:` branch: an empty
self.token means there is no session to be validly connected to, so the
report is ignored and none of the reconnect side effects fire.

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so _set_wa_connected() is exercised as a plain function against a
stub — same approach as tests/test_status_synced_with_connected_sound.py.
"""

import time

import pytest


class _Recorder:
    def __init__(self):
        self.played = 0

    def play(self):
        self.played += 1


class _I18n:
    @staticmethod
    def t(key):
        return key


class _Stub:
    # The first confirmed connection of a session also kicks off the
    # send-capabilities probe on a background thread — real HTTP, and not
    # what any test here is about. Record it instead.
    def _check_send_capabilities(self):
        self.capability_probes = getattr(self, "capability_probes", 0) + 1

    def __init__(self, token="tok"):
        from main import MainWindow
        self._set_wa_connected = MainWindow._set_wa_connected.__get__(self)
        self._set_preparing_status_if_idle = (
            MainWindow._set_preparing_status_if_idle.__get__(self))
        self._reset_startup_probe = MainWindow._reset_startup_probe.__get__(self)
        self._announce_sync_events_enabled = MainWindow._announce_sync_events_enabled.__get__(self)
        self._self_inflicted_teardown_expected = (
            MainWindow._self_inflicted_teardown_expected.__get__(self))
        self._WA_STARTUP_GRACE_SECONDS = MainWindow._WA_STARTUP_GRACE_SECONDS

        self.settings = {}
        self.token = token

        self._shutting_down = False
        self._wpp_updating = False
        self._wa_connected = False
        self._auto_offline = False
        self._wa_connect_announced = False
        self._wa_offline_strikes = 0
        self._dead_browser_strikes = 0
        self._auto_repair_dialog_shown = False
        self._wa_startup_time = time.time()
        self.background_mode = False
        self.i18n = _I18n()
        self.offline_mode_sound = _Recorder()
        self.connected_sound = _Recorder()
        self.statuses = []
        self.spoken = []
        self._tray_status = "tray_connecting"
        self.ws = None
        self._sync_completed = False
        self._initial_sync_running = False
        self._last_sync_attempt_ts = 0
        self._sync_retry_count = 0
        self.sync_triggered = 0
        self.reconnect_threads_started = 0

    def _apply_offline_state(self):
        pass

    def trigger_sync_if_needed(self):
        self.sync_triggered += 1

    def _set_status(self, text):
        self._tray_status = text
        self.statuses.append(text)

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def _startup_offline_confirmed(self):
        return False

    def _reconnect_websocket_now(self):
        self.reconnect_threads_started += 1


@pytest.fixture(autouse=True)
def _no_wx(monkeypatch):
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


@pytest.fixture(autouse=True)
def _no_threads(monkeypatch):
    """threading.Thread(target=self._reconnect_websocket_now, ...).start()
    must not actually spawn a thread in a unit test — run inline so the
    assertion can see whether it ran at all."""
    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr("main.threading.Thread", _InlineThread)


class TestStaleConnectedReportAfterDisconnect:
    def test_a_stale_connected_report_with_no_token_is_ignored(self):
        s = _Stub(token="tok")
        # First-ever connect, same as a normal session start.
        s._set_wa_connected(True, "status-session CONNECTED")
        assert s.connected_sound.played == 1

        # _on_disconnect(): token cleared, connected flag reset — the app
        # now looks like it just went offline.
        s.token = ""
        s._wa_connected = False
        s.sync_triggered = 0

        # The stale check_wa_connection_http() response that was already in
        # flight lands here, still claiming CONNECTED.
        s._set_wa_connected(True, "status-session CONNECTED")

        # None of the "we just reconnected" side effects fired a second time.
        assert s.connected_sound.played == 1
        assert s.sync_triggered == 0
        assert s.reconnect_threads_started == 0

    def test_a_connected_report_with_a_real_token_still_works(self):
        """Regression guard: the guard must not swallow genuine reconnects."""
        s = _Stub(token="tok")
        s._set_wa_connected(True, "status-session CONNECTED")
        assert s.connected_sound.played == 1

        # A real drop-and-recover: still has a token throughout.
        s._set_wa_connected(False, "status-session CONNECTED but isConnected() false")
        s._set_wa_connected(True, "reconnected")

        assert s.sync_triggered >= 1

    def test_the_very_first_connect_of_a_session_never_has_an_empty_token(self):
        """Sanity check the guard's premise: by the time any connected
        report can reach _set_wa_connected(), self.token is already set —
        the guard only ever fires in the disconnect-race window, not on
        ordinary startup."""
        s = _Stub(token="tok")
        s._set_wa_connected(True, "status-session CONNECTED")
        assert s.connected_sound.played == 1
        assert s.sync_triggered == 1
