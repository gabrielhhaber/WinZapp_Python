"""Where MainWindow may take a profile snapshot, and what it does when the
profile turns out to be broken.

The mechanism lives in core/profile_recovery.py (tested there). What is pinned
here is the policy, because every one of these conditions is the difference
between a restore point that helps and one that makes things worse:

* a snapshot may only be taken from a profile that was closed cleanly, or it
  captures the half-written leveldb it exists to protect against;
* it may never run on the Windows WM_ENDSESSION path, whose whole budget is
  needed by the flush;
* the session must be closed before the profile is touched, or the restore
  overwrites a leveldb while Chrome holds it open;
* and when there is nothing to restore, the user has to be *told* — this only
  ever happens while already offline, where the connection announcements have
  long since gone quiet.
"""

import types

import pytest

from core.profile_recovery import ProfileHealthTracker
from main import MainWindow


class _MetadataDB:
    """Just the metadata pair the recovery generation is persisted through."""

    def __init__(self):
        self.values = {}

    def get_metadata_json(self, key, default=None):
        return self.values.get(key, default)

    def set_metadata_json(self, key, value):
        self.values[key] = value


class _Stub:
    """Carries only what the methods under test actually touch."""

    def __init__(self, paired=True, token="sess123:tok", global_dir="/g"):
        self.settings = {"privateinfo": {"paired": paired}}
        self.token = token
        self.global_dir = global_dir
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.background_mode = True
        self.audits = []
        self.announced = []
        self.recovered = 0
        self.error_sound = types.SimpleNamespace(play=lambda: None)
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self.db = _MetadataDB()

    # Bound from the real class: the generation ladder decides *which*
    # snapshot goes back, so a stub that faked it would let the wiring drift
    # from the module its own tests cover.
    _PROFILE_RECOVERY_GENERATION_KEY = MainWindow._PROFILE_RECOVERY_GENERATION_KEY
    _profile_recovery_generation = MainWindow._profile_recovery_generation
    _set_profile_recovery_generation = MainWindow._set_profile_recovery_generation

    def _shutdown_audit(self, msg):
        self.audits.append(msg)

    def output(self, text, interrupt=False):
        self.announced.append(text)

    def wait_for_profile_release(self, session_name, timeout=20.0):
        # Real implementation polls win32 process handles for up to
        # `timeout` seconds -- not appropriate off the real profile paths a
        # unit test stub has none of.
        return True

    def _recover_suspect_profile(self):
        self.recovered += 1

    # Bound from the real class rather than faked: these two ARE the
    # user-visible half of the feature, and a stub that only records a call
    # would pass while the announcement said nothing.
    def _announce_profile_beyond_repair(self):
        MainWindow._announce_profile_beyond_repair(self)

    def _announce_profile_restored(self):
        MainWindow._announce_profile_restored(self)


def _note(stub, status):
    return MainWindow._note_status_for_profile_health(stub, status)


def _cycle(stub, times):
    for _ in range(times):
        _note(stub, "INITIALIZING")
        _note(stub, "CLOSED")


class TestTheDetectorIsWiredToTheStatusPoll:
    def test_three_failed_cycles_trigger_recovery(self):
        stub = _Stub()
        _cycle(stub, 3)
        assert stub.recovered == 1

    def test_two_are_not_enough(self):
        stub = _Stub()
        _cycle(stub, 2)
        assert stub.recovered == 0

    def test_an_unpaired_account_is_never_touched(self):
        """Mid-pairing there is no login to lose, and the QR/code flow drives
        the session through these very states on purpose."""
        stub = _Stub(paired=False)
        _cycle(stub, 5)
        assert stub.recovered == 0

    def test_a_successful_connection_clears_the_tally(self):
        stub = _Stub()
        _cycle(stub, 2)
        _note(stub, "CONNECTED")
        _cycle(stub, 2)
        assert stub.recovered == 0

    def test_a_broken_tracker_never_breaks_the_health_poll(self, monkeypatch):
        """This runs inside check_wa_connection_http(), which decides whether
        the app is online. It must not be able to take that down."""
        stub = _Stub()
        stub._profile_health = types.SimpleNamespace(
            note_status=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        _note(stub, "CLOSED")
        assert stub.recovered == 0


class TestRecoveryRunsAtMostOncePerLaunch:
    def test_a_second_crossing_does_nothing(self, monkeypatch):
        stub = _Stub()
        calls = []
        monkeypatch.setattr(
            "core.profile_recovery.has_snapshot",
            lambda *a, **kw: calls.append(a) or False)
        monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: None)
        MainWindow._recover_suspect_profile(stub)
        MainWindow._recover_suspect_profile(stub)
        assert len(calls) == 1

    def test_without_a_token_nothing_is_attempted(self, monkeypatch):
        stub = _Stub(token="")
        monkeypatch.setattr(
            "core.profile_recovery.has_snapshot",
            lambda *a, **kw: pytest.fail("should not have looked for a snapshot"))
        MainWindow._recover_suspect_profile(stub)


