"""Tests for the startup grace window that keeps a booting session from being
announced as an outage.

Reported live: launching WinZapp announced "modo offline ativado
automaticamente" — sound and speech — and the session reported itself
connected 54 ms later. The log shows why:

    01:29:07,296  strike 1  status-session CLOSED        -> 'connecting'
    01:29:07,560  strike 2  status-session INITIALIZING  -> 'connecting'
    01:29:15,267  strike 3  status-session INITIALIZING  -> 'connecting'
    01:29:15,533  strike 4  ...
    01:29:15,796  strike 5  ...
    01:29:16,058  strike 6  ...
    01:29:16,329  strike 7  WhatsApp connection is down   <- announced
    01:29:16,383            WhatsApp connection is up

The grace had two bounds — 45 seconds and 6 not-yet-connected readings — and
they were measured in incompatible units. The window was sized for the health
checker's 30 s cadence, where 6 readings span three minutes and the clock
always runs out first. But _run_sync() polls check_wa_connection_http() every
0.2 s while waiting for the connection, and under that loop the same 6
readings were spent in 1.2 s: a 45-second grace collapsed to barely one,
9 seconds into a launch. The reading cap is gone; the clock is what the grace
actually meant.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so _set_wa_connected() is exercised as a plain function against a stub — same
approach as tests/test_shutdown_suppresses_offline.py.
"""

import time

import pytest

from main import MainWindow


class _Recorder:
    """Stands in for the sound objects and the speech output."""

    def __init__(self):
        self.played = 0
        self.spoken = []

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

    _set_wa_connected = MainWindow._set_wa_connected
    _reset_startup_probe = MainWindow._reset_startup_probe
    _announce_sync_events_enabled = MainWindow._announce_sync_events_enabled
    _self_inflicted_teardown_expected = MainWindow._self_inflicted_teardown_expected
    _WA_STARTUP_GRACE_SECONDS = MainWindow._WA_STARTUP_GRACE_SECONDS

    # Overridden per-test; the default keeps the grace intact.
    def _startup_offline_confirmed(self):
        return self.network_down

    def __init__(self, started_ago=0.0, network_down=False):
        self.network_down = network_down
        self.token = "tok"
        self.settings = {}
        self._shutting_down = False
        self._wpp_updating = False
        self._wa_connected = False
        self._auto_offline = False
        self._wa_connect_announced = False
        self._wa_offline_strikes = 0
        self._dead_browser_strikes = 0
        self._auto_repair_dialog_shown = False
        self._wa_startup_time = time.time() - started_ago
        self.background_mode = False
        self.i18n = _I18n()
        self.offline_mode_sound = _Recorder()
        self.connected_sound = _Recorder()
        self.statuses = []
        self.spoken = []
        self._tray_status = ""
        # Only reached on the connected branch.
        self.ws = None
        self._sync_completed = False
        self._last_sync_attempt_ts = 0
        self._sync_retry_count = 0
        self.sync_triggered = 0

    # -- collaborators the method touches -------------------------------
    def _apply_offline_state(self):
        pass

    def trigger_sync_if_needed(self):
        self.sync_triggered += 1

    def _set_status(self, text):
        self.statuses.append(text)

    def _set_preparing_status_if_idle(self):
        pass

    def output(self, text, interrupt=False):
        self.spoken.append(text)


@pytest.fixture(autouse=True)
def _no_wx(monkeypatch):
    """wx.CallAfter would need a running app; run the callback inline."""
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


class TestStartupGraceIsTimeBased:
    def test_a_fast_poll_loop_cannot_exhaust_the_grace(self):
        """The regression: _run_sync() polls every 0.2 s, which used to burn
        the whole window in about a second."""
        s = _Stub(started_ago=9.0)          # 9 s into a 45 s grace
        for _ in range(40):                  # far more than the old cap of 6
            s._set_wa_connected(False, "status-session INITIALIZING")

        assert s.offline_mode_sound.played == 0
        assert "offline_mode_auto_enabled" not in s.spoken
        assert s.statuses and s.statuses[-1] == "tray_connecting"
        assert s._auto_offline is False

    def test_the_grace_still_ends_on_the_clock(self):
        s = _Stub(started_ago=MainWindow._WA_STARTUP_GRACE_SECONDS + 1)
        s._set_wa_connected(False, "status-session INITIALIZING")

        assert s.offline_mode_sound.played == 1
        assert "offline_mode_auto_enabled" in s.spoken
        assert s._auto_offline is True

    def test_a_confirmed_logout_skips_the_grace_entirely(self):
        """notLogged/QRCODE are a definite answer, not a slow boot."""
        s = _Stub(started_ago=1.0)
        s._set_wa_connected(False, "status-session notLogged", confirmed=True)

        assert s._auto_offline is True
        assert s.offline_mode_sound.played == 1

    def test_the_grace_only_covers_the_first_connection_of_a_session(self):
        """Once connected, a later drop is a real outage and says so, however
        early in the session it happens."""
        s = _Stub(started_ago=1.0)
        s._set_wa_connected(True, "status-session CONNECTED")
        assert s._wa_connect_announced is True

        s._set_wa_connected(False, "status-session CONNECTED but isConnected() false")
        assert s._auto_offline is True
        assert s.offline_mode_sound.played == 1

    def test_connecting_within_the_grace_announces_nothing_about_offline(self):
        s = _Stub(started_ago=2.0)
        for _ in range(10):
            s._set_wa_connected(False, "status-session INITIALIZING")
        s._set_wa_connected(True, "session-logged")

        assert s.offline_mode_sound.played == 0
        assert s._auto_offline is False
        assert s.connected_sound.played == 1

    def test_the_removed_reading_cap_is_really_gone(self):
        """Guards the constant itself: reintroducing it would silently restore
        the cadence-dependent behaviour."""
        assert not hasattr(MainWindow, "_WA_STARTUP_GRACE_STRIKES")


