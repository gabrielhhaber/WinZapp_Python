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

    def _shutdown_audit(self, msg):
        self.audits.append(msg)

    def output(self, text, interrupt=False):
        self.announced.append(text)

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
            lambda *a: calls.append(a) or False)
        monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: None)
        MainWindow._recover_suspect_profile(stub)
        MainWindow._recover_suspect_profile(stub)
        assert len(calls) == 1

    def test_without_a_token_nothing_is_attempted(self, monkeypatch):
        stub = _Stub(token="")
        monkeypatch.setattr(
            "core.profile_recovery.has_snapshot",
            lambda *a: pytest.fail("should not have looked for a snapshot"))
        MainWindow._recover_suspect_profile(stub)


class TestWithNoSnapshotTheUserIsTold:
    """The dead end. It only happens while already offline, where
    _set_wa_connected(False, ...) has hit its no-change early return and said
    nothing — so silence here is a user with no idea what to do."""

    def test_the_message_is_announced(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr("core.profile_recovery.has_snapshot", lambda *a: False)
        monkeypatch.setattr("main.wx.CallAfter",
                            lambda fn, *a, **kw: fn(*a, **kw))
        MainWindow._recover_suspect_profile(stub)
        assert "profile_corrupted_repair_needed" in stub.announced

    def test_the_suspicion_is_audited_either_way(self, monkeypatch):
        """shutdown_audit.log is the only file that survives the next launch,
        and this is exactly the diagnosis a user's next report needs."""
        stub = _Stub()
        monkeypatch.setattr("core.profile_recovery.has_snapshot", lambda *a: False)
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
