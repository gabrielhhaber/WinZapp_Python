"""issue #202: the profile-recovery re-arm and the QR-flood counter reset
must happen as one atomic event, not two that can drift apart.

`_recover_suspect_profile()` runs at most once per launch behind
`_profile_recovery_attempted`, cleared only once a session reports CONNECTED.
That re-arm used to live in `_note_status_for_profile_health()`, keyed on the
bare status string alone — but `createSessionUtil.start()` can promote a
session to CONNECTED off its own state listener before the live
`isConnected()` probe ever agrees ("the event wins"), and
`check_wa_connection_http()` calls `_note_status_for_profile_health()`
*before* it confirms that probe. So a CONNECTED the probe was about to refuse
could re-arm recovery without resetting `_unattended_qr_events`, which only
zeroes once the probe agrees, inside `_set_wa_connected()`. A second recovery
could then start mid QR-flood on an event the counter had already counted,
and `on_qrcode_update()` returns as soon as recovery starts — landing on the
exact event where `seen == _UNATTENDED_QR_LIMIT` stops the halt from ever
being evaluated for the rest of that flood.

The fix moves the re-arm into `_set_wa_connected()`'s own "connection just
came back up" branch, right beside the counter reset, so both only ever fire
together, on the one event that branch already treats as the real online
transition.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so `_set_wa_connected()` is exercised as a plain function against a stub —
same approach as tests/test_disconnect_ignores_stale_connected.py.
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


class _BoomOnBool:
    """Raises when the re-arm tests it, standing in for any failure inside
    the wrapped block."""

    def __bool__(self):
        raise RuntimeError("diagnostic is broken")


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
        self._send_capabilities_checked = False
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

        # What the QR-flood halt guards, and what this file is actually
        # about: pre-arm both as a broken/mid-flood session would carry them.
        self._unattended_qr_events = 3
        self._qr_flood_halted = False
        self._profile_recovery_attempted = True
        self._recovery_generation = 0

    def _profile_recovery_generation(self):
        return self._recovery_generation

    def _set_profile_recovery_generation(self, value):
        self._recovery_generation = value

    def _apply_offline_state(self):
        pass

    def trigger_sync_if_needed(self):
        self.sync_triggered += 1

    def _set_status(self, text):
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
    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr("main.threading.Thread", _InlineThread)


class TestTheRearmAndTheCounterResetTogether:
    def test_a_probe_that_disagrees_leaves_both_untouched(self):
        """The exact gap issue #202 describes: CONNECTED promoted by the
        state listener, but isConnected() refuses it. Neither the counter
        nor the recovery latch may move."""
        s = _Stub()

        s._set_wa_connected(False, "status-session CONNECTED but isConnected() false")

        assert s._unattended_qr_events == 3
        assert s._profile_recovery_attempted is True

    def test_a_probe_that_agrees_resets_both_in_the_same_call(self):
        s = _Stub()

        s._set_wa_connected(True, "status-session CONNECTED")

        assert s._unattended_qr_events == 0
        assert s._profile_recovery_attempted is False

    def test_the_generation_ladder_is_deliberately_left_behind(self):
        """Only the recovery BUDGET moved here. The generation ladder — which
        chooses WHICH snapshot a restore reaches for — stays on the
        status-string reading in _note_status_for_profile_health(), and this
        pins that on purpose rather than by omission.

        #202 is about the budget racing the flood counter. The ladder never
        touches _unattended_qr_events at all, so that argument does not reach
        it — and moving it costs a property it depends on: it has to be
        re-asserted on EVERY CONNECTED poll, not once per transition. A manual
        re-pair completes inside Connect.show_connection_dial(), which runs
        before prepare_sync() opens the database, so the write here would be a
        silent no-op (_set_profile_recovery_generation() guards on self.db) —
        and with a transition-only reset nothing would clear it again for the
        rest of that launch, sending the next break at a day-old .prev
        snapshot instead of the newest one. See
        TestTheLadderStillResetsOnTheStatusReading below for the other half.
        """
        s = _Stub()
        s._recovery_generation = 2

        s._set_wa_connected(True, "status-session CONNECTED")

        assert s._recovery_generation == 2
        # ...while the two that DID move still fire on this same event.
        assert s._profile_recovery_attempted is False
        assert s._unattended_qr_events == 0

    def test_a_broken_generation_read_cannot_take_the_connection_offline(self):
        """The block is wrapped because its old home was wrapped twice — a bug
        in a diagnostic must never change the connection verdict. Here it
        would: this runs inside check_wa_connection_http()'s try, whose
        handler ends in _set_wa_connected(False, ...)."""
        s = _Stub()

        s._profile_recovery_attempted = _BoomOnBool()

        s._set_wa_connected(True, "status-session CONNECTED")

        # Reached the rest of the branch regardless.
        assert s._unattended_qr_events == 0
        assert s._wa_connected is True


    def test_a_disagreeing_probe_then_a_later_agreeing_one_only_rearms_once_it_agrees(self):
        """The realistic sequence: the flood carries on across a couple of
        refused CONNECTEDs before the probe finally agrees."""
        s = _Stub()

        s._set_wa_connected(False, "status-session CONNECTED but isConnected() false")
        s._set_wa_connected(False, "status-session CONNECTED but isConnected() false")
        assert s._profile_recovery_attempted is True
        assert s._unattended_qr_events == 3

        s._set_wa_connected(True, "status-session CONNECTED")
        assert s._profile_recovery_attempted is False
        assert s._unattended_qr_events == 0

    def test_a_stale_connected_report_with_no_token_touches_neither(self):
        """Regression guard against the neighbouring fix in this same
        method (test_disconnect_ignores_stale_connected.py): the no-token
        early return must not accidentally reach the re-arm/reset lines
        either."""
        s = _Stub(token="")

        s._set_wa_connected(True, "status-session CONNECTED")

        assert s._unattended_qr_events == 3
        assert s._profile_recovery_attempted is True

    def test_already_connected_reconfirmation_does_not_repeat_the_reset(self):
        """A repeat CONNECTED while nothing changed must hit the method's
        own early return, not re-run the reset every health-check tick —
        harmless either way here since the values are idempotent, but this
        pins that the early return still fires before reaching them."""
        s = _Stub()
        s._set_wa_connected(True, "status-session CONNECTED")
        assert s._unattended_qr_events == 0  # the first connect's own reset

        s._unattended_qr_events = 5  # simulate a fresh QR seen after connecting
        s._set_wa_connected(True, "status-session CONNECTED (re-poll)")

        assert s._unattended_qr_events == 5  # untouched: early return, not a reset


class TestTheLadderStillResetsOnTheStatusReading:
    """The other half of the split: the generation ladder stays in
    _note_status_for_profile_health(), re-asserted on every CONNECTED poll.

    Moving it into _set_wa_connected() alongside the budget looks tidier and
    is wrong. That block sits below a no-change early return, so it only runs
    on a real False->True transition — and the one transition every manual
    re-pair goes through happens inside Connect.show_connection_dial(), which
    MainWindow.__init__ calls BEFORE prepare_sync() opens the database. The
    write would find no self.db, no-op silently, and never be attempted again
    for the rest of that launch: a stale ladder then sends the next break at
    the .prev snapshot — up to a day older — instead of the newest one.
    """

    class _HealthStub:
        def __init__(self, generation=0, db=object()):
            from main import MainWindow
            self._note_status_for_profile_health = (
                MainWindow._note_status_for_profile_health.__get__(self))
            self.settings = {"privateinfo": {"paired": True}}
            self.db = db
            self._generation = generation
            self.generation_writes = []
            self.recover_calls = 0

        def _profile_recovery_generation(self):
            # The real one answers 0 when there is no database to read.
            return self._generation if self.db is not None else 0

        def _set_profile_recovery_generation(self, value):
            # The real one is a no-op without a database — that is the whole
            # point of this test.
            if self.db is None:
                return
            self._generation = value
            self.generation_writes.append(value)

        def _recover_suspect_profile(self, *a, **kw):
            self.recover_calls += 1
            return True

    def test_a_connected_reading_clears_a_stale_ladder(self):
        s = self._HealthStub(generation=2)

        s._note_status_for_profile_health("CONNECTED")

        assert s._generation == 0
        assert s.generation_writes == [0]

    def test_every_poll_re_asserts_it_not_just_the_first(self):
        """The property a transition-only reset would lose."""
        s = self._HealthStub(generation=1)

        s._note_status_for_profile_health("CONNECTED")
        s._generation = 1          # something armed it again mid-launch
        s._note_status_for_profile_health("CONNECTED")

        assert s._generation == 0
        assert s.generation_writes == [0, 0]

    def test_a_ladder_already_at_zero_is_not_rewritten(self):
        s = self._HealthStub(generation=0)

        s._note_status_for_profile_health("CONNECTED")

        assert s.generation_writes == []

    def test_a_non_connected_reading_leaves_it_alone(self):
        s = self._HealthStub(generation=2)

        s._note_status_for_profile_health("CLOSED")

        assert s._generation == 2

    def test_the_pairing_launch_that_has_no_database_yet_recovers_next_poll(self):
        """The failure a transition-only reset would make permanent: pairing
        completes before prepare_sync(), so this write cannot land — and
        because this path runs on EVERY poll, the one after the database
        opens finishes the job."""
        s = self._HealthStub(generation=2, db=None)

        s._note_status_for_profile_health("CONNECTED")
        assert s._generation == 2          # nothing could be written yet

        s.db = object()                    # prepare_sync() has now run
        s._note_status_for_profile_health("CONNECTED")

        assert s._generation == 0
