"""Tests for MainWindow._on_end_session()'s self-healing _shutting_down reset.

_shutting_down is never reset anywhere else in the codebase, and every
logout-detection guard added against the self-inflicted-close bug
(on_connection_update's "close" branch, on_wpp_status_find,
check_wa_connection_http's CLOSED auto-start) now trusts it permanently for
the rest of the process's life once set. _on_end_session() sets it on
WM_ENDSESSION (Windows telling the app the machine is shutting down) --  but
per Windows' own documented semantics WM_ENDSESSION's bEnding can in
principle still be FALSE (another app vetoed a shutdown this one already
got as far as receiving notice of). If that ever happens and the process
keeps running, _shutting_down would otherwise stay True forever, silently
disabling real logout detection for the rest of the session.

WebSocketClient/MainWindow are not directly involved here; only
_on_end_session and the timer it arms are exercised, bound onto a stub (no
wx.App / real Windows session needed).
"""

import threading
import time

from main import MainWindow


class _Stub:
    _on_end_session = MainWindow._on_end_session
    _END_SESSION_UNSTICK_SECONDS = 0.05
    _WINDOWS_SHUTDOWN_BUDGET = MainWindow._WINDOWS_SHUTDOWN_BUDGET

    def __init__(self, already_shutting_down=False):
        self._shutting_down = already_shutting_down
        self._teardown_started_lock = threading.Lock()
        self._teardown_complete_event = threading.Event()
        self.stop_wpp_server_calls = 0
        self.flush_calls = 0

    def _shutdown_audit(self, msg):
        pass

    def _stop_wpp_server(self, budget=None):
        self.stop_wpp_server_calls += 1

    def _flush_pending_debounced_saves(self):
        self.flush_calls += 1

    def GetHandle(self):
        return 0


class _FakeEvent:
    def __init__(self):
        self.skipped = False

    def Skip(self):
        self.skipped = True


class TestShuttingDownIsSetImmediately:
    def test_flag_set_and_stop_wpp_server_called(self):
        s = _Stub()
        event = _FakeEvent()

        s._on_end_session(event)

        assert s._shutting_down is True
        assert s.stop_wpp_server_calls == 1
        assert event.skipped is True

    def test_signals_teardown_complete_when_it_owns_teardown(self):
        """A real_exit()/_ipc_quit() call that lost the lock race waits on
        this event before self-terminating -- if this path never sets it,
        that caller sits out its full 60-85s bound instead of noticing this
        one already finished in a few milliseconds."""
        s = _Stub()

        s._on_end_session(_FakeEvent())

        assert s._teardown_complete_event.is_set()


class TestDoesNotDoubleTeardownWhenAlreadyInProgress:
    """The gap found by re-reviewing this method against _perform_shutdown():
    both used to set/check _shutting_down without any shared lock, so a
    local quit (real_exit()'s background thread) racing a WM_ENDSESSION (or
    an IPC "quit" from another account, also delivered on the wx main
    thread) could see _shutting_down still False on both sides and BOTH
    call _stop_wpp_server() concurrently -- two threads racing on the same
    self.wpp_process and taskkill target. _teardown_started_lock closes
    that: whichever caller wins the lock proceeds normally, the other
    (finding _shutting_down already True inside the lock) must not repeat
    the teardown or arm a second unstick timer."""

    def test_stop_wpp_server_is_not_called_again_when_teardown_already_started(self):
        s = _Stub(already_shutting_down=True)
        event = _FakeEvent()

        s._on_end_session(event)

        assert s.stop_wpp_server_calls == 0
        assert s.flush_calls == 0
        assert event.skipped is True

    def test_does_not_signal_completion_for_teardown_it_does_not_own(self):
        """This path only skips out of the way -- the OTHER path (still
        genuinely running) is the one whose own finish should set the event,
        not this early return."""
        s = _Stub(already_shutting_down=True)

        s._on_end_session(_FakeEvent())

        assert not s._teardown_complete_event.is_set()

    def test_shutting_down_stays_true_and_no_unstick_timer_is_armed(self):
        """Arming a second unstick timer here would, after
        _END_SESSION_UNSTICK_SECONDS, reset _shutting_down back to False
        while the OTHER path's teardown might still genuinely be running --
        reopening the exact self-inflicted-logout window this flag exists
        to close."""
        s = _Stub(already_shutting_down=True)
        s._END_SESSION_UNSTICK_SECONDS = 0.05

        s._on_end_session(_FakeEvent())
        assert s._shutting_down is True

        time.sleep(0.3)  # well past the (would-be) unstick window

        assert s._shutting_down is True, (
            "no second unstick timer should have been armed by this call"
        )


class TestSelfHealingUnstick:
    def test_flag_clears_itself_if_process_is_still_running_later(self):
        s = _Stub()
        s._on_end_session(_FakeEvent())
        assert s._shutting_down is True

        # Give the background threading.Timer time to fire (armed for
        # _END_SESSION_UNSTICK_SECONDS = 0.05s on the stub).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and s._shutting_down:
            time.sleep(0.01)

        assert s._shutting_down is False, (
            "a Windows shutdown that never actually completed must not leave "
            "logout detection permanently disabled for the rest of the session"
        )

    def test_unstick_also_clears_the_stale_completion_signal(self):
        """Left set, a LATER genuinely-new teardown's loser would see this
        abandoned attempt's event already set and self-terminate immediately,
        believing that teardown finished when it may still be mid
        _stop_wpp_server()."""
        s = _Stub()
        s._on_end_session(_FakeEvent())
        assert s._teardown_complete_event.is_set()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and s._shutting_down:
            time.sleep(0.01)

        assert not s._teardown_complete_event.is_set()

    def test_a_normal_real_exit_never_needs_the_unstick(self):
        """Sanity check on the assumption the unstick relies on: a real
        shutdown's own teardown budget is nowhere near the 60s production
        default, so in the ordinary case the process is long gone before
        this would ever fire. Guards the constant itself from silently
        shrinking below what a legitimate slow close-session flush needs."""
        assert MainWindow._END_SESSION_UNSTICK_SECONDS >= (
            MainWindow._WPP_GRACEFUL_STOP_SECONDS
            + MainWindow._SHUTDOWN_FLUSH_TIMEOUT
            + MainWindow._TOKEN_PERSIST_GRACE_SECONDS
        )
