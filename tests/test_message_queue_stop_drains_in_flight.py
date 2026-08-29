"""Tests for MessageQueue.stop() waiting (briefly, boundedly) for an
in-flight send to finish before returning.

Reported gap (found by re-reviewing the shutdown path, not from a live
report): _perform_shutdown() calls message_queue.stop() BEFORE
_stop_wpp_server(), which goes on to close-session and eventually taskkill
the same Node process a worker thread might be mid-HTTP-request to. Before
this fix, stop() only signalled the workers and returned immediately --
so a send that was seconds from succeeding could get its connection cut by
the session close/kill that follows right after, turning a legitimate send
into an ambiguous/lost one for no reason. Bounded (not indefinite) so a
stuck/huge upload can never block Ctrl+Alt+Shift+Q forever.
"""

import pathlib
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

import core.message_queue as message_queue
from core.message_queue import MessageQueue, PendingMessage


@pytest.fixture(autouse=True)
def direct_call_after(monkeypatch):
    monkeypatch.setattr(
        message_queue.wx, "CallAfter",
        lambda callback, *args: callback(*args), raising=False,
    )


class _MainWindow:
    offline_mode = False
    _wa_connected = True

    def __init__(self):
        self._own_sent_ids_lock = threading.Lock()
        self._own_sent_ids = set()
        self.send_started = threading.Event()
        self.release_send = threading.Event()

    def send_text_message(self, *_args, **_kwargs):
        self.send_started.set()
        self.release_send.wait(timeout=5)
        return "text-id"

    def _on_message_sent(self, *_args):
        pass

    def _on_message_failed(self, *_args):
        pass

    def _on_message_unconfirmed(self, *_args):
        pass

    def _on_cancelled_message_dropped(self, *_args):
        pass


class TestStopWaitsForInFlightSend:
    def test_stop_blocks_until_the_in_flight_send_actually_finishes(self):
        main_window = _MainWindow()
        queue = MessageQueue(main_window)
        queue._STOP_DRAIN_SECONDS = 2.0
        queue._STOP_DRAIN_POLL_SECONDS = 0.02
        try:
            queue.enqueue(PendingMessage("t1", "chat-a", text="hello"))
            assert main_window.send_started.wait(timeout=1)

            # Release the in-flight send a little after stop() is called, so a
            # stop() that returned immediately (the old behaviour) would win
            # the race and this assertion would catch it.
            def _release_soon():
                time.sleep(0.3)
                main_window.release_send.set()
            threading.Thread(target=_release_soon, daemon=True).start()

            started = time.monotonic()
            queue.stop()
            elapsed = time.monotonic() - started

            assert elapsed >= 0.25, "stop() must not return before the send finished"
            assert elapsed < queue._STOP_DRAIN_SECONDS, "must return as soon as drained, not wait out the whole budget"
        finally:
            main_window.release_send.set()

    def test_stop_gives_up_after_the_bound_and_still_returns(self):
        """A send that never finishes (stuck upload) must not block shutdown
        forever -- stop() must return once the bound elapses regardless."""
        main_window = _MainWindow()
        queue = MessageQueue(main_window)
        queue._STOP_DRAIN_SECONDS = 0.3
        queue._STOP_DRAIN_POLL_SECONDS = 0.02
        try:
            queue.enqueue(PendingMessage("t1", "chat-a", text="hello"))
            assert main_window.send_started.wait(timeout=1)

            started = time.monotonic()
            queue.stop()  # release_send is never set
            elapsed = time.monotonic() - started

            assert elapsed >= queue._STOP_DRAIN_SECONDS
            assert elapsed < queue._STOP_DRAIN_SECONDS + 1.0, "must not overshoot the bound by much"
        finally:
            main_window.release_send.set()

    def test_stop_returns_immediately_when_nothing_is_in_flight(self):
        main_window = _MainWindow()
        queue = MessageQueue(main_window)
        queue._STOP_DRAIN_SECONDS = 5.0
        started = time.monotonic()
        queue.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
