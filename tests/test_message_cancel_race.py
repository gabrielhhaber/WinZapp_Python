"""Regression tests: deleting a message while it was still being sent.

MessageQueue only ever checked PendingMessage.cancel_event *before* the network
call started — never during or after it, since none of send_text_message()/
send_audio_message()/send_contact_attachment() (or a media upload finishing
between two progress-callback checks) can be interrupted mid-flight. So a
message could reach WhatsApp after the user cancelled it (conversations.py's
_cancel_pending_message() removes the row and calls queue.cancel() the moment
the user confirms), and the worker still ran the normal success path: register
the real id, call _on_message_sent() on a row that no longer exists. Delivered,
and invisible ever after.

Defects pinned here, each of which has its own way of going wrong:

1. Nothing revoked the delivered message, and nothing told the user it had gone
   out. The queue now reports every cancelled message it had already claimed —
   delivered (revoke it, and restore the row if the revoke fails) or dropped
   (release it) — and never silently discards the outcome.

2. The cancel check sat inside the success branch, so the ambiguous and
   definitive-failure branches still fired UI callbacks for the cancelled
   message: NVDA announcing "send not confirmed" for a row the user just
   deleted, and a modal error dialog for an upload abandoned on purpose.

3. The echo cannot be told apart from any other fromMe message except through
   _own_sent_ids, which can only be filled once the send call returns — and the
   echo routinely arrives before that. Registering the id is therefore not
   enough on its own: with the cancelled message's record deleted,
   on_new_message()'s by-type matching hands its WhatsApp id to the next
   unrelated pending send of the same type, which then permanently carries it
   (wrong delivery status, "delete for everyone" and quoting aimed at the wrong
   message, a {local_id}.msv renamed onto the wrong id so playback loads
   someone else's audio). So a cancelled message that a worker had already
   claimed keeps its *record* — marked _cancelled_awaiting_id, still pending —
   as the anchor the echo binds to, while only its row goes away.

4. A send answering {"ok": True} with no id (main.py's "ID not found in
   response", and both quote fallbacks) must not put the row back as a *sent*
   message under its local UUID: the echo would match nothing and append a
   second copy of the same message.

MessageQueue notifies the UI through wx.CallAfter, which no test has a wx.App
to dispatch. The autouse fixture below patches CallAfter on each module under
test rather than installing a fake `wx` in sys.modules the way
tests/test_message_queue_concurrency.py does: that variant works only as long
as no test imports the real wx first (`sys.modules.setdefault`), so whichever
file happens to import first silently decides it for every other.
"""

import os
import threading
import time
import types

import pytest

import core.message_queue as message_queue
import core.websocket_client as websocket_client
import ui.conversations as conversations
from core.message_queue import MessageQueue, PendingMessage
from core.websocket_client import WebSocketClient
from main import MainWindow
from ui.conversations import ConversationsPanel


CHAT = "5511999999999@s.whatsapp.net"


@pytest.fixture(autouse=True)
def direct_call_after(monkeypatch):
    """Dispatch wx.CallAfter straight to its callback along the whole chain
    under test (queue → main window → panel, and socket → on_new_message).

    Patched per module rather than once on `wx`, because whether the three hold
    the same module object depends on which test file imported first.
    """
    direct = lambda callback, *args: callback(*args)
    for module in (message_queue, websocket_client, conversations):
        monkeypatch.setattr(module.wx, "CallAfter", direct, raising=False)


def _wait_until(predicate, timeout=2.0):
    """Poll until predicate() is true; returns whether it became true."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _pending_text(local_id: str, body: str) -> dict:
    return {
        "_local_id":      local_id,
        "_local_pending": True,
        "key":            {"id": local_id, "fromMe": True, "remoteJid": CHAT},
        "message":        {"conversation": body},
        "messageType":    "conversation",
        "messageTimestamp": 1700000000,
        "pushName":       "",
    }


def _echo(real_id: str, body: str) -> dict:
    return {
        "key":            {"id": real_id, "fromMe": True, "remoteJid": CHAT},
        "message":        {"conversation": body},
        "messageType":    "conversation",
        "messageTimestamp": 1700000001,
        "pushName":       "",
    }


class _MainWindow:
    """The slice of MainWindow the queue and the echo path actually touch."""

    offline_mode = False
    _wa_connected = True

    def __init__(self):
        self._own_sent_ids_lock = threading.Lock()
        self._own_sent_ids = set()
        self.sent_calls = []
        self.failed_calls = []
        self.unconfirmed_calls = []
        self.cancelled_delivered_calls = []
        self.cancelled_dropped_calls = []

    def _on_message_sent(self, *args):
        self.sent_calls.append(args)

    def _on_message_failed(self, *args):
        self.failed_calls.append(args)

    def _on_message_unconfirmed(self, *args):
        self.unconfirmed_calls.append(args)

    # Mirrors the real signatures rather than taking *args, so a call made with
    # keywords (the ambiguous route does) records the same shape as a
    # positional one.
    def _on_cancelled_message_delivered(self, local_id, real_id=None,
                                        remote_jid=None, audio_path=None,
                                        quote_lost=False, ambiguous=False):
        self.cancelled_delivered_calls.append(
            (local_id, real_id, remote_jid, audio_path, quote_lost, ambiguous)
        )

    def _on_cancelled_message_dropped(self, local_id, audio_path=None):
        self.cancelled_dropped_calls.append((local_id, audio_path))


def _blocking_sender(started, release, result):
    """A send that parks mid-flight so cancel() genuinely lands during the POST."""
    def _send(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=3)
        return result
    return _send


class TestWhatCancelPromises:
    """cancel() has to answer "was this stopped for good?", because that is the
    only thing separating "delete it, done" from "hold on to it until the queue
    says what happened"."""

    def test_a_message_still_waiting_in_the_queue_is_stopped_for_good(self):
        main_window = _MainWindow()
        main_window.offline_mode = True   # parks the worker before any pickup
        main_window.send_text_message = lambda *a, **kw: "REAL_ID"
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))

            assert queue.cancel("loc-1") is True
        finally:
            queue.stop()

    def test_a_message_stopped_before_pickup_is_still_reported(self):
        """No worker will ever touch it again, so cancel() is the only place its
        outcome can be reported — and the temporary WAV a voice recording was
        sent from is known nowhere else."""
        main_window = _MainWindow()
        main_window.offline_mode = True
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage(
                "loc-1", CHAT, audio_path="recording.wav"
            ))

            assert queue.cancel("loc-1") is True
            assert main_window.cancelled_dropped_calls == [("loc-1", "recording.wav")]
        finally:
            queue.stop()

    def test_a_message_already_being_sent_is_not(self):
        started, release = threading.Event(), threading.Event()
        main_window = _MainWindow()
        main_window.send_text_message = _blocking_sender(started, release, "REAL_A")
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)

            assert queue.cancel("loc-1") is False
        finally:
            release.set()
            # Let the worker finish reporting before the fixture puts the real
            # wx.CallAfter back — it would assert on "no wx.App created yet".
            _wait_until(lambda: main_window.cancelled_delivered_calls)
            queue.stop()

    def test_a_message_the_worker_never_sends_after_a_cancel(self):
        """The claim and the flag check share the queue's lock, so a cancel can
        never be told "stopped" while the worker goes on to send anyway."""
        main_window = _MainWindow()
        main_window.offline_mode = True
        sent = []
        main_window.send_text_message = lambda *a, **kw: sent.append(a) or "REAL_ID"
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert queue.cancel("loc-1") is True
            main_window.offline_mode = False
            queue.flush()

            time.sleep(0.1)
            assert sent == []
        finally:
            queue.stop()