class TestWithNoSnapshotTheUserIsTold:
    """The dead end. It only happens while already offline, where
    _set_wa_connected(False, ...) has hit its no-change early return and said
    nothing — so silence here is a user with no idea what to do."""

    def test_the_message_is_announced(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr("core.profile_recovery.has_snapshot", lambda *a, **kw: False)
        monkeypatch.setattr("main.wx.CallAfter",
                            lambda fn, *a, **kw: fn(*a, **kw))
        MainWindow._recover_suspect_profile(stub)
        assert "profile_corrupted_repair_needed" in stub.announced

    def test_the_suspicion_is_audited_either_way(self, monkeypatch):
        """shutdown_audit.log is the only file that survives the next launch,
        and this is exactly the diagnosis a user's next report needs."""
        stub = _Stub()
        monkeypatch.setattr("core.profile_recovery.has_snapshot", lambda *a, **kw: False)
        monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: None)
        MainWindow._recover_suspect_profile(stub)
        assert any("profile suspect" in line for line in stub.audits)


class TestSnapshotsAreOnlyTakenWhenTheProfileIsProvablyConsistent:
    @pytest.fixture
    def captured(self, monkeypatch):
        calls = []
        monkeypatch.setattr("core.profile_recovery.capture_snapshot",
                            lambda *a, **kw: calls.append(a) or True)
        return calls

    def test_a_clean_close_with_no_budget_snapshots(self, captured):
        stub = _Stub()
        MainWindow._capture_profile_snapshot(stub, "sess123", True, None)
        assert len(captured) == 1
        assert any("snapshot" in line for line in stub.audits)

    def test_an_unconfirmed_close_never_snapshots(self, captured):
        """browser_closed_cleanly False means the graceful close-session never
        confirmed, so the leveldb may be mid-write — precisely the state a
        restore point must never capture."""
        stub = _Stub()
        MainWindow._capture_profile_snapshot(stub, "sess123", False, None)
        assert captured == []

    def test_the_windows_shutdown_path_never_snapshots(self, captured):
        """A budget means Windows owns the clock, ~5s before the process is
        killed as hung. Copying hundreds of megabytes would spend it on the
        wrong thing — the flush is what prevents the corruption."""
        stub = _Stub()
        MainWindow._capture_profile_snapshot(stub, "sess123", True, 5.0)
        assert captured == []

    def test_a_failing_snapshot_never_breaks_the_teardown(self, monkeypatch):
        """A missing restore point is a nicety lost; a teardown that dies here
        is the corruption itself."""
        stub = _Stub()
        monkeypatch.setattr(
            "core.profile_recovery.capture_snapshot",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
        MainWindow._capture_profile_snapshot(stub, "sess123", True, None)

    def test_without_a_global_dir_nothing_is_attempted(self, captured):
        stub = _Stub(global_dir=None)
        MainWindow._capture_profile_snapshot(stub, "sess123", True, None)
        assert captured == []


class TestTheTrackerItselfIsTheOneUsed:
    def test_main_uses_the_shared_tracker_class(self):
        """A second copy of this decision is how the detector starts
        disagreeing with the module its tests cover."""
        stub = _Stub()
        _note(stub, "INITIALIZING")
        assert isinstance(stub._profile_health, ProfileHealthTracker)


class TestTheStartRequestArmsTheDetector:
    """/start-session is the timing-independent evidence that a start is being
    attempted; the INITIALIZING window is narrower than the polling interval,
    so the poll cannot be relied on to catch it. See the tracker's docstring."""

    def _start(self, stub):
        return MainWindow._note_session_start_for_profile_health(stub)

    def test_start_requests_alone_reach_the_threshold(self):
        stub = _Stub()
        for _ in range(3):
            self._start(stub)
            _note(stub, "CLOSED")
        assert stub.recovered == 1

    def test_the_real_broken_installs_poll_sequence_recovers(self):
        # The readings log.log actually recorded on 2026-09-08, with the
        # start-session POSTs the health checker made between them.
        stub = _Stub()
        _note(stub, "INITIALIZING")
        for _ in range(3):
            _note(stub, "disconnectedMobile")
            _note(stub, "CLOSED")
            self._start(stub)
        assert stub.recovered == 1

    def test_an_unpaired_account_is_still_never_touched(self):
        stub = _Stub(paired=False)
        for _ in range(6):
            self._start(stub)
            _note(stub, "CLOSED")
        assert stub.recovered == 0

    def test_a_broken_tracker_never_breaks_the_health_poll(self, monkeypatch):
        """Same rule as its sibling: profile health observes the connection
        poll and must never be able to change that poll's verdict."""
        stub = _Stub()
        stub.settings = None          # any access raises
        self._start(stub)             # must not propagate


class TestARunThatNeverConnectedMayNotOverwriteTheRestorePoint:
    """The trap that came within hours of costing a real session.

    Closing cleanly is evidence about *how* the profile was written, never
    about whether what was written is worth keeping. On 2026-09-08 the profile
    stopped carrying a login, WhatsApp Web logged itself out of it, and the
    good snapshot was 16.5 h old — the only thing standing between a
    successful hand-restore and a permanently lost session was the 24 h
    refresh window not having elapsed yet.
    """

    @pytest.fixture
    def captured(self, monkeypatch):
        calls = []
        monkeypatch.setattr("core.profile_recovery.capture_snapshot",
                            lambda *a, **kw: calls.append(a) or True)
        return calls

    def test_a_clean_close_after_a_run_that_never_connected_is_refused(self, captured):
        stub = _Stub()
        _note(stub, "INITIALIZING")
        _note(stub, "CLOSED")
        MainWindow._capture_profile_snapshot(stub, "sess123", True, None)
        assert captured == []
        assert any("never connected" in line for line in stub.audits)

    def test_a_run_that_connected_at_some_point_still_snapshots(self, captured):
        stub = _Stub()
        _note(stub, "CONNECTED")
        _note(stub, "CLOSED")        # ordinary quit after a healthy session
        MainWindow._capture_profile_snapshot(stub, "sess123", True, None)
        assert len(captured) == 1

    def test_no_tracker_at_all_behaves_as_before(self, captured):
        """Absent tracker means no connection poll ever ran, which is not
        evidence of anything — refusing there would silently stop snapshotting
        on installs this was never meant to touch."""
        stub = _Stub()
        assert not hasattr(stub, "_profile_health")
        MainWindow._capture_profile_snapshot(stub, "sess123", True, None)
        assert len(captured) == 1


class TestARestoreThatConnectedEarnsAnotherChance:
    """The once-per-launch bound, and the case where it is the wrong answer.

    Measured live on 2026-09-09. The QR-triggered recovery fired, the snapshot
    went back, and the session connected and began syncing at 00:55:51. Eleven
    seconds later a *superseded* session start — a create() from 45 s earlier,
    still counting down its 30 s auth-probe bound against the profile that had
    since been replaced — timed out and force-killed the browser by
    userDataDir, taking the healthy session with it. The relaunch found a
    profile WhatsApp then logged out of, and the launch's only recovery had
    already been spent, so the user reached the pairing dialog with a good
    snapshot still on disk.

    The bound exists to stop a restore loop on a snapshot that does not work.
    A snapshot that reached CONNECTED is not that snapshot.
    """

    def test_the_budget_is_spent_by_a_first_recovery(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr("core.profile_recovery.has_snapshot",
                            lambda *a, **kw: False)
        monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: None)
        MainWindow._recover_suspect_profile(stub)
        assert stub._profile_recovery_attempted is True

    def test_a_connection_gives_it_back(self):
        stub = _Stub()
        stub._profile_recovery_attempted = True
        _note(stub, "CONNECTED")
        assert stub._profile_recovery_attempted is False

    def test_a_second_break_after_that_connection_recovers_again(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr("core.profile_recovery.has_snapshot",
                            lambda *a, **kw: True)
        monkeypatch.setattr("main.threading.Thread",
                            lambda *a, **kw: types.SimpleNamespace(start=lambda: None))
        assert MainWindow._recover_suspect_profile(stub) is True
        assert MainWindow._recover_suspect_profile(stub) is False
        _note(stub, "CONNECTED")
        assert MainWindow._recover_suspect_profile(stub) is True

    def test_without_a_connection_it_still_runs_once(self, monkeypatch):
        """The loop guard is intact: nothing here can re-arm without a
        CONNECTED in between."""
        stub = _Stub()
        monkeypatch.setattr("core.profile_recovery.has_snapshot",
                            lambda *a, **kw: True)
        monkeypatch.setattr("main.threading.Thread",
                            lambda *a, **kw: types.SimpleNamespace(start=lambda: None))
        assert MainWindow._recover_suspect_profile(stub) is True
        for _ in range(5):
            _note(stub, "CLOSED")
            assert MainWindow._recover_suspect_profile(stub) is False


class TestASuccessfulFileLevelRestoreResetsTheQrRepairLatch:
    """issue #203 review finding: _profile_repair_started (set by
    core/websocket_client.py's _handle_unattended_qr(), tracked separately
    from _profile_recovery_attempted so a QR event arriving mid-restore
    cannot mistake "still in flight" for "confirmed nothing to restore") has
    no reset of its own anywhere else -- unlike _profile_recovery_attempted,
    which _note_status_for_profile_health() clears on a CONNECTED reading.

    A snapshot restore can succeed at the file level and still not reach
    CONNECTED (this same method's own generation-climbing branch above exists
    because a restored snapshot sometimes does not hold). Left unreset,
    _profile_repair_started would silently swallow every later unattended-QR
    event for the rest of the launch -- no repair dialog, and, worse, no
    flood halt either, since _handle_unattended_qr()'s early return on this
    flag skips past the halt check too. So the success branch of _restore()
    resets it alongside _unattended_qr_events: the ambiguity the flag exists
    to prevent is over the moment this branch runs (the restore is no longer
    "in flight"), so a later event can only be a genuinely new flood, which
    the ordinary grace/confirm/halt path is built to handle on its own.
    """

    @pytest.fixture
    def stub(self, monkeypatch):
        s = _Stub()
        s._unattended_qr_events = 4
        s._profile_repair_started = True
        monkeypatch.setattr(
            "main.threading.Thread",
            lambda target=None, **kw: types.SimpleNamespace(
                start=lambda: target and target()))
        monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        monkeypatch.setattr("main.api_post", lambda *a, **kw: None)
        monkeypatch.setattr("core.profile_recovery.has_snapshot", lambda *a, **kw: True)
        return s

    def test_a_successful_restore_clears_the_latch(self, stub, monkeypatch):
        monkeypatch.setattr("core.profile_recovery.restore_snapshot",
                            lambda *a, **kw: True)

        MainWindow._recover_suspect_profile(stub)

        assert stub._profile_repair_started is False
        assert stub._unattended_qr_events == 0

    def test_a_failed_restore_leaves_the_latch_alone(self, stub, monkeypatch):
        """on_give_up already surfaces the failure and _auto_repair_dialog_shown
        (set by the caller) takes over routing every later event straight to
        the halt check -- nothing needs _profile_repair_started reset here,
        and this pins that the fix above did not widen to this branch too."""
        monkeypatch.setattr("core.profile_recovery.restore_snapshot",
                            lambda *a, **kw: False)
        gave_up = []

        MainWindow._recover_suspect_profile(stub, on_give_up=lambda: gave_up.append(1))

        assert gave_up == [1]
        assert stub._profile_repair_started is True
