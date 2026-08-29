"""WM_ENDSESSION must not hand the process to Windows mid-teardown.

PR #141 correctly locked the _shutting_down check-and-set so a local quit, an
IPC quit and WM_ENDSESSION can no longer run _stop_wpp_server() concurrently.
But its losing branch — "teardown already started elsewhere" — destroyed the
shutdown block reason and returned at once, which is precisely what tells
Windows it may terminate now.

The damage that causes is documented in CLAUDE.md: a run whose
shutdown_audit.log shows STARTUP with no _stop_wpp_server before it was killed
rather than closed, WhatsApp's auth state never reached userDataDir, and the
profile comes back unusable — the session-loss symptom this whole PR is about.

The old code at least ran its own budgeted _stop_wpp_server() and flushed
something. The fix is to wait on the winning path's _teardown_complete_event,
bounded by the same Windows budget.
"""

import inspect
import threading

import pytest

from main import MainWindow


class _Event:
    """Records what it was asked to wait for."""

    def __init__(self, result=True):
        self._result = result
        self.waited_with = None

    def wait(self, timeout=None):
        self.waited_with = timeout
        return self._result

    def set(self):
        pass

    def clear(self):
        pass


class _Stub:
    _WINDOWS_SHUTDOWN_BUDGET = 4
    _END_SESSION_UNSTICK_SECONDS = 60.0
    _TEARDOWN_OWNED_ELSEWHERE_WAIT_SECONDS = 80.0
    stopped_with = None

    def __init__(self, already_tearing_down):
        self._teardown_started_lock = threading.Lock()
        self._teardown_complete_event = _Event()
        self._shutting_down = already_tearing_down
        self._audits = []

    def GetHandle(self):
        raise RuntimeError("no real window in a test")

    def _shutdown_audit(self, msg):
        self._audits.append(msg)

    def _stop_wpp_server(self, budget=None):
        self.stopped_with = budget

    def _flush_pending_debounced_saves(self):
        pass


class _Evt:
    def __init__(self):
        self.skipped = False

    def Skip(self):
        self.skipped = True


class TestTeardownOwnedElsewhere:
    def test_it_waits_for_the_owning_teardown_before_letting_windows_kill_us(self):
        stub = _Stub(already_tearing_down=True)
        evt = _Evt()

        MainWindow._on_end_session(stub, evt)

        assert stub._teardown_complete_event.waited_with == stub._WINDOWS_SHUTDOWN_BUDGET, (
            "the losing branch returned immediately — Windows then terminates "
            "the process while the owning path is still mid _stop_wpp_server()"
        )
        assert evt.skipped
        # It must NOT start a competing teardown of its own; that is exactly
        # what the lock is there to prevent.
        assert stub.stopped_with is None

    def test_the_owner_still_does_its_own_budgeted_teardown(self):
        stub = _Stub(already_tearing_down=False)
        evt = _Evt()

        MainWindow._on_end_session(stub, evt)

        assert stub.stopped_with == stub._WINDOWS_SHUTDOWN_BUDGET
        assert stub._shutting_down is True
        assert evt.skipped


class TestUnstickTimer:
    def test_it_mutates_the_shutdown_flags_under_the_lock(self):
        """_shutting_down and _teardown_complete_event are mutated under
        _teardown_started_lock everywhere else. Unlocked, this timer can clear
        an event belonging to a LATER teardown that genuinely finished,
        stranding that teardown's loser for its whole 80s wait."""
        src = inspect.getsource(MainWindow._on_end_session)
        unstick = src[src.index("def _unstick_if_still_running"):]
        assert "with self._teardown_started_lock:" in unstick.split("try:")[0]

    def test_the_unstick_window_is_shorter_than_the_wait_it_frees(self):
        """A loser blocked on the event must never outlive the timer that
        would have freed it."""
        assert (MainWindow._END_SESSION_UNSTICK_SECONDS
                < MainWindow._TEARDOWN_OWNED_ELSEWHERE_WAIT_SECONDS)