class TestASendThatOutranItsOwnCancel:
    def test_the_delivered_id_is_still_registered_as_ours(self):
        started, release = threading.Event(), threading.Event()

        main_window = _MainWindow()
        main_window.send_text_message = _blocking_sender(started, release, "REAL_A")
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)

            assert queue.cancel("loc-1") is False
            release.set()

            assert _wait_until(lambda: "REAL_A" in main_window._own_sent_ids)
        finally:
            queue.stop()

    def test_the_completion_path_gets_what_it_needs_to_revoke(self):
        started, release = threading.Event(), threading.Event()

        main_window = _MainWindow()
        main_window.send_text_message = _blocking_sender(started, release, "REAL_A")
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-1") is False
            release.set()

            assert _wait_until(lambda: main_window.cancelled_delivered_calls)
            assert main_window.cancelled_delivered_calls[0] == (
                "loc-1", "REAL_A", CHAT, None, False, False,
            )
        finally:
            queue.stop()

    def test_the_outcome_is_reported_exactly_once(self):
        """Not "at least once": the finally is a net for branches that report
        nothing, so a branch that did report has to say so. Two reports mean the
        revoke is asked for twice, or asked for and then undone by a drop."""
        started, release = threading.Event(), threading.Event()

        main_window = _MainWindow()
        main_window.send_text_message = _blocking_sender(started, release, "REAL_A")
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-1") is False
            release.set()

            assert _wait_until(lambda: main_window.cancelled_delivered_calls)
            # Give a duplicate its chance to arrive before ruling it out.
            assert not _wait_until(
                lambda: len(main_window.cancelled_delivered_calls) > 1
                or main_window.cancelled_dropped_calls,
                timeout=0.3,
            )
        finally:
            queue.stop()

    def test_a_lost_quote_reaches_the_restored_row(self):
        """Otherwise a restored row reads as a reply to a quote the recipient
        never got — what tests/test_quote_lost_drops_reply_header.py exists for."""
        started, release = threading.Event(), threading.Event()

        main_window = _MainWindow()
        main_window.send_text_message = _blocking_sender(
            started, release, {"ok": True, "id": "REAL_A", "quote_lost": True}
        )
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-1") is False
            release.set()

            assert _wait_until(lambda: main_window.cancelled_delivered_calls)
            assert main_window.cancelled_delivered_calls[0][4] is True
        finally:
            queue.stop()

    def test_a_success_without_an_id_is_reported_as_delivered(self):
        """{"ok": True} with no id — main.py's "ID not found in response" and
        both quote fallbacks answer exactly that."""
        started, release = threading.Event(), threading.Event()

        main_window = _MainWindow()
        main_window.send_text_message = _blocking_sender(
            started, release, {"ok": True}
        )
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-1") is False
            release.set()

            assert _wait_until(lambda: main_window.cancelled_delivered_calls)
            assert main_window.cancelled_delivered_calls[0][1] is None
            assert main_window.cancelled_dropped_calls == []
        finally:
            queue.stop()

    def test_the_row_is_never_marked_as_sent(self):
        started, release = threading.Event(), threading.Event()

        main_window = _MainWindow()
        main_window.send_text_message = _blocking_sender(started, release, "REAL_A")
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-1") is False
            release.set()

            assert _wait_until(lambda: main_window.cancelled_delivered_calls)
            assert main_window.sent_calls == []
            assert main_window.failed_calls == []
        finally:
            queue.stop()

    def test_the_recorded_wav_is_handed_over_for_cleanup(self, tmp_path):
        """The temporary WAV is deleted by the main window, the same place
        _on_message_sent() deletes it — the queue must pass it on rather than
        unlink it and lose the voice note's only copy."""
        started, release = threading.Event(), threading.Event()
        audio_path = str(tmp_path / "recording.wav")
        with open(audio_path, "wb") as handle:
            handle.write(b"fake wav data")

        main_window = _MainWindow()
        main_window.send_audio_message = _blocking_sender(started, release, "REAL_AUDIO")
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-audio", CHAT, audio_path=audio_path))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-audio") is False
            release.set()

            assert _wait_until(lambda: main_window.cancelled_delivered_calls)
            assert main_window.cancelled_delivered_calls[0][3] == audio_path
        finally:
            queue.stop()


