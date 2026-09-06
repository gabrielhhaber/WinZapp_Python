"""Tests that the "preparing_to_sync" status text changes at the exact same
moment connected_sound plays — not earlier.

Reported live: prepare_sync() (called synchronously during __init__, well
before the window/tray even exist and long before the connection is
actually confirmed) used to set the "preparing_to_sync" status itself. The
window ended up appearing already on "Preparando para sincronizar" with
connected_sound only playing several seconds later, once
_set_wa_connected(True) actually ran — the "Conectando..." status the user
expects to see until the sound fires was skipped every time. Fixed by
having _set_wa_connected(True)'s first-ever-connect branch (the same branch
that plays the sound) set the status too, and having
wait_messages_set() not set it at all anymore.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so _set_wa_connected() is exercised as a plain function against a stub —
same approach as tests/test_startup_grace.py.
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

    def __init__(self):
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
        self.token = "tok"

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
        # Starts as "tray_connecting" — mirrors MainWindow.__init__.
        self._tray_status = "tray_connecting"
        self.ws = None
        self._sync_completed = False
        self._initial_sync_running = False
        self._last_sync_attempt_ts = 0
        self._sync_retry_count = 0
        self.sync_triggered = 0

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


@pytest.fixture(autouse=True)
def _no_wx(monkeypatch):
    """wx.CallAfter would need a running app; run the callback inline."""
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


class TestStatusChangesInLockstepWithTheConnectedSound:
    def test_status_is_still_connecting_right_before_the_first_connect(self):
        s = _Stub()
        assert s._tray_status == "tray_connecting"

    def test_first_ever_connect_sets_preparing_to_sync_and_plays_the_sound_together(self):
        s = _Stub()

        s._set_wa_connected(True, "status-session CONNECTED")

        assert s.connected_sound.played == 1
        assert s._tray_status == "preparing_to_sync"
        # Both effects landed in the same call — nothing in between could
        # have observed the sound played without the status having changed.
        assert s.statuses == ["preparing_to_sync"]

    def test_status_never_reads_preparing_to_sync_before_the_sound_plays(self):
        """Regression guard: many not-yet-connected updates (a slow boot)
        must never advance the status past "connecting" on their own."""
        s = _Stub()
        for _ in range(20):
            s._set_wa_connected(False, "status-session INITIALIZING")
        assert "preparing_to_sync" not in s.statuses
        assert s.connected_sound.played == 0

        s._set_wa_connected(True, "session-logged")
        assert s.connected_sound.played == 1
        assert s.statuses[-1] == "preparing_to_sync"

    def test_delayed_preparing_callback_cannot_overwrite_an_active_sync(self):
        s = _Stub()
        s._initial_sync_running = True

        s._set_preparing_status_if_idle()

        assert s.statuses == []
        assert s._tray_status == "tray_connecting"

    def test_a_later_reconnect_clears_status_instead_of_re_announcing_preparing(self):
        """A drop-and-recover mid-session is not a fresh startup — the
        "conectando -> preparando" staging only applies once per session."""
        s = _Stub()
        s._set_wa_connected(True, "status-session CONNECTED")
        assert s.statuses[-1] == "preparing_to_sync"

        s._tray_status = "tray_wa_disconnected"
        s._set_wa_connected(False, "status-session CONNECTED but isConnected() false")
        s._set_wa_connected(True, "reconnected")

        assert s.statuses[-1] == ""
        assert s.connected_sound.played == 1  # not played a second time
