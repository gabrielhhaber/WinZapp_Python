"""Holding the outgoing queue while the session is deliberately closed.

A backup taken with WinZapp open (Settings > Cópia de segurança) closes the
WhatsApp session for as long as the copy takes, which can be minutes.

Two halves. The queue attempts nothing while held, so a message written during
the backup waits instead of spending its retries against a session that is
deliberately gone. And a request already on the wire is waited for rather than
cut off by the close: that one ends as an *ambiguous* outcome, which the queue
never retries and reports to the user as a send it could not confirm.

Nothing the user sends may be lost, failed or left unconfirmed because WinZapp
chose that moment to close the session.
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

    def __init__(self, block=False):
        self._own_sent_ids_lock = threading.Lock()
        self._own_sent_ids = set()
        self.sends = []
        self.failed = []
        self.unconfirmed = []
        self.sent = threading.Event()
        self.send_started = threading.Event()
        self.release_send = threading.Event()
        self._block = block

    def send_text_message(self, jid, text, **kwargs):
        self.sends.append(jid)
        self.send_started.set()
        if self._block:
            self.release_send.wait(timeout=5)
        return "real-id"

    def _on_message_sent(self, *_args):
        self.sent.set()

    # Recorded, never raised: these run inside the worker thread's own try,
    # which would turn an AssertionError into one more failure count and the
    # test would pass without ever checking anything.
    def _on_message_failed(self, *args):
        self.failed.append(args)

    def _on_message_unconfirmed(self, *args):
        self.unconfirmed.append(args)

    def _on_cancelled_message_dropped(self, *_args):
        pass


def _queue(main_window):
    queue = MessageQueue(main_window)
    queue._RETRY_INTERVAL = 0.05
    queue._STOP_DRAIN_POLL_SECONDS = 0.01
    return queue


def _message(local_id="m1"):
    return PendingMessage(local_id=local_id, jid="5511@s.whatsapp.net", text="oi")


class TestHold:
    def test_a_held_queue_attempts_nothing_and_keeps_the_message(self):
        main_window = _MainWindow()
        queue = _queue(main_window)
        try:
            queue.hold()
            msg = _message()
            queue.enqueue(msg)
            time.sleep(0.3)      # several retry cycles

            assert main_window.sends == []
            assert queue.has_work()
            assert msg.fail_count == 0
            assert queue.is_held()
            assert main_window.failed == [] and main_window.unconfirmed == []
        finally:
            queue.stop()

    def test_releasing_sends_what_was_waiting_right_away(self):
        main_window = _MainWindow()
        queue = _queue(main_window)
        try:
            queue.hold()
            queue.enqueue(_message())
            time.sleep(0.2)
            assert main_window.sends == []

            queue.release()

            assert main_window.sent.wait(timeout=5)
            assert main_window.sends == ["5511@s.whatsapp.net"]
            assert not queue.has_work()
            assert not queue.is_held()
        finally:
            queue.stop()

    def test_a_message_written_while_held_waits_instead_of_failing(self):
        """The user presses Enter during the backup: it goes out afterwards."""
        main_window = _MainWindow()
        queue = _queue(main_window)
        try:
            queue.hold()
            queue.enqueue(_message("written-during-backup"))
            time.sleep(0.3)
            queue.release()

            assert main_window.sent.wait(timeout=5)
            assert main_window.sends == ["5511@s.whatsapp.net"]
            assert main_window.failed == [] and main_window.unconfirmed == []
        finally:
            queue.stop()

    def test_release_without_a_hold_changes_nothing(self):
        main_window = _MainWindow()
        queue = _queue(main_window)
        try:
            queue.release()
            assert not queue.is_held()
        finally:
            queue.stop()


class _RacingMainWindow(_MainWindow):
    """Holds the queue in the one window that matters: after the worker has
    passed the loop-level hold check and before it claims the message."""

    def __init__(self):
        super().__init__()
        self.queue = None
        self.reads = 0

    @property
    def _wa_connected(self):
        # Read once per cycle and again per message; only the second read of a
        # cycle that has something to send sits inside the window.
        if self.queue is not None and self.queue.has_work():
            self.reads += 1
            if self.reads == 2:
                self.queue.hold()
        return True


class TestWaitUntilIdle:
    def test_true_when_nothing_is_on_the_wire(self):
        main_window = _MainWindow()
        queue = _queue(main_window)
        try:
            assert queue.wait_until_idle(0.2) is True
        finally:
            queue.stop()

    def test_false_while_a_send_is_still_in_flight(self):
        main_window = _MainWindow(block=True)
        queue = _queue(main_window)
        try:
            queue.enqueue(_message())
            assert main_window.send_started.wait(timeout=5)

            assert queue.wait_until_idle(0.2) is False

            main_window.release_send.set()
            assert queue.wait_until_idle(5) is True
        finally:
            main_window.release_send.set()
            queue.stop()

    def test_a_hold_landing_just_before_the_send_is_still_seen(self):
        """The drain must never answer "nothing on the wire" to a caller about
        to close the session while a worker is one step from POSTing: that send
        dies with the browser as an ambiguous outcome nothing ever retries.

        What this proves is the two assertions after the drain — no send
        happened and the message is still queued. The drain call itself answers
        on its first poll either way; it is here because it is what the caller
        does, not as the guard.
        """
        main_window = _RacingMainWindow()
        queue = _queue(main_window)
        main_window.queue = queue
        try:
            queue.enqueue(_message())

            assert queue.wait_until_idle(3) is True
            time.sleep(0.3)      # several cycles after the drain answered

            assert main_window.sends == []
            assert queue.has_work()
            assert main_window.failed == [] and main_window.unconfirmed == []
        finally:
            queue.stop()

    def test_holding_does_not_cut_off_a_send_already_on_the_wire(self):
        """Nothing can recall a request in flight, so it is waited for."""
        main_window = _MainWindow(block=True)
        queue = _queue(main_window)
        try:
            queue.enqueue(_message())
            assert main_window.send_started.wait(timeout=5)
            queue.hold()
            main_window.release_send.set()

            assert queue.wait_until_idle(5) is True
            assert main_window.sent.is_set()
            assert main_window.sends == ["5511@s.whatsapp.net"]
        finally:
            queue.stop()


class TestHasWork:
    def test_reports_queued_and_in_flight_messages(self):
        main_window = _MainWindow(block=True)
        queue = _queue(main_window)
        try:
            assert queue.has_work() is False
            queue.hold()
            queue.enqueue(_message())
            assert queue.has_work() is True          # queued, nothing attempted

            queue.release()
            assert main_window.send_started.wait(timeout=5)
            assert queue.has_work() is True          # now on the wire

            main_window.release_send.set()
            assert queue.wait_until_idle(5) is True
            assert queue.has_work() is False
        finally:
            main_window.release_send.set()
            queue.stop()
