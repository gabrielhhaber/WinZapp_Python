"""Tests for MainWindow._yield_to_in_progress_self_restart().

The gap found by re-reviewing _stop_wpp_server() against the two
wake-from-sleep self-restart cycles (_recovery_restart_active /
_restarting_wpp_session, see _self_inflicted_teardown_expected()): neither
holds _teardown_started_lock, because they are not teardown -- they are a
close+start cycle running on their own thread, independent of any quit. A
user quit landing exactly while one is in flight let _stop_wpp_server()'s
own close-session/flush-wait race the restart's close-session/start-session
call on the SAME session: the restart's start-session can flip status back
to INITIALIZING mid-_wait_for_session_flushed(), starving it of the CLOSED
reading it needs and falling through to a taskkill that risks the exact
"Session Unpaired" corruption this whole mechanism exists to prevent.

_yield_to_in_progress_self_restart() gives a short, bounded grace for an
already-in-progress restart to finish before _stop_wpp_server() proceeds --
narrowing this race, not eliminating it (a restart that outlasts the grace
still races), which is the deliberate, documented tradeoff: a user who
explicitly asked to quit should not wait anywhere near that cycle's own
~30-60s worst case for an invisible background operation.
"""

import threading
import time

from main import MainWindow


class _Stub:
    _yield_to_in_progress_self_restart = MainWindow._yield_to_in_progress_self_restart
    _SELF_RESTART_YIELD_SECONDS = 0.3
    _SELF_RESTART_YIELD_POLL_SECONDS = 0.02

    def __init__(self):
        self._recovery_restart_active = False
        self._restarting_wpp_session = False


class TestNoRestartInProgress:
    def test_returns_immediately_when_neither_flag_is_set(self):
        s = _Stub()
        started = time.monotonic()

        s._yield_to_in_progress_self_restart()

        assert time.monotonic() - started < 0.1


class TestRestartClearsBeforeTheGraceWindow:
    def test_recovery_restart_active_clearing_returns_early(self):
        s = _Stub()
        s._recovery_restart_active = True

        def _clear_soon():
            time.sleep(0.05)
            s._recovery_restart_active = False
        threading.Thread(target=_clear_soon, daemon=True).start()

        started = time.monotonic()
        s._yield_to_in_progress_self_restart()
        elapsed = time.monotonic() - started

        assert elapsed < s._SELF_RESTART_YIELD_SECONDS

    def test_restarting_wpp_session_clearing_returns_early(self):
        s = _Stub()
        s._restarting_wpp_session = True

        def _clear_soon():
            time.sleep(0.05)
            s._restarting_wpp_session = False
        threading.Thread(target=_clear_soon, daemon=True).start()

        started = time.monotonic()
        s._yield_to_in_progress_self_restart()
        elapsed = time.monotonic() - started

        assert elapsed < s._SELF_RESTART_YIELD_SECONDS


class TestRestartOutlivesTheGraceWindow:
    def test_gives_up_after_the_bound_and_still_returns(self):
        """Must never block shutdown indefinitely for an invisible
        background recovery cycle -- proceeds anyway once the short grace
        elapses, logging that the race was not fully closed."""
        s = _Stub()
        s._recovery_restart_active = True  # never cleared

        started = time.monotonic()
        s._yield_to_in_progress_self_restart()
        elapsed = time.monotonic() - started

        assert elapsed >= s._SELF_RESTART_YIELD_SECONDS
        assert elapsed < s._SELF_RESTART_YIELD_SECONDS + 1.0


class TestOnlyCalledWhenNothingElseOwnsTheDeadline:
    """_stop_wpp_server(budget=...) is WM_ENDSESSION's own tightly-bounded
    call (Windows kills an unresponsive app in ~5s). This yield's own wait
    sits OUTSIDE that budget, so honoring it there could add its full
    _SELF_RESTART_YIELD_SECONDS on top of a 4s budget -- comfortably past
    what triggers a "Not Responding" kill mid-shutdown. Skipped entirely
    when a budget is given, rather than budgeted, so a tight budget's real
    phases (close-session, flush poll) are never starved by it."""

    def test_the_call_is_guarded_on_budget_is_none(self):
        import inspect
        from main import MainWindow
        source = inspect.getsource(MainWindow._stop_wpp_server)
        call_at = source.index("_yield_to_in_progress_self_restart()")
        guard = source[:call_at]
        assert "if budget is None:" in guard[-500:]