class TestTheOtherOutcomesStaySilentAfterACancel:
    """The cancel check has to precede the outcome branches, not sit inside the
    success one."""

    def test_an_ambiguous_timeout_announces_nothing(self):
        started, release = threading.Event(), threading.Event()

        main_window = _MainWindow()
        main_window.send_text_message = _blocking_sender(
            started, release, {"ok": False, "ambiguous": True, "error": "timeout"}
        )
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-1") is False
            release.set()

            assert _wait_until(lambda: main_window.cancelled_delivered_calls)
            assert main_window.unconfirmed_calls == []
        finally:
            queue.stop()

    def test_an_ambiguous_outcome_is_not_reported_as_never_sent(self):
        """A timeout is "unknown", not "it never went out" — the whole reason
        the ambiguous branch refuses to retry. Releasing the record here would
        leave a message WhatsApp Web flushes on reconnect arriving with no
        anchor, to take the next pending send's identity."""
        started, release = threading.Event(), threading.Event()

        main_window = _MainWindow()
        main_window.send_text_message = _blocking_sender(
            started, release, {"ok": False, "ambiguous": True, "error": "timeout"}
        )
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-1") is False
            release.set()

            assert _wait_until(lambda: main_window.cancelled_delivered_calls)
            assert main_window.cancelled_dropped_calls == []
            local_id, real_id, _jid, _wav, _quote, ambiguous = (
                main_window.cancelled_delivered_calls[0]
            )
            assert real_id is None      # nothing to revoke
            # ...and reported AS unknown, not as delivered: that is what stops
            # the restored row from becoming a permanent pending anchor.
            assert ambiguous is True
        finally:
            queue.stop()

    def test_a_definitive_failure_opens_no_error_dialog(self):
        """A cancelled media upload that then fails server-side after the last
        progress callback (so MessageCancelled is never raised)."""
        started, release = threading.Event(), threading.Event()

        main_window = _MainWindow()
        main_window.send_media_attachment = _blocking_sender(
            started, release, {"ok": False, "retry": False, "error": "boom"}
        )
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage(
                "loc-media", CHAT, media_path="big.bin", media_type="document",
            ))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-media") is False
            release.set()

            assert _wait_until(lambda: main_window.cancelled_dropped_calls)
            assert main_window.failed_calls == []
        finally:
            queue.stop()

    def test_a_send_that_failed_is_reported_as_dropped(self):
        """The UI is holding this message's record: without the report it would
        wait forever for an echo that is never coming."""
        started, release = threading.Event(), threading.Event()
        audio_path = "recording.wav"

        main_window = _MainWindow()
        main_window.send_audio_message = _blocking_sender(
            started, release, {"ok": False, "retry": False, "error": "boom"}
        )
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, audio_path=audio_path))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-1") is False
            release.set()

            assert _wait_until(lambda: main_window.cancelled_dropped_calls)
            assert main_window.cancelled_dropped_calls[0] == ("loc-1", audio_path)
            assert main_window._own_sent_ids == set()
            assert main_window.cancelled_delivered_calls == []
        finally:
            queue.stop()

    def test_an_upload_interrupted_mid_transfer_is_reported_as_dropped(self):
        """MessageCancelled raised from the progress callback — the one case
        where the cancel really does stop a transfer already running."""
        started = threading.Event()

        class _Uploading(_MainWindow):
            def send_media_attachment(self, *_args, **kwargs):
                started.set()
                callback = kwargs["progress_callback"]
                while True:
                    callback(0.25)
                    time.sleep(0.01)

        main_window = _Uploading()
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage(
                "loc-media", CHAT, media_path="big.bin", media_type="document",
            ))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-media") is False

            assert _wait_until(lambda: main_window.cancelled_dropped_calls)
            assert main_window.failed_calls == []
        finally:
            queue.stop()


class TestACancelThatLandsInsideAnOutcomeBranch:
    """Every outcome branch pops the message from _pending before its
    wx.CallAfter reaches the main thread. From that pop onwards cancel() answers
    False and the panel starts holding the message's record — so a cancel
    landing in that gap must still produce exactly one report, or the record is
    held forever and the next send of the same type has its echo stolen by it.

    The gap is entered deterministically here: the fixture dispatches CallAfter
    inline, so the stub's own outcome handler runs on the worker thread inside
    the window, and cancelling from there is the user clicking delete at the end
    of a stuck upload or a fourth failed attempt.
    """

    def _run(self, monkeypatch, send_result):
        """Cancel exactly in the gap: after the branch's own _pending.pop(), and
        before the callback it queued is dispatched on the main thread. That is
        the real ordering — both run on the UI thread, so the handler dispatched
        after the user's click is all that is left to answer for the message."""
        started, release = threading.Event(), threading.Event()
        reported = threading.Event()
        state = {}

        class _Reporting(_MainWindow):
            # The real handlers, which is where the routing under test lives.
            _on_message_unconfirmed = MainWindow._on_message_unconfirmed
            _on_message_failed = MainWindow._on_message_failed
            _is_cancelled_send = MainWindow._is_cancelled_send
            background_mode = True

            def _on_cancelled_message_delivered(self, *args, **kwargs):
                super()._on_cancelled_message_delivered(*args, **kwargs)
                reported.set()

            def _on_cancelled_message_dropped(self, *args, **kwargs):
                super()._on_cancelled_message_dropped(*args, **kwargs)
                reported.set()

        main_window = _Reporting()
        # The panel is holding the record, exactly as _cancel_pending_message()
        # leaves it once cancel() has answered False.
        main_window.conversations_panel = types.SimpleNamespace(
            _is_cancelled_pending=lambda lid: lid in state.get("held", ()),
        )
        main_window.send_text_message = _blocking_sender(started, release, send_result)
        queue = MessageQueue(main_window)

        def _cancel_then_dispatch(callback, *args):
            if callback in (main_window._on_message_unconfirmed,
                            main_window._on_message_failed):
                assert queue.cancel("loc-1") is False
                state["held"] = {"loc-1"}
            callback(*args)
        monkeypatch.setattr(message_queue.wx, "CallAfter", _cancel_then_dispatch)

        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)
            release.set()
            assert reported.wait(timeout=2), (
                "the cancelled message was never reported: the panel is left "
                "holding its record forever"
            )
        finally:
            queue.stop()
        return main_window

    def test_a_cancel_during_the_ambiguous_report_is_still_answered(self, monkeypatch):
        main_window = self._run(
            monkeypatch, {"ok": False, "ambiguous": True, "error": "timeout"}
        )

        # Unknown outcome: not released as "never sent"...
        assert main_window.cancelled_dropped_calls == []
        # ...and not passed off as delivered either, or the restored row would
        # stay pending forever and swallow the next message's echo.
        assert main_window.cancelled_delivered_calls
        assert main_window.cancelled_delivered_calls[0][5] is True

    def test_a_cancel_during_the_give_up_report_is_still_answered(self, monkeypatch):
        main_window = self._run(
            monkeypatch, {"ok": False, "retry": False, "error": "boom"}
        )

        # It definitively did not go out: the anchor is released.
        assert main_window.cancelled_dropped_calls
        assert main_window.cancelled_delivered_calls == []

    def test_a_cancel_inside_a_branch_that_reports_nothing_is_still_answered(self):
        """The retryable-failure branch notifies no one — it leaves the message
        queued for the next attempt — so there is no callback to make
        cancel-aware. The finally is what answers for it: it is the one place
        every branch passes through, which is what makes "a cancel answered
        False always gets exactly one report" structural rather than a list of
        branches somebody has to keep complete.

        The cancel is landed inside that branch through the one thing it reads
        from the main window, which is also a real interleaving: the user
        clicking delete between the send returning and the retry decision.
        """
        started, release = threading.Event(), threading.Event()

        class _CancelsWhileFailing(_MainWindow):
            queue = None

            @property
            def _last_send_error(self):
                assert self.queue.cancel("loc-1") is False
                return "boom"

        main_window = _CancelsWhileFailing()
        main_window.send_text_message = _blocking_sender(
            started, release, {"ok": False, "retry": True, "error": ""}
        )
        queue = MessageQueue(main_window)
        main_window.queue = queue
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)
            release.set()

            assert _wait_until(lambda: main_window.cancelled_dropped_calls), (
                "the cancelled message was never reported: the panel is left "
                "holding its record forever"
            )
            assert main_window.failed_calls == []
        finally:
            queue.stop()

    def test_a_report_that_raises_does_not_take_the_queue_down_with_it(self):
        """The finally's report is the one call outside the loop body's own
        except, so an exception there runs on the worker thread with nothing
        left to catch it — the thread dies and nothing is ever sent again,
        silently."""
        started, release = threading.Event(), threading.Event()

        class _ExplodesOnReport(_MainWindow):
            queue = None

            @property
            def _last_send_error(self):
                assert self.queue.cancel("loc-1") is False
                return "boom"

            def _on_cancelled_message_dropped(self, *args, **kwargs):
                super()._on_cancelled_message_dropped(*args, **kwargs)
                raise RuntimeError("the UI hand-off blew up")

        main_window = _ExplodesOnReport()
        main_window.send_text_message = _blocking_sender(
            started, release, {"ok": False, "retry": True, "error": ""}
        )
        queue = MessageQueue(main_window)
        main_window.queue = queue
        try:
            queue.enqueue(PendingMessage("loc-1", CHAT, text="oi"))
            assert started.wait(timeout=1)
            release.set()
            assert _wait_until(lambda: main_window.cancelled_dropped_calls)

            # The worker has to still be alive for the next message.
            main_window.send_text_message = lambda *a, **kw: "REAL_B"
            queue.enqueue(PendingMessage("loc-2", CHAT, text="segunda"))
            assert _wait_until(lambda: main_window.sent_calls), (
                "the worker thread died on the failed report — the queue is "
                "stuck forever"
            )
        finally:
            queue.stop()

    def test_the_stale_held_record_does_not_swallow_the_next_send(
            self, monkeypatch, tmp_path):
        """The consequence the report exists to prevent, end to end: A is
        deleted while its send is failing, then B is sent and echoes back. The
        echo must resolve B, not the record A left behind."""
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        pending_a = _pending_text("loc-A", "primeira")
        pending_b = _pending_text("loc-B", "segunda")
        main_window = _EchoMainWindow(records=[pending_a, pending_b])
        main_window.message_queue = _FakeQueue(result=False)
        panel = _make_panel(main_window, pending_a)
        ConversationsPanel._cancel_pending_message(panel, pending_a, "loc-A")

        # The queue reports that A never went out, which releases the record.
        ConversationsPanel.discard_cancelled_message(panel, "loc-A")
        _Socket(main_window).on_messages_upsert({"data": _echo("REAL_B", "segunda")})

        assert pending_b["key"]["id"] == "REAL_B"
        assert pending_b["_local_pending"] is False
        assert pending_a["key"]["id"] == "loc-A"


