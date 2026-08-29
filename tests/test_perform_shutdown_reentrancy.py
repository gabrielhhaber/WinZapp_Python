"""Tests for _perform_shutdown()'s return value and _teardown_complete_event.

The gap found by re-reviewing real_exit()'s _teardown() and _ipc_quit()
against the new _teardown_started_lock (added to fix a separate TOCTOU race
between _perform_shutdown() and _on_end_session()): both callers used to
call self._terminate_process() unconditionally in a `finally`, regardless
of whether _perform_shutdown() actually did the teardown work or found it
already owned by another concurrent caller (a second call into this same
method, or _on_end_session() running on another thread). Self-terminating
in that second case could os._exit() the process while the OTHER, WINNING
call was still genuinely mid _stop_wpp_server() -- killing Chrome before it
finished flushing, the exact LevelDB-corruption failure mode this whole
mechanism exists to prevent.

_perform_shutdown() now returns True only when THIS call performed the
teardown, and always sets _teardown_complete_event in a `finally` (even on
an unexpected exception) so a caller that got False back has something
bounded to wait on before it is safe to terminate.

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so _perform_shutdown() is exercised as a plain function against a
minimal stub -- same approach as tests/test_shutdown_suppresses_offline.py.
Every attribute _perform_shutdown() touches through hasattr()/getattr() is
simply omitted from the stub so those branches no-op; only the
unconditionally-called ones (_stop_wpp_server, _flush_pending_debounced_saves)
need a stand-in.
"""

import threading

import pytest

from main import MainWindow


class _Stub:
    _perform_shutdown = MainWindow._perform_shutdown

    def __init__(self, raise_in_stop_wpp_server=False):
        self._teardown_started_lock = threading.Lock()
        self._teardown_complete_event = threading.Event()
        self._shutting_down = False
        self.stop_wpp_server_calls = 0
        self.flush_calls = 0
        self._raise_in_stop_wpp_server = raise_in_stop_wpp_server

    def _stop_wpp_server(self):
        self.stop_wpp_server_calls += 1
        if self._raise_in_stop_wpp_server:
            raise RuntimeError("boom")

    def _flush_pending_debounced_saves(self):
        self.flush_calls += 1


class TestFirstCallOwnsTeardown:
    def test_returns_true_and_does_the_work(self):
        s = _Stub()
        assert s._perform_shutdown() is True
        assert s.stop_wpp_server_calls == 1
        assert s.flush_calls == 1
        assert s._shutting_down is True

    def test_sets_the_completion_event(self):
        s = _Stub()
        s._perform_shutdown()
        assert s._teardown_complete_event.is_set()


class TestSecondCallDefersToTheFirst:
    def test_returns_false_and_does_not_repeat_the_work(self):
        s = _Stub()
        s._shutting_down = True  # simulates another path having already started

        result = s._perform_shutdown()

        assert result is False
        assert s.stop_wpp_server_calls == 0
        assert s.flush_calls == 0

    def test_does_not_set_the_completion_event_itself(self):
        """The deferring call must not fake-signal completion -- only the
        call that actually did the work may set it."""
        s = _Stub()
        s._shutting_down = True

        s._perform_shutdown()

        assert not s._teardown_complete_event.is_set()


class TestCompletionEventIsSetEvenOnException:
    def test_an_exception_inside_teardown_still_sets_the_event(self):
        """A caller waiting on _teardown_complete_event must not be stranded
        for its full timeout just because something inside teardown raised."""
        s = _Stub(raise_in_stop_wpp_server=True)

        with pytest.raises(RuntimeError):
            s._perform_shutdown()

        assert s._teardown_complete_event.is_set()
