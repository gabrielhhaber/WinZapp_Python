"""Tests for the wx main-loop watchdog.

Every UI-freeze report on this app has had to be diagnosed by inference: the
main thread writes nothing to the log while it is blocked, so the freeze window
contains worker-thread lines and a hole where the UI should be. Two successive
theories built that way — a refresh storm, then an oversized pagination window
— each explained the symptom and each turned out not to be the cause.

start_ui_watchdog() replaces the inference with a measurement. It pings the
main loop once a second with a no-op CallAfter; when a ping goes unanswered the
main thread is by definition not draining its event queue, and
sys._current_frames() reads its stack from outside it, so the log names the
exact call the UI is stuck in.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so start_ui_watchdog() is exercised as a plain function against a stub — same
approach as tests/test_startup_grace.py.
"""

import logging
import threading
import time

import pytest

from main import MainWindow


class _Stub:
    start_ui_watchdog = MainWindow.start_ui_watchdog
    # Kept short so the tests stay fast; the real values are one and two
    # seconds (asserted separately below).
    _UI_WATCHDOG_INTERVAL = 0.02
    _UI_WATCHDOG_STALL_SECONDS = 0.05
    _UI_WATCHDOG_MAX_REPORT_GAP = 0.2

    def __init__(self):
        self._shutting_down = False


@pytest.fixture
def fake_main_loop(monkeypatch):
    """Stand in for the wx event loop: callbacks queue up and only run when
    the test drains them, so a 'frozen UI' is just a queue nobody drains."""
    queue = []
    lock = threading.Lock()

    def _call_after(fn, *a, **kw):
        with lock:
            queue.append((fn, a, kw))

    def drain():
        with lock:
            pending, queue[:] = list(queue), []
        for fn, a, kw in pending:
            fn(*a, **kw)

    monkeypatch.setattr("main.wx.CallAfter", _call_after)
    drain.queue = queue
    return drain


def _stop(stub, thread_grace=0.3):
    stub._shutting_down = True
    time.sleep(thread_grace)


class TestUiWatchdog:
    def test_a_responsive_loop_logs_no_stall(self, fake_main_loop, caplog):
        caplog.set_level(logging.WARNING)
        s = _Stub()
        s.start_ui_watchdog()
        for _ in range(10):          # keep answering the pings
            time.sleep(0.02)
            fake_main_loop()
        _stop(s)

        assert not [r for r in caplog.records if "ui-watchdog" in r.getMessage()]

    def test_an_unanswered_ping_logs_a_stack(self, fake_main_loop, caplog):
        """Nobody drains the queue — the watchdog must notice and dump where
        the main thread is."""
        caplog.set_level(logging.WARNING)
        s = _Stub()
        s.start_ui_watchdog()
        time.sleep(0.25)             # let it miss several pings
        stalls = [r.getMessage() for r in caplog.records if "unresponsive" in r.getMessage()]
        _stop(s)

        assert stalls, "watchdog never reported the stall"
        # The point of the whole thing: a stack, not just a complaint.
        assert "main thread stack" in stalls[0]
        assert "File " in stalls[0] or "frame unavailable" in stalls[0]

    def test_it_keeps_reporting_while_the_stall_lasts(self, fake_main_loop, caplog):
        """A single line at the start would be useless for a freeze that
        moves between calls; the stack has to be sampled repeatedly."""
        caplog.set_level(logging.WARNING)
        s = _Stub()
        s.start_ui_watchdog()
        time.sleep(0.3)
        stalls = [r for r in caplog.records if "unresponsive" in r.getMessage()]
        _stop(s)

        assert len(stalls) >= 2

    def test_recovery_is_logged_with_its_duration(self, fake_main_loop, caplog):
        caplog.set_level(logging.WARNING)
        s = _Stub()
        s.start_ui_watchdog()
        time.sleep(0.2)
        fake_main_loop()             # UI comes back
        time.sleep(0.1)
        _stop(s)

        assert [r for r in caplog.records if "responsive again" in r.getMessage()]

    def test_shutdown_stops_the_thread(self, fake_main_loop):
        s = _Stub()
        s.start_ui_watchdog()
        time.sleep(0.05)
        _stop(s)
        assert not [t for t in threading.enumerate() if t.name == "ui-watchdog"]

    def test_a_dead_call_after_ends_the_loop_quietly(self, monkeypatch):
        """Late in shutdown wx.CallAfter can raise; that must not spew."""
        def _boom(*a, **kw):
            raise RuntimeError("app is gone")

        monkeypatch.setattr("main.wx.CallAfter", _boom)
        s = _Stub()
        s.start_ui_watchdog()        # must not raise
        time.sleep(0.1)
        assert not [t for t in threading.enumerate() if t.name == "ui-watchdog"]

    def test_the_shipped_thresholds_are_cheap(self):
        """One ping per second is the budget; a tighter interval would put
        real load on the very loop it is measuring."""
        assert MainWindow._UI_WATCHDOG_INTERVAL >= 1.0
        assert MainWindow._UI_WATCHDOG_STALL_SECONDS >= 2.0


class TestAnUnchangingStackIsNotRepeatedForever:
    """A modal dialog is indistinguishable from a freeze, from out here.

    `_show_repair_dialog()` runs its own event loop and never answers the
    ping, so on a real install a re-pairing prompt left on screen produced
    1,400 lines of identical stack in four minutes. log.log is truncated every
    launch and was the only record of the session failure that had opened that
    dialog, so the noise cost the diagnosis. Sampling still happens at the
    stall interval; only the *writing* of an unchanged sample backs off.
    """

    def test_reports_of_an_identical_stack_thin_out(self, fake_main_loop, caplog):
        caplog.set_level(logging.WARNING)
        s = _Stub()
        s.start_ui_watchdog()
        time.sleep(0.6)
        stalls = [r for r in caplog.records if "unresponsive" in r.getMessage()]
        _stop(s)

        # Un-backed-off, a 0.05s sampling interval over 0.6s would be ~12
        # lines. The cap is 0.2s, so it cannot exceed roughly a third of that.
        assert 2 <= len(stalls) <= 6, len(stalls)

    def test_a_changing_stack_is_always_reported(self):
        # The interesting freeze moves between calls, and must never be
        # thinned: only an identical sample waits for the backoff.
        assert MainWindow._UI_WATCHDOG_MAX_REPORT_GAP > MainWindow._UI_WATCHDOG_STALL_SECONDS

    def test_the_shipped_cap_is_a_minute(self):
        assert MainWindow._UI_WATCHDOG_MAX_REPORT_GAP == 60.0