class _CancelDuringTheGap:
    """A cancel_event that fires the user's click inside the worker's own gap.

    The gap is one statement wide: between the in-flight cancel check (which is
    what routes a cancellation into the cancelled path) and the outcome branch's
    own _pending.pop(). A cancel landing there still finds the message, so
    cancel() sets the flag and answers False — but the worker has already
    decided it was not cancelled and runs the ordinary branch. Its callback is
    then cancel-aware and reports; the finally must NOT report again.

    Hooked on the SECOND is_set(): the first is the claim under the queue's
    lock, and cancelling from there deadlocks on that same lock.
    """

    def __init__(self):
        self._event = threading.Event()
        self.hook = None
        self.calls = 0

    def set(self):
        self._event.set()

    def is_set(self):
        # Read before the hook runs, so the worker sees the pre-cancel answer —
        # which is the whole point of the window.
        value = self._event.is_set()
        self.calls += 1
        if self.calls == 2 and self.hook is not None:
            self.hook()
        return value


class TestACancelInsideTheGapBetweenTheCheckAndThePop:
    """Every outcome branch marks itself as having reported. Without that, this
    window produces TWO reports for one message: the branch's own cancel-aware
    callback, and then the finally's net — two revoke threads and two spoken
    announcements for a single cancellation."""

    @pytest.mark.parametrize("send_result", [
        "REAL_A",
        {"ok": False, "ambiguous": True, "error": "timeout"},
        {"ok": False, "retry": False, "error": "boom"},
    ], ids=["success", "ambiguous", "give_up"])
    def test_exactly_one_report(self, send_result):
        started, release = threading.Event(), threading.Event()
        held = set()

        class _Reporting(_MainWindow):
            _on_message_sent = MainWindow._on_message_sent
            _on_message_unconfirmed = MainWindow._on_message_unconfirmed
            _on_message_failed = MainWindow._on_message_failed
            _is_cancelled_send = MainWindow._is_cancelled_send
            background_mode = True

        main_window = _Reporting()
        main_window.conversations_panel = types.SimpleNamespace(
            _is_cancelled_pending=lambda lid: lid in held,
            _mark_message_sent=lambda *a, **kw: None,
            _mark_message_unconfirmed=lambda *a: None,
            _mark_message_failed=lambda *a: None,
        )
        main_window._schedule_set_chats = lambda *a, **kw: None
        main_window.db = types.SimpleNamespace(update_message_id=lambda *a: None)
        main_window.send_text_message = _blocking_sender(
            started, release, send_result
        )

        queue = MessageQueue(main_window)
        message = PendingMessage("loc-1", CHAT, text="oi")
        message.cancel_event = _CancelDuringTheGap()

        def _the_user_clicks_delete():
            assert queue.cancel("loc-1") is False   # a worker owns it
            held.add("loc-1")
        message.cancel_event.hook = _the_user_clicks_delete

        def _reports():
            return (main_window.cancelled_delivered_calls
                    + main_window.cancelled_dropped_calls)

        try:
            queue.enqueue(message)
            assert started.wait(timeout=2)
            release.set()

            assert _wait_until(_reports), "the cancellation was never reported"
            # Give a second report its chance to arrive before ruling it out.
            assert not _wait_until(lambda: len(_reports()) > 1, timeout=0.4), (
                f"reported twice: {_reports()}"
            )
        finally:
            queue.stop()