class TestGraceEndsEarlyWithNoNetwork:
    """The grace stays long for a slow boot only because a real absence of
    network is detected separately — see _startup_offline_confirmed()."""

    def test_no_network_announces_offline_without_waiting_out_the_window(self):
        s = _Stub(started_ago=1.0, network_down=True)
        s._set_wa_connected(False, "status-session INITIALIZING")

        assert s._auto_offline is True
        assert s.offline_mode_sound.played == 1
        assert "offline_mode_auto_enabled" in s.spoken

    def test_a_reachable_network_keeps_the_full_grace(self):
        """A slow WPPConnect boot with working internet must still be silent."""
        s = _Stub(started_ago=1.0, network_down=False)
        for _ in range(40):
            s._set_wa_connected(False, "status-session INITIALIZING")

        assert s._auto_offline is False
        assert s.offline_mode_sound.played == 0
        assert s.statuses[-1] == "tray_connecting"

    def test_connecting_clears_the_network_verdict(self):
        """So a later grace window does not inherit a stale offline verdict."""
        s = _Stub(started_ago=1.0)
        s._startup_probe_offline = True
        s._startup_probe_fails = 5
        s._set_wa_connected(True, "session-logged")

        assert s._startup_probe_offline is False
        assert s._startup_probe_fails == 0


class _ProbeStub:
    _startup_offline_confirmed = MainWindow._startup_offline_confirmed
    _reset_startup_probe = MainWindow._reset_startup_probe
    _STARTUP_PROBE_INTERVAL = MainWindow._STARTUP_PROBE_INTERVAL
    _STARTUP_PROBE_STRIKES = MainWindow._STARTUP_PROBE_STRIKES

    def __init__(self, reachable):
        self._reset_startup_probe()
        self._reachable = reachable
        self.probes = 0

    def _probe_whatsapp_host(self):
        self.probes += 1
        return self._reachable


def _drain(stub):
    """Run the fire-and-forget probe thread to completion."""
    import threading
    for t in threading.enumerate():
        if t.name == "startup-net-probe":
            t.join(timeout=5)


class TestStartupOfflineProbe:
    def test_never_blocks_and_needs_a_second_failure_to_conclude(self):
        s = _ProbeStub(reachable=False)
        # First call only *starts* a probe — it must not block on the answer.
        assert s._startup_offline_confirmed() is False
        _drain(s)
        assert s._startup_probe_fails == 1
        assert s._startup_probe_offline is False

        s._startup_probe_at = 0.0          # let the rate limit through
        assert s._startup_offline_confirmed() is False
        _drain(s)
        assert s._startup_probe_offline is True

        # Now cached — answered without probing again.
        before = s.probes
        assert s._startup_offline_confirmed() is True
        assert s.probes == before

    def test_a_reachable_host_never_concludes_offline(self):
        s = _ProbeStub(reachable=True)
        for _ in range(4):
            s._startup_probe_at = 0.0
            assert s._startup_offline_confirmed() is False
            _drain(s)
        assert s._startup_probe_offline is False
        assert s._startup_probe_fails == 0

    def test_a_fast_poll_loop_cannot_spawn_a_probe_per_call(self):
        """_run_sync() polls every 0.2 s; without the rate limit that would be
        25 network probes in five seconds."""
        s = _ProbeStub(reachable=False)
        for _ in range(25):
            s._startup_offline_confirmed()
        _drain(s)
        assert s.probes == 1

    def test_a_recovered_network_resets_the_failure_tally(self):
        s = _ProbeStub(reachable=False)
        s._startup_offline_confirmed()
        _drain(s)
        assert s._startup_probe_fails == 1

        s._reachable = True
        s._startup_probe_at = 0.0
        s._startup_offline_confirmed()
        _drain(s)
        assert s._startup_probe_fails == 0

    def test_a_raising_probe_does_not_leave_the_gate_stuck(self):
        """_startup_probe_running must always clear, or every later call
        returns False forever and the early exit dies silently."""
        s = _ProbeStub(reachable=False)
        s._probe_whatsapp_host = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        s._startup_offline_confirmed()
        _drain(s)
        assert s._startup_probe_running is False
