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
* a restore that succeeded has to hand back the QR-flood allowance the burst
  that triggered it already spent, or the flood halt latches over a profile
  that was just repaired and nothing ever starts it again;
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
        self.profile_released = False

    # Bound from the real class: the generation ladder decides *which*
    # snapshot goes back, so a stub that faked it would let the wiring drift
    # from the module its own tests cover.
    _PROFILE_RECOVERY_GENERATION_KEY = MainWindow._PROFILE_RECOVERY_GENERATION_KEY
    _profile_recovery_generation = MainWindow._profile_recovery_generation
    _set_profile_recovery_generation = MainWindow._set_profile_recovery_generation

    def browser_payload_blocks_startup(self):
        """A healthy browser by default. _recover_suspect_profile() now refuses
        outright when the bundled Chromium cannot start, because a failed
        browser launch produces exactly the "died without connecting" the
        tracker counts — see tests/test_broken_browser_payload.py."""
        return None, None

    def wait_for_profile_release(self, session_name, timeout=20.0):
        """Only reached by the tests that let the restore thread run; every
        other one is refused before this by has_snapshot."""
        self.profile_released = True
        return True

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


class _InlineThread:
    """The restore runs on its own daemon thread in production; nothing here
    could observe what it did otherwise."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


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

    The re-arm itself (clearing _profile_recovery_attempted and the
    generation ladder) used to happen right here, inside
    _note_status_for_profile_health(), triggered by the bare status string.
    It moved to _set_wa_connected()'s own "connection just came back up"
    branch (issue #202 — a CONNECTED the live isConnected() probe was about
    to refuse could re-arm recovery without the QR-flood counter it must
    stay in lockstep with resetting alongside it; see
    tests/test_qr_flood_rearm_counter.py for that half). What is pinned here
    is only _recover_suspect_profile()'s own once-per-launch bound —
    _profile_recovery_attempted is set directly to stand in for whichever
    caller re-armed it.
    """

    def test_the_budget_is_spent_by_a_first_recovery(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr("core.profile_recovery.has_snapshot",
                            lambda *a, **kw: False)
        monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: None)
        MainWindow._recover_suspect_profile(stub)
        assert stub._profile_recovery_attempted is True

    def test_a_connected_status_alone_no_longer_rearms_it_here(self):
        """Regression guard for the move described in the class docstring:
        this status-observer path must not duplicate the re-arm any more,
        or a CONNECTED the probe later refuses would re-arm recovery again
        exactly as issue #202 described — just from this call site instead
        of the old one."""
        stub = _Stub()
        stub._profile_recovery_attempted = True
        _note(stub, "CONNECTED")
        assert stub._profile_recovery_attempted is True

    def test_a_second_break_after_that_connection_recovers_again(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr("core.profile_recovery.has_snapshot",
                            lambda *a, **kw: True)
        monkeypatch.setattr("main.threading.Thread",
                            lambda *a, **kw: types.SimpleNamespace(start=lambda: None))
        assert MainWindow._recover_suspect_profile(stub) is True
        assert MainWindow._recover_suspect_profile(stub) is False
        stub._profile_recovery_attempted = False  # what _set_wa_connected() now does
        assert MainWindow._recover_suspect_profile(stub) is True

    def test_without_a_connection_it_still_runs_once(self, monkeypatch):
        """The loop guard is intact: nothing here can re-arm on its own."""
        stub = _Stub()
        monkeypatch.setattr("core.profile_recovery.has_snapshot",
                            lambda *a, **kw: True)
        monkeypatch.setattr("main.threading.Thread",
                            lambda *a, **kw: types.SimpleNamespace(start=lambda: None))
        assert MainWindow._recover_suspect_profile(stub) is True
        for _ in range(5):
            _note(stub, "CLOSED")
            assert MainWindow._recover_suspect_profile(stub) is False


class TestASuccessfulRestoreGivesBackTheQrFloodAllowance:
    """The restore thread's own `self._unattended_qr_events = 0`.

    The burst of QR/pairing codes that brought us here was minted by the
    profile now moved aside, so counting it against
    WebSocketClient._UNATTENDED_QR_LIMIT halts a session that is about to be
    fine — and the halt is the expensive half: _halt_unattended_qr_session()
    latches _qr_flood_halted, so check_wa_connection_http() never issues
    another /start-session and the profile just restored is never started at
    all. The user is left offline with no route back, which is exactly the
    "worse than the flood" outcome _handle_unattended_qr() describes.

    Reachable in ordinary operation: a paired install's counter normally
    stands at 1 or 2 by the time the restore lands, because the reset happens
    behind close-session, wait_for_profile_release and a copy of a few hundred
    megabytes while codes keep arriving every ~20-30 s.
    tests/test_qrcode_auto_repair_dialog.py models that timing from the QR
    side (_FakeMainWindow.finish_restore()); this is the production line it
    stands in for.
    """

    @pytest.fixture
    def restoring(self, monkeypatch):
        """Everything between the trigger and the reset, made synchronous.
        Returns a setter for the one outcome the two tests differ on."""
        monkeypatch.setattr("core.profile_recovery.has_snapshot", lambda *a, **kw: True)
        monkeypatch.setattr("main.api_post", lambda *a, **kw: None)
        monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        monkeypatch.setattr("main.threading.Thread", _InlineThread)

        def _restore_succeeds(restored):
            monkeypatch.setattr("core.profile_recovery.restore_snapshot",
                                lambda *a, **kw: restored)
        return _restore_succeeds

    def test_the_flood_counter_is_cleared_once_the_restore_succeeded(self, restoring):
        restoring(True)
        stub = _Stub()
        stub._unattended_qr_events = 2      # one code short of the halt

        assert MainWindow._recover_suspect_profile(stub) is True

        assert stub.profile_released
        assert stub._unattended_qr_events == 0
        assert "profile_restored_from_snapshot" in stub.announced

    def test_a_failed_restore_leaves_the_flood_counter_alone(self, restoring):
        """Nothing was moved aside, so the codes counted so far are real ones
        and the ceiling on them has to keep applying — the halt is the only
        thing that stops WhatsApp being asked for more."""
        restoring(False)
        stub = _Stub()
        stub._unattended_qr_events = 2

        MainWindow._recover_suspect_profile(stub)

        assert stub._unattended_qr_events == 2
        assert "profile_corrupted_repair_needed" in stub.announced