class TestANonCancelledSendIsUnaffected:
    def test_normal_success_still_registers_and_notifies(self):
        main_window = _MainWindow()
        main_window.send_text_message = lambda *a, **kw: "REAL_ID"
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-2", CHAT, text="oi"))

            assert _wait_until(lambda: main_window.sent_calls)
            assert "REAL_ID" in main_window._own_sent_ids
            assert len(main_window.sent_calls) == 1
            assert main_window.sent_calls[0][0] == "loc-2"
            assert main_window.cancelled_delivered_calls == []
            assert main_window.cancelled_dropped_calls == []
        finally:
            queue.stop()


# ── The echo, end to end ─────────────────────────────────────────────────────
# on_new_message() is reached exactly the way the Socket.IO thread reaches it,
# so these cover the two things that actually protect the next message:
# WebSocketClient.on_messages_upsert() consulting _own_sent_ids, and the
# cancelled message's own record still being there to be matched.

class _Executor:
    def submit(self, fn, *args, **kwargs):
        return None


class _EchoMainWindow(_MainWindow):
    """_MainWindow plus the state MainWindow.on_new_message() reads."""

    on_new_message = MainWindow.on_new_message
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _counts_as_last_message = MainWindow._counts_as_last_message
    # The real guard, not a no-op: on_new_message() routes every message
    # through it, and binding the genuine one means these echo tests would
    # also catch it starting to redirect an ordinary 1:1 echo. Inert for the
    # fixtures here — CHAT is a plain phone JID and no message carries a
    # "participant", so neither shape it looks for can match.
    _redirect_self_chat_artifact = MainWindow._redirect_self_chat_artifact
    _phone_digits_equivalent = staticmethod(MainWindow._phone_digits_equivalent)

    def __init__(self, records):
        super().__init__()
        self.chats = {CHAT: {"remoteJid": CHAT, "unreadCount": 0, "t": 0,
                             "messages": {"messages": {"records": records}}}}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._deleted_chats = set()
        self._msg_bg_executor = _Executor()
        self.deleted_from_db = []
        self.db = types.SimpleNamespace(
            insert_message=lambda *a, **kw: None,
            delete_message=lambda jid, mid: self.deleted_from_db.append(mid),
        )
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self.spoken = []
        self.message_queue = None

    # ── on_new_message's dependencies ───────────────────────────────────────
    def _live_events_ready(self):
        return True

    def _is_self_jid(self, jid):
        return False

    def _extract_lid_mapping(self, msg):
        pass

    def _is_cleared_message(self, remote_jid, msg):
        return False

    def apply_forwarded_duration(self, msg):
        pass

    def _apply_group_subject_change(self, *args, **kwargs):
        pass

    def _refresh_mention_cache_on_membership_change(self, *args, **kwargs):
        pass

    def _apply_group_settings_change(self, *args, **kwargs):
        pass

    def _schedule_save(self, *args, **kwargs):
        pass

    def _schedule_set_chats(self, *args, **kwargs):
        pass

    # ── the panel's dependencies ────────────────────────────────────────────
    def get_chat(self, jid):
        return self.chats.get(jid)

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def _recompute_chat_last_message(self, jid):
        pass

    def records(self):
        return self.chats[CHAT]["messages"]["messages"]["records"]


class _Socket:
    """WebSocketClient's echo routing, bound to a stub main window."""

    on_messages_upsert = WebSocketClient.on_messages_upsert

    def __init__(self, main_window):
        self.main_window = main_window


class _FakeQueue:
    """A queue that has already claimed the message — cancel() answers False."""

    def __init__(self, result=False):
        self.result = result
        self.cancelled = []

    def cancel(self, local_id):
        self.cancelled.append(local_id)
        return self.result


def _make_panel(main_window, msg=None):
    panel = ConversationsPanel.__new__(ConversationsPanel)
    panel.main_window = main_window
    panel._outgoing_virtual_messages = {msg["_local_id"]: msg} if msg else {}
    panel._media_upload_progress = {}
    panel._upload_stages_seen = {}
    panel._media_transfer_started = set()
    panel._cancelled_pending_messages = {}
    panel._hide_media_transfer_gauge = lambda: None
    panel.removed = []
    panel.restored = []
    panel.on_incoming_message = lambda jid, m: panel.restored.append((jid, m))

    def _remove(ids, focus_previous=False):
        # Faithful to the real remove_messages_by_id() in the one respect these
        # tests turn on: it drops the record from the chat as well as the row.
        panel.removed.append(set(ids))
        chat = main_window.get_chat(CHAT)
        if chat is not None:
            records = chat["messages"]["messages"]["records"]
            chat["messages"]["messages"]["records"] = [
                r for r in records if r.get("key", {}).get("id") not in ids
            ]
    panel.remove_messages_by_id = _remove
    return panel


class TestTheEchoCannotStealAnotherMessagesIdentity:
    def test_a_second_pending_message_of_the_same_type_keeps_its_own_id(self):
        """Text A is cancelled mid-POST and delivered anyway; text B is sent
        right after and is still pending. A's echo must not resolve B."""
        pending_b = _pending_text("loc-B", "segunda")
        main_window = _EchoMainWindow(records=[pending_b])

        started, release = threading.Event(), threading.Event()
        main_window.send_text_message = _blocking_sender(started, release, "REAL_A")
        queue = MessageQueue(main_window)
        try:
            queue.enqueue(PendingMessage("loc-A", CHAT, text="primeira"))
            assert started.wait(timeout=1)
            assert queue.cancel("loc-A") is False
            release.set()
            # Deliberately not asserted: this is the hand-off the fix adds, and
            # the point of the test is what happens to B when the worker does
            # NOT make it — so wait for it, then go on either way.
            _wait_until(lambda: main_window.cancelled_delivered_calls, timeout=1.0)
        finally:
            queue.stop()

        _Socket(main_window).on_messages_upsert({"data": _echo("REAL_A", "primeira")})

        assert pending_b["key"]["id"] == "loc-B"
        assert pending_b["_local_pending"] is True

    def test_an_echo_arriving_before_the_send_returns_binds_to_the_right_row(self):
        """The window _own_sent_ids cannot close: the echo can arrive before the
        send call has even returned, so the cancelled message's own record has
        to still be there for the by-type matcher to find. Remove that record
        and B inherits A's WhatsApp id instead."""
        cancelled_a = _pending_text("loc-A", "primeira")
        pending_b = _pending_text("loc-B", "segunda")
        main_window = _EchoMainWindow(records=[cancelled_a, pending_b])
        main_window.message_queue = _FakeQueue(result=False)
        panel = _make_panel(main_window, cancelled_a)

        ConversationsPanel._cancel_pending_message(panel, cancelled_a, "loc-A")

        # Nothing has registered REAL_A yet — the send has not returned.
        assert main_window._own_sent_ids == set()
        _Socket(main_window).on_messages_upsert({"data": _echo("REAL_A", "primeira")})

        assert cancelled_a["key"]["id"] == "REAL_A"
        assert pending_b["key"]["id"] == "loc-B"
        assert pending_b["_local_pending"] is True

    def test_the_held_record_stays_out_of_the_chat_list(self):
        """It is only an anchor for the echo — the user deleted the row, so it
        must not come back as the conversation's last message."""
        cancelled_a = _pending_text("loc-A", "primeira")

        assert MainWindow._counts_as_last_message(cancelled_a) is True
        cancelled_a["_cancelled_awaiting_id"] = True
        assert MainWindow._counts_as_last_message(cancelled_a) is False

    def test_the_held_record_stays_out_of_the_conversation(self):
        """populate_messages() rebuilds the message list straight from the
        records, filtering on this predicate alone: without the marker here, the
        deleted row is back — as "sending" — the moment the user leaves the
        conversation and returns, for the whole length of an upload."""
        panel = ConversationsPanel.__new__(ConversationsPanel)
        cancelled_a = _pending_text("loc-A", "primeira")

        assert ConversationsPanel._is_displayable_message(panel, cancelled_a) is True
        cancelled_a["_cancelled_awaiting_id"] = True
        assert ConversationsPanel._is_displayable_message(panel, cancelled_a) is False

    def test_an_ordinary_echo_from_another_device_still_gets_through(self):
        """The guard is _own_sent_ids membership, not "fromMe" — a message the
        user typed on their phone must keep resolving normally."""
        pending_b = _pending_text("loc-B", "segunda")
        main_window = _EchoMainWindow(records=[pending_b])

        _Socket(main_window).on_messages_upsert({"data": _echo("REAL_B", "segunda")})

        assert pending_b["key"]["id"] == "REAL_B"
        assert pending_b["_local_pending"] is False


# ── The UI side of the cancellation ──────────────────────────────────────────

class TestCancellingAPendingRow:
    def test_a_cancellation_that_won_deletes_everything(self, monkeypatch, tmp_path):
        """cancel() promised the send was stopped: no record to hold, nothing to
        wait for, and no reason to keep the message anywhere — including the
        voice/media copies cached under its local UUID, which nothing will ever
        look up again."""
        voice = tmp_path / "voice_messages"
        voice.mkdir()
        (tmp_path / "media").mkdir()
        open(str(voice / "loc-1.msv"), "wb").close()
        monkeypatch.setattr(
            conversations, "data_path", lambda name: str(tmp_path / name)
        )
        msg = _pending_text("loc-1", "oi")
        main_window = _EchoMainWindow(records=[msg])
        main_window.message_queue = _FakeQueue(result=True)
        panel = _make_panel(main_window, msg)

        ConversationsPanel._cancel_pending_message(panel, msg, "loc-1")

        assert panel.removed == [{"loc-1"}]
        assert main_window.records() == []
        assert panel._cancelled_pending_messages == {}
        assert panel._outgoing_virtual_messages == {}
        assert os.listdir(str(voice)) == []

    def test_a_cancellation_that_lost_holds_the_record(self, monkeypatch, tmp_path):
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        msg = _pending_text("loc-1", "oi")
        main_window = _EchoMainWindow(records=[msg])
        main_window.message_queue = _FakeQueue(result=False)
        panel = _make_panel(main_window, msg)

        ConversationsPanel._cancel_pending_message(panel, msg, "loc-1")

        assert panel.removed == [{"loc-1"}]          # the row still goes
        assert main_window.records() == [msg]        # the record does not
        assert msg["_cancelled_awaiting_id"] is True
        assert msg["_local_pending"] is True
        assert ConversationsPanel._is_cancelled_pending(panel, "loc-1") is True

    def test_the_stash_does_not_grow_without_bound(self):
        main_window = _EchoMainWindow(records=[])
        panel = _make_panel(main_window)

        for i in range(80):
            ConversationsPanel._remember_cancelled_pending(
                panel, f"loc-{i}", _pending_text(f"loc-{i}", "oi")
            )

        assert len(panel._cancelled_pending_messages) == 50
        assert "loc-79" in panel._cancelled_pending_messages


class TestReleasingAHeldCancellation:
    def test_a_chat_with_no_messages_block_is_survivable(self, monkeypatch, tmp_path):
        """_cancel_pending_message() returns early when the record is not in the
        chat, without creating the messages block the release path then writes
        to. Reachable, and a KeyError here happens on the UI thread."""
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        main_window = _EchoMainWindow(records=[])
        main_window.chats[CHAT] = {"remoteJid": CHAT}      # no messages block
        panel = _make_panel(main_window)
        panel._cancelled_pending_messages["loc-1"] = _pending_text("loc-1", "oi")

        ConversationsPanel.discard_cancelled_message(panel, "loc-1")

        assert main_window.chats[CHAT]["messages"]["messages"]["records"] == []

    def test_a_dropped_message_is_deleted_for_real(self, monkeypatch, tmp_path):
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        msg = _pending_text("loc-1", "oi")
        main_window = _EchoMainWindow(records=[msg])
        main_window.message_queue = _FakeQueue(result=False)
        panel = _make_panel(main_window, msg)
        ConversationsPanel._cancel_pending_message(panel, msg, "loc-1")

        ConversationsPanel.discard_cancelled_message(panel, "loc-1")

        assert main_window.records() == []
        assert "loc-1" in main_window.deleted_from_db
        assert panel._cancelled_pending_messages == {}
        assert main_window.spoken == []


class TestCompletingACancellationThatLost:
    def test_the_delivered_message_is_revoked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        msg = _pending_text("loc-1", "oi")
        main_window = _EchoMainWindow(records=[msg])
        main_window.message_queue = _FakeQueue(result=False)
        main_window.revoke_calls = []
        main_window.delete_message_for_everyone = (
            lambda jid, key: main_window.revoke_calls.append((jid, key)) or True
        )
        panel = _make_panel(main_window, msg)
        ConversationsPanel._cancel_pending_message(panel, msg, "loc-1")

        ConversationsPanel.complete_cancelled_message_delivery(
            panel, "loc-1", "REAL_A", CHAT
        )
        assert _wait_until(lambda: main_window.revoke_calls)

        jid, msg_key = main_window.revoke_calls[0]
        assert jid == CHAT
        assert msg_key["id"] == "REAL_A"
        assert msg_key["fromMe"] is True

    def test_a_successful_revoke_is_announced_and_leaves_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        msg = _pending_text("loc-1", "oi")
        main_window = _EchoMainWindow(records=[msg])
        main_window.message_queue = _FakeQueue(result=False)
        panel = _make_panel(main_window, msg)
        ConversationsPanel._cancel_pending_message(panel, msg, "loc-1")

        ConversationsPanel._finish_cancelled_message_delivery(
            panel, "loc-1", "REAL_A", True
        )

        assert panel.restored == []
        assert main_window.records() == []
        assert "REAL_A" in main_window.deleted_from_db
        assert panel._cancelled_pending_messages == {}
        assert main_window.spoken == ["cancelled_message_revoked"]

    def test_a_failed_revoke_puts_the_row_back_under_the_real_id(self, monkeypatch, tmp_path):
        """A delivered message the app pretends to have cancelled is worse than
        a cancellation that visibly failed."""
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        msg = _pending_text("loc-1", "oi")
        main_window = _EchoMainWindow(records=[msg])
        main_window.message_queue = _FakeQueue(result=False)
        panel = _make_panel(main_window, msg)
        ConversationsPanel._cancel_pending_message(panel, msg, "loc-1")

        ConversationsPanel._finish_cancelled_message_delivery(
            panel, "loc-1", "REAL_A", False
        )

        assert msg["key"]["id"] == "REAL_A"
        assert msg["_local_pending"] is False
        assert "_cancelled_awaiting_id" not in msg
        assert panel.restored == [(CHAT, msg)]
        assert main_window.records() == [msg]
        assert main_window.spoken == ["cancelled_message_still_sent"]

    def test_a_restored_row_drops_a_quote_that_never_went_out(self, monkeypatch, tmp_path):
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        msg = _pending_text("loc-1", "oi")
        msg["contextInfo"] = {"quotedMessage": {"conversation": "citada"}}
        main_window = _EchoMainWindow(records=[msg])
        main_window.message_queue = _FakeQueue(result=False)
        panel = _make_panel(main_window, msg)
        ConversationsPanel._cancel_pending_message(panel, msg, "loc-1")

        ConversationsPanel._finish_cancelled_message_delivery(
            panel, "loc-1", "REAL_A", False, True
        )

        assert "contextInfo" not in msg

    def _delivered_without_an_id(self, monkeypatch, tmp_path):
        """The {"ok": True}-with-no-id answer, restored and back in the chat."""
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        msg = _pending_text("loc-1", "oi")
        main_window = _EchoMainWindow(records=[msg])
        main_window.message_queue = _FakeQueue(result=False)
        main_window.revoke_calls = []
        main_window.delete_message_for_everyone = (
            lambda jid, key: main_window.revoke_calls.append((jid, key)) or True
        )
        panel = _make_panel(main_window, msg)
        ConversationsPanel._cancel_pending_message(panel, msg, "loc-1")
        ConversationsPanel.complete_cancelled_message_delivery(
            panel, "loc-1", "", CHAT
        )
        return main_window, panel, msg

    def test_a_delivery_with_no_real_id_leaves_the_row_pending(
            self, monkeypatch, tmp_path):
        """There is no id to revoke and none to restore it under, so the row
        goes back exactly as it was — the echo is still the only thing that can
        give it one."""
        main_window, panel, msg = self._delivered_without_an_id(monkeypatch, tmp_path)

        assert main_window.revoke_calls == []   # nothing to ask the API about
        assert msg["_local_pending"] is True
        assert msg["key"]["id"] == "loc-1"
        assert panel.restored == [(CHAT, msg)]

    def test_a_delivery_with_no_real_id_is_not_duplicated_by_its_own_echo(
            self, monkeypatch, tmp_path):
        """Restoring it as a *sent* message under its local UUID would leave the
        echo nothing to match, and it would be appended as a second copy of the
        same message."""
        main_window, _panel, msg = self._delivered_without_an_id(monkeypatch, tmp_path)

        _Socket(main_window).on_messages_upsert({"data": _echo("REAL_A", "oi")})

        assert len(main_window.records()) == 1
        assert msg["key"]["id"] == "REAL_A"

    def test_an_unknown_outcome_does_not_leave_a_permanent_anchor(
            self, monkeypatch, tmp_path):
        """A timeout is a third outcome, not a flavour of "delivered". Left
        pending like a confirmed-but-ID-less send, the row is an anchor that
        never resolves — and on_new_message() matches the FIRST pending record
        of the type, so the next message's echo lands on this one instead: the
        ghost takes B's WhatsApp ID and B stays "sending" forever.

        _mark_message_unconfirmed() already answers this for a send that was
        never cancelled, and this mirrors it."""
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        msg_a = _pending_text("loc-A", "primeira")
        main_window = _EchoMainWindow(records=[msg_a])
        main_window.message_queue = _FakeQueue(result=False)
        panel = _make_panel(main_window, msg_a)
        ConversationsPanel._cancel_pending_message(panel, msg_a, "loc-A")

        ConversationsPanel.complete_cancelled_message_delivery(
            panel, "loc-A", "", CHAT, False, True,
        )

        assert msg_a["_local_pending"] is False
        assert msg_a["_send_unconfirmed"] is True
        assert main_window.spoken == ["cancelled_message_unconfirmed"]

        # The next message the user sends, and its echo.
        pending_b = _pending_text("loc-B", "segunda")
        main_window.records().append(pending_b)
        _Socket(main_window).on_messages_upsert({"data": _echo("REAL_B", "segunda")})

        assert pending_b["key"]["id"] == "REAL_B"
        assert pending_b["_local_pending"] is False
        assert msg_a["key"]["id"] == "loc-A"

    def test_a_failed_revoke_with_no_record_left_still_says_so(self, monkeypatch, tmp_path):
        """The row cannot come back (the stash entry is gone), but the user must
        not be left believing a delivered message was cancelled."""
        monkeypatch.setattr(conversations, "data_path", lambda name: str(tmp_path))
        main_window = _EchoMainWindow(records=[])
        panel = _make_panel(main_window)

        ConversationsPanel._finish_cancelled_message_delivery(
            panel, "loc-gone", "REAL_A", False
        )

        assert panel.restored == []
        assert main_window.spoken == ["cancelled_message_still_sent"]


class TestTheDeleteDialogReadsTheIdItActsOn:
    """_on_menu_delete_message() cannot be called headlessly (it runs its own
    ShowModal), so this checks the one line that has to come after it: the
    dialog's nested event loop is where the worker's wx.CallAfter(
    _on_message_sent) is dispatched, so a message that was pending when the
    menu opened can already carry its real WhatsApp id by the time the dialog
    closes. Removing the row by the id captured before ShowModal() matches
    nothing and leaves a just-revoked message on screen."""

    def test_the_id_is_re_read_after_the_dialog_closes(self):
        import inspect

        source = inspect.getsource(ConversationsPanel._on_menu_delete_message)
        after_dialog = source.split("dlg.Destroy()", 1)[1]

        assert 'msg_id = msg.get("key", {}).get("id", "")' in after_dialog


class TestTheLocalMediaCache:
    """The cancelled path used to drop the temporary WAV and leave
    voice_messages/{local_id}.msv orphaned forever."""

    def _dirs(self, tmp_path):
        voice = tmp_path / "voice_messages"
        media = tmp_path / "media"
        voice.mkdir()
        media.mkdir()
        return str(voice), str(media)

    def test_a_restored_message_takes_its_cached_copies_with_it(self, tmp_path):
        voice, media = self._dirs(tmp_path)
        open(os.path.join(voice, "loc-1.msv"), "wb").close()
        open(os.path.join(media, "loc-1.wzmedia"), "wb").close()

        conversations.promote_local_media_cache(voice, media, "loc-1", "REAL_A")

        assert os.path.isfile(os.path.join(voice, "REAL_A.msv"))
        assert os.path.isfile(os.path.join(media, "REAL_A.wzmedia"))
        assert not os.path.isfile(os.path.join(voice, "loc-1.msv"))

    def test_an_existing_copy_under_the_real_id_is_not_overwritten(self, tmp_path):
        voice, media = self._dirs(tmp_path)
        with open(os.path.join(voice, "loc-1.msv"), "wb") as handle:
            handle.write(b"local")
        with open(os.path.join(voice, "REAL_A.msv"), "wb") as handle:
            handle.write(b"already downloaded")

        conversations.promote_local_media_cache(voice, media, "loc-1", "REAL_A")

        with open(os.path.join(voice, "REAL_A.msv"), "rb") as handle:
            assert handle.read() == b"already downloaded"

    def test_a_revoked_message_leaves_nothing_behind(self, tmp_path):
        voice, media = self._dirs(tmp_path)
        open(os.path.join(voice, "loc-1.msv"), "wb").close()
        open(os.path.join(media, "loc-1.wzmedia"), "wb").close()

        conversations.discard_local_media_cache(voice, media, "loc-1")

        assert os.listdir(voice) == []
        assert os.listdir(media) == []

    def test_missing_files_are_not_an_error(self, tmp_path):
        voice, media = self._dirs(tmp_path)

        conversations.promote_local_media_cache(voice, media, "loc-1", "REAL_A")
        conversations.discard_local_media_cache(voice, media, "loc-1")


# ── The main-window hand-off ─────────────────────────────────────────────────

class _SentStub:
    """MainWindow._on_message_sent() against a panel that already cancelled."""

    _on_message_sent = MainWindow._on_message_sent
    _is_cancelled_send = MainWindow._is_cancelled_send

    def __init__(self, cancelled):
        self.conversations_panel = types.SimpleNamespace(
            _is_cancelled_pending=lambda local_id: local_id in cancelled,
            _mark_message_sent=lambda *a, **kw: self.marked.append(a),
        )
        self.marked = []
        self.completed = []
        self.db = types.SimpleNamespace(update_message_id=lambda *a: None)

    def _on_cancelled_message_delivered(self, *args):
        self.completed.append(args)


class TestASendThatBeatItsCancelToTheUiThread:
    def test_it_completes_the_cancellation_instead_of_marking_it_sent(self):
        stub = _SentStub(cancelled={"loc-1"})

        MainWindow._on_message_sent(stub, "loc-1", None, "REAL_A", CHAT, True)

        assert stub.completed == [("loc-1", "REAL_A", CHAT, None, True)]
        assert stub.marked == []

    def test_an_uncancelled_message_takes_the_normal_path(self):
        stub = _SentStub(cancelled=set())

        MainWindow._on_message_sent(stub, "loc-1", None, "REAL_A", CHAT, False)

        assert stub.completed == []
        assert stub.marked


class TestTheMainWindowHandOff:
    """MainWindow._on_cancelled_message_delivered/_dropped own the temporary WAV
    and must not swallow an outcome when there is no panel to hand it to."""

    class _Stub:
        _on_cancelled_message_delivered = MainWindow._on_cancelled_message_delivered
        _on_cancelled_message_dropped = MainWindow._on_cancelled_message_dropped
        _discard_temp_recording = staticmethod(MainWindow._discard_temp_recording)

        def __init__(self, with_panel=True):
            self.completed = []
            self.discarded = []
            if with_panel:
                self.conversations_panel = types.SimpleNamespace(
                    complete_cancelled_message_delivery=(
                        lambda *a: self.completed.append(a)
                    ),
                    discard_cancelled_message=lambda lid: self.discarded.append(lid),
                )

    def test_the_temporary_wav_is_deleted_on_both_outcomes(self, tmp_path):
        for name in ("delivered.wav", "dropped.wav"):
            path = str(tmp_path / name)
            with open(path, "wb") as handle:
                handle.write(b"wav")

        stub = self._Stub()
        stub._on_cancelled_message_delivered(
            "loc-1", "REAL_A", CHAT, str(tmp_path / "delivered.wav")
        )
        stub._on_cancelled_message_dropped("loc-2", str(tmp_path / "dropped.wav"))

        assert not os.path.isfile(str(tmp_path / "delivered.wav"))
        assert not os.path.isfile(str(tmp_path / "dropped.wav"))

    def test_the_panel_is_told_which_outcome_it_was(self):
        stub = self._Stub()

        stub._on_cancelled_message_delivered("loc-1", "REAL_A", CHAT, None, True)
        stub._on_cancelled_message_dropped("loc-2", None)

        assert stub.completed == [("loc-1", "REAL_A", CHAT, True, False)]
        assert stub.discarded == ["loc-2"]

    def test_an_unknown_outcome_is_passed_on_as_unknown(self):
        stub = self._Stub()

        stub._on_cancelled_message_delivered("loc-1", ambiguous=True)

        assert stub.completed == [("loc-1", None, None, False, True)]

    def test_a_missing_panel_is_survivable(self):
        stub = self._Stub(with_panel=False)

        stub._on_cancelled_message_delivered("loc-1", "REAL_A", CHAT, None)
        stub._on_cancelled_message_dropped("loc-2", None)
