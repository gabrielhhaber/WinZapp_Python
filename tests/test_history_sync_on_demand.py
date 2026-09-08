"""Tests for the on-demand history-sync path and its health reporting.

WhatsApp's multi-device design only pushes a bounded window of history to a
linked device and keeps the rest on the primary phone. WhatsApp Web surfaces
that as the "get older messages from your phone" banner; underneath it sends a
peer data operation request of type HISTORY_SYNC_ON_DEMAND, which WPPConnect
does not wrap — WinZapp reaches it through the /request-older-messages route
added to the patched deviceController.

Two behaviours matter enough to pin down here:

  * Running out of *local* history is not the same as running out of history.
    fetch_older_messages() used to add the chat to _exhausted_chats the moment
    the API answered with an empty list, and the guard at the top of that
    method then refused to ever query it again. Since the phone replies to an
    on-demand request asynchronously (a history-sync chunk, minutes later),
    that permanent marker would throw away the very messages the request went
    out to fetch. So: request sent => chat stays re-queryable; request not
    sent => mark exhausted exactly like before.

  * A dead history-sync pipeline is completely silent from WinZapp's side —
    get-messages keeps answering 200 with a short list, which looks identical
    to a genuinely short conversation. log_history_sync_status() exists to put
    that distinction in log.log instead of requiring a CDP session against the
    live page to find it.
"""

import logging

import pytest

from main import MainWindow


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Stub:
    """Carries only what the methods under test actually touch."""

    wpp_server = "http://127.0.0.1"
    wpp_port = 6300
    token = "tok"

    fetch_history_sync_status = MainWindow.fetch_history_sync_status
    log_history_sync_status = MainWindow.log_history_sync_status
    request_older_messages = MainWindow.request_older_messages
    unblock_history_sync = MainWindow.unblock_history_sync
    refresh_history_still_landing = MainWindow.refresh_history_still_landing
    wait_for_restarted_history_sync = MainWindow.wait_for_restarted_history_sync
    _history_session_is_gone = MainWindow._history_session_is_gone
    _HISTORY_WAIT_PROGRESS_SECONDS = MainWindow._HISTORY_WAIT_PROGRESS_SECONDS
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self, connected=True):
        self._wa_connected = connected
        self.offline_mode = False
        self._phone_to_lid = {}
        self._lid_to_phone = {}

    def _should_abort_sync_for_offline(self):
        return False


class TestFetchHistorySyncStatus:
    def test_returns_response_payload(self, monkeypatch):
        stub = _Stub()
        payload = {"backendWorkerBridgeReady": True, "storeCounts": {"message": 9000}}
        monkeypatch.setattr(
            "main.requests.get",
            lambda *a, **k: _Response(200, {"status": "success", "response": payload}),
        )
        assert stub.fetch_history_sync_status() == payload

    def test_disconnected_session_never_calls_the_api(self, monkeypatch):
        stub = _Stub(connected=False)

        def _boom(*a, **k):
            raise AssertionError("must not hit the API while disconnected")

        monkeypatch.setattr("main.requests.get", _boom)
        assert stub.fetch_history_sync_status() is None

    def test_missing_route_is_not_fatal(self, monkeypatch):
        """An older client/api/ build simply has no such endpoint."""
        stub = _Stub()
        monkeypatch.setattr("main.requests.get", lambda *a, **k: _Response(404, text="nope"))
        assert stub.fetch_history_sync_status() is None
        assert stub._history_status_disconnected is False

    def test_disconnected_response_is_remembered(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            "main.requests.get",
            lambda *a, **k: _Response(404, text='{"status":"Disconnected"}'))
        assert stub.fetch_history_sync_status() is None
        assert stub._history_status_disconnected is True

    def test_transport_error_is_swallowed(self, monkeypatch):
        stub = _Stub()

        def _raise(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr("main.requests.get", _raise)
        assert stub.fetch_history_sync_status() is None


class TestLogHistorySyncStatus:
    def test_dead_bridge_is_logged_at_error(self, monkeypatch, caplog):
        stub = _Stub()
        monkeypatch.setattr(
            _Stub,
            "fetch_history_sync_status",
            lambda self, timeout=30: {
                "backendWorkerBridgeReady": False,
                "unprocessedChunks": 20,
                "storeCounts": {"message": 1083, "chat": 604},
                "chunkStatus": {"1": "notification_stored", "2": "notification_stored"},
            },
        )
        with caplog.at_level(logging.INFO):
            stub.log_history_sync_status(context="after initial sync")
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "a dead bridge must be loud — it invalidates every sync"
        assert "backend worker bridge is DOWN" in errors[0].getMessage()

    def test_chunk_states_are_tallied(self, monkeypatch, caplog):
        stub = _Stub()
        monkeypatch.setattr(
            _Stub,
            "fetch_history_sync_status",
            lambda self, timeout=30: {
                "backendWorkerBridgeReady": True,
                "unprocessedChunks": 0,
                "storeCounts": {"message": 50000, "chat": 604},
                "chunkStatus": {
                    "1": "notification_stored",
                    "2": "decoded",
                    "3": "decoded",
                },
            },
        )
        with caplog.at_level(logging.INFO):
            stub.log_history_sync_status()
        tallies = [r.getMessage() for r in caplog.records if "chunk states" in r.getMessage()]
        assert tallies
        assert "'decoded': 2" in tallies[0]
        assert "'notification_stored': 1" in tallies[0]

    def test_healthy_bridge_logs_no_error(self, monkeypatch, caplog):
        stub = _Stub()
        monkeypatch.setattr(
            _Stub,
            "fetch_history_sync_status",
            lambda self, timeout=30: {
                "backendWorkerBridgeReady": True,
                "unprocessedChunks": 0,
                "storeCounts": {"message": 50000, "chat": 604},
            },
        )
        with caplog.at_level(logging.INFO):
            assert stub.log_history_sync_status() is not None
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_no_status_available_returns_none(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(_Stub, "fetch_history_sync_status", lambda self, timeout=30: None)
        assert stub.log_history_sync_status(context="x") is None


class TestRequestOlderMessages:
    def test_posts_to_the_cus_form_for_a_phone_jid(self, monkeypatch):
        stub = _Stub()
        seen = {}

        def _post(url, **kwargs):
            seen["url"] = url
            return _Response(200, {"status": "success", "response": {"requested": True}})

        monkeypatch.setattr("main.requests.post", _post)
        assert stub.request_older_messages("5535999999999@s.whatsapp.net") is True
        assert seen["url"].endswith("/request-older-messages/5535999999999@c.us")

    def test_prefers_the_lid_form_when_one_is_mapped(self, monkeypatch):
        """Same addressing rule sync_chat_messages() uses."""
        stub = _Stub()
        stub._phone_to_lid["5535999999999@s.whatsapp.net"] = "12345@lid"
        seen = {}
        monkeypatch.setattr(
            "main.requests.post",
            lambda url, **k: (
                seen.__setitem__("url", url),
                _Response(200, {"status": "success", "response": {"requested": True}}),
            )[1],
        )
        stub.request_older_messages("5535999999999@s.whatsapp.net")
        assert seen["url"].endswith("/request-older-messages/12345@lid")

    def test_group_jid_is_passed_through_unchanged(self, monkeypatch):
        stub = _Stub()
        seen = {}
        monkeypatch.setattr(
            "main.requests.post",
            lambda url, **k: (
                seen.__setitem__("url", url),
                _Response(200, {"status": "success", "response": {"requested": True}}),
            )[1],
        )
        stub.request_older_messages("120363000000000000@g.us")
        assert seen["url"].endswith("/request-older-messages/120363000000000000@g.us")

    def test_server_refusal_reports_false(self, monkeypatch):
        """WhatsApp Web disables on-demand sending after repeated failures."""
        stub = _Stub()
        monkeypatch.setattr(
            "main.requests.post",
            lambda *a, **k: _Response(
                500,
                {"status": "error", "response": {"error": "on-demand requests disabled"}},
            ),
        )
        assert stub.request_older_messages("120363000000000000@g.us") is False

    def test_200_without_requested_flag_is_not_a_success(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            "main.requests.post",
            lambda *a, **k: _Response(200, {"status": "success", "response": {"requested": False}}),
        )
        assert stub.request_older_messages("120363000000000000@g.us") is False

    def test_disconnected_session_never_calls_the_api(self, monkeypatch):
        stub = _Stub(connected=False)

        def _boom(*a, **k):
            raise AssertionError("must not hit the API while disconnected")

        monkeypatch.setattr("main.requests.post", _boom)
        assert stub.request_older_messages("120363000000000000@g.us") is False

    def test_transport_error_is_swallowed(self, monkeypatch):
        stub = _Stub()

        def _raise(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr("main.requests.post", _raise)
        assert stub.request_older_messages("120363000000000000@g.us") is None


class TestRefreshHistoryStillLanding:
    """The flag that tells the backfill whether a short chat is short or early.

    _note_backfill_state() runs on the sync's worker threads and must not make
    its own status call per chat, so this is read once per pass and left on the
    instance.
    """

    def _stub(self, status, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status", lambda self, timeout=30: status)
        return stub

    def test_queued_chunks_mean_history_is_still_landing(self, monkeypatch):
        stub = self._stub({"unprocessedChunks": 12, "initialSyncComplete": True}, monkeypatch)
        assert stub.refresh_history_still_landing() is True
        assert stub._history_still_landing is True

    def test_a_fresh_pairing_counts_even_with_an_empty_queue(self, monkeypatch):
        """The new-pairing case, and the reason the chunk count alone is not
        enough: at the moment the first sync runs the phone has usually not
        delivered anything yet, so an empty queue there means "nothing has
        arrived", not "nothing is coming"."""
        stub = self._stub({"unprocessedChunks": 0, "initialSyncComplete": False}, monkeypatch)
        assert stub.refresh_history_still_landing() is True

    def test_a_settled_session_is_settled(self, monkeypatch):
        stub = self._stub({"unprocessedChunks": 0, "initialSyncComplete": True}, monkeypatch)
        stub._history_still_landing = True
        assert stub.refresh_history_still_landing() is False
        assert stub._history_still_landing is False


    def test_an_incomplete_recent_sync_keeps_history_landing(self, monkeypatch):
        stub = self._stub(
            {"unprocessedChunks": 0, "initialSyncComplete": True, "recentCompleted": False},
            monkeypatch)
        assert stub.refresh_history_still_landing() is True
        assert stub._history_still_landing is True

    def test_an_unreadable_status_is_safe_before_the_first_check(self, monkeypatch):
        stub = self._stub(None, monkeypatch)
        assert stub.refresh_history_still_landing() is True
        assert stub._history_still_landing is True

    def test_an_unreadable_status_preserves_the_last_known_state(self, monkeypatch):
        stub = self._stub(None, monkeypatch)
        stub._history_still_landing = False
        assert stub.refresh_history_still_landing() is False
        assert stub._history_still_landing is False


class TestWaitForRestartedHistorySync:
    def _clock(self, monkeypatch):
        clock = [0.0]
        monkeypatch.setattr("main.time.monotonic", lambda: clock[0])
        monkeypatch.setattr(
            "main.time.sleep",
            lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def test_stable_count_is_not_completion_while_recent_is_false(self, monkeypatch):
        self._clock(monkeypatch)
        stub = _Stub()
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status", lambda self, timeout=10: {
                "unprocessedChunks": 0,
                "recentCompleted": False,
                "storeCounts": {"message": 10199},
            })

        assert stub.wait_for_restarted_history_sync(timeout=8) is False

    def test_recent_true_and_empty_queue_completes_wait(self, monkeypatch):
        self._clock(monkeypatch)
        statuses = iter((
            {"unprocessedChunks": 0, "recentCompleted": False,
             "storeCounts": {"message": 10199}},
            {"unprocessedChunks": 0, "recentCompleted": True,
             "storeCounts": {"message": 39288}},
        ))
        stub = _Stub()
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status",
            lambda self, timeout=10: next(statuses))

        assert stub.wait_for_restarted_history_sync(timeout=8) is True

    def test_a_transient_unreadable_status_does_not_end_the_wait(self, monkeypatch):
        """Reported live: one 503 ``session_not_ready`` / "WAPI is not
        defined" 5m20s into the 10-minute budget ended the wait, which made
        _run_sync() defer its whole message phase — on a session whose RECENT
        pass went on to complete 10 minutes later. WhatsApp Web re-injects
        WAPI routinely while a big history transfer runs, so an unreadable
        status mid-wait is the expected case, not a failure.
        """
        self._clock(monkeypatch)
        statuses = iter((
            {"unprocessedChunks": 3, "recentCompleted": False,
             "storeCounts": {"message": 10199}},
            None,   # the 503: session still up, page just not ready yet
            None,
            {"unprocessedChunks": 0, "recentCompleted": True,
             "storeCounts": {"message": 115761}},
        ))
        stub = _Stub()
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status",
            lambda self, timeout=10: next(statuses))

        assert stub.wait_for_restarted_history_sync(timeout=60) is True

    def test_an_unreadable_status_ends_the_wait_once_the_session_is_gone(
        self, monkeypatch
    ):
        """The other half: tolerating a transient failure must not turn a real
        disconnect into a ten-minute stall."""
        self._clock(monkeypatch)
        stub = _Stub()
        stub._wa_connected = False
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status", lambda self, timeout=10: None)

        assert stub.wait_for_restarted_history_sync(timeout=600) is False

    def test_a_permanently_unreadable_status_still_stops_at_the_deadline(
        self, monkeypatch
    ):
        """Tolerance is bounded by the timeout, not unbounded: a session that
        stays up but never answers again gives up when the budget runs out."""
        self._clock(monkeypatch)
        stub = _Stub()
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status", lambda self, timeout=10: None)

        assert stub.wait_for_restarted_history_sync(timeout=8) is False

    def test_the_outcome_records_which_kind_of_false_this_was(self, monkeypatch):
        """The caller has to tell the two False cases apart: a budget that ran
        out while history was still arriving is a reason to expect short
        chats, not a reason to skip the message phase — see _run_sync()."""
        self._clock(monkeypatch)
        stub = _Stub()
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status", lambda self, timeout=10: {
                "unprocessedChunks": 4,
                "recentCompleted": False,
                "storeCounts": {"message": 1200},
            })

        assert stub.wait_for_restarted_history_sync(timeout=8) is False
        assert stub._history_wait_outcome == "timeout"

    def test_a_gone_session_is_recorded_as_such_and_not_as_a_timeout(self, monkeypatch):
        self._clock(monkeypatch)
        stub = _Stub()
        stub._wa_connected = False
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status", lambda self, timeout=10: None)

        assert stub.wait_for_restarted_history_sync(timeout=600) is False
        assert stub._history_wait_outcome == "session_gone"

    def test_an_offline_abort_is_recorded_as_a_gone_session(self, monkeypatch):
        self._clock(monkeypatch)
        stub = _Stub()
        monkeypatch.setattr(_Stub, "_should_abort_sync_for_offline", lambda self: True)

        assert stub.wait_for_restarted_history_sync(timeout=600) is False
        assert stub._history_wait_outcome == "session_gone"

    def test_completion_is_recorded_too(self, monkeypatch):
        self._clock(monkeypatch)
        stub = _Stub()
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status", lambda self, timeout=10: {
                "unprocessedChunks": 0,
                "recentCompleted": True,
                "storeCounts": {"message": 39288},
            })

        assert stub.wait_for_restarted_history_sync(timeout=8) is True
        assert stub._history_wait_outcome == "completed"

    def test_the_wait_reports_progress_while_it_waits(self, monkeypatch, caplog):
        """It polls every 2s for up to 10 minutes; logging nothing until it
        fails made "the phone is still transferring" and "this is wedged"
        indistinguishable in log.log."""
        self._clock(monkeypatch)
        stub = _Stub()
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status", lambda self, timeout=10: {
                "unprocessedChunks": 7,
                "recentCompleted": False,
                "storeCounts": {"message": 4211, "chat": 130},
            })

        with caplog.at_level(logging.INFO):
            stub.wait_for_restarted_history_sync(timeout=60)

        progress = [r for r in caplog.records if "of budget left" in r.getMessage()]
        assert progress, "the wait logged nothing at all while waiting"
        first = progress[0].getMessage()
        assert "messages=4211" in first
        assert "chats=130" in first
        assert "unprocessed=7" in first
        # Throttled, not once per 2s poll: 60s of waiting at a 15s interval.
        assert len(progress) <= 6, f"too chatty: {len(progress)} lines"

    def test_incomplete_recent_waits_even_when_queue_was_not_restarted(self):
        payload = {
            "restarted": False,
            "recentCompleted": False,
            "unprocessed": 0,
        }

        assert MainWindow._recent_history_needs_wait(payload) is True

    def test_completed_recent_does_not_wait(self):
        payload = {
            "restarted": False,
            "recentCompleted": True,
            "unprocessed": 0,
        }

        assert MainWindow._recent_history_needs_wait(payload) is False

    def _stub(self, status, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            _Stub, "fetch_history_sync_status", lambda self, timeout=30: status)
        return stub

    def test_a_disconnected_status_stops_history_landing(self, monkeypatch):
        stub = self._stub(None, monkeypatch)
        stub._history_status_disconnected = True
        stub._history_still_landing = True
        assert stub.refresh_history_still_landing() is False
        assert stub._history_still_landing is False


def _history_sync_warnings(caplog):
    """WARNING+ messages from this feature only.

    caplog sees the whole root logger, and other modules' teardown noise
    (asyncio's "Task was destroyed but it is pending!") lands in it when the
    full suite runs — filtering on the tag keeps these assertions about the
    code under test.
    """
    return [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING and "[history-sync]" in r.getMessage()
    ]


class TestUnblockHistorySync:
    """The queue deadlock this endpoint exists to clear.

    WhatsApp Web takes the next chunk to process by sorting unprocessed
    notifications on *descending* syncType, so ON_DEMAND (6) always outranks
    RECENT (3) — while an ON_DEMAND chunk is only let through once
    `recentCompleted` is true, which stays false until the RECENT chunks queued
    behind it have been processed. Nothing breaks that tie on its own: the same
    row is picked and refused on every pass, forever. Measured live: four such
    chunks held 22 recent ones (~30MB of history) frozen; dropping them took
    WhatsApp Web's message store from 1,526 to 6,014 rows in 40 seconds.
    """

    def test_removed_chunks_are_reported_loudly(self, monkeypatch, caplog):
        stub = _Stub()
        monkeypatch.setattr(
            "main.requests.post",
            lambda *a, **k: _Response(200, {"status": "success", "response": {
                "recentCompleted": False, "unprocessed": 26, "recentWaiting": 22,
                "onDemandPending": 4, "removed": ["a", "b", "c", "d"],
                "restarted": True,
            }}),
        )
        with caplog.at_level(logging.INFO):
            payload = stub.unblock_history_sync()
        assert payload["removed"] == ["a", "b", "c", "d"]
        warnings = _history_sync_warnings(caplog)
        assert warnings, "dropping chunks is not a routine event — say so"
        assert "blocking 22" in warnings[0]

    def test_healthy_queue_is_a_quiet_no_op(self, monkeypatch, caplog):
        stub = _Stub()
        monkeypatch.setattr(
            "main.requests.post",
            lambda *a, **k: _Response(200, {"status": "success", "response": {
                "recentCompleted": True, "unprocessed": 0, "recentWaiting": 0,
                "onDemandPending": 0, "removed": [],
                "skipped": "recent sync complete — on-demand chunks can be processed",
            }}),
        )
        with caplog.at_level(logging.INFO):
            payload = stub.unblock_history_sync()
        assert payload["removed"] == []
        assert not _history_sync_warnings(caplog)

    def test_missing_route_is_not_fatal(self, monkeypatch):
        """An older client/api/ build simply has no such endpoint."""
        stub = _Stub()
        monkeypatch.setattr("main.requests.post", lambda *a, **k: _Response(404, text="nope"))
        assert stub.unblock_history_sync() is None

    def test_disconnected_session_never_calls_the_api(self, monkeypatch):
        stub = _Stub(connected=False)

        def _boom(*a, **k):
            raise AssertionError("must not hit the API while disconnected")

        monkeypatch.setattr("main.requests.post", _boom)
        assert stub.unblock_history_sync() is None

    def test_transport_error_is_swallowed(self, monkeypatch):
        stub = _Stub()

        def _raise(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr("main.requests.post", _raise)
        assert stub.unblock_history_sync() is None

    def test_restart_rehydrates_bootstrap_after_page_reload(self):
        """A fresh bootstrap instance otherwise rejects every manual restart."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        controller = (
            root / "client" / "api_patches" / "src" / "controller"
            / "deviceController.ts"
        ).read_text(encoding="utf-8")
        rehydrate_at = controller.index("setInitialChatHistorySynced")
        restart_at = controller.index(
            "continueSync.call(boot, source.ManualRestart)"
        )
        assert rehydrate_at < restart_at
        assert "initialComplete === true" in controller


class _FetchStub:
    """Just the exhaustion bookkeeping of fetch_older_messages()."""

    def __init__(self, request_succeeds):
        self._exhausted_chats = set()
        self._older_requested_chats = set()
        self._request_succeeds = request_succeeds
        self.requested_for = []

    def request_older_messages(self, jid, timeout=60):
        self.requested_for.append(jid)
        return self._request_succeeds


def _run_empty_result_branch(stub, remote_jid):
    """Mirror of the no-messages branch in fetch_older_messages()."""
    already_asked = remote_jid in stub._older_requested_chats
    requested = False
    if not already_asked:
        stub._older_requested_chats.add(remote_jid)
        requested = stub.request_older_messages(remote_jid)
    if not requested:
        stub._exhausted_chats.add(remote_jid)


class TestExhaustionBookkeeping:
    """A chat we just asked the phone about must stay re-queryable.

    fetch_older_messages() returns early for anything in _exhausted_chats, so
    marking a chat there right after firing an on-demand request would discard
    the reply the request exists to collect.
    """

    def test_successful_request_leaves_the_chat_requeryable(self):
        stub = _FetchStub(request_succeeds=True)
        _run_empty_result_branch(stub, "120363000000000000@g.us")
        assert stub.requested_for == ["120363000000000000@g.us"]
        assert "120363000000000000@g.us" not in stub._exhausted_chats

    def test_failed_request_marks_exhausted(self):
        stub = _FetchStub(request_succeeds=False)
        _run_empty_result_branch(stub, "120363000000000000@g.us")
        assert "120363000000000000@g.us" in stub._exhausted_chats

    def test_the_phone_is_asked_only_once_per_chat(self):
        """Second empty result gives up instead of re-asking the phone."""
        stub = _FetchStub(request_succeeds=True)
        jid = "120363000000000000@g.us"
        _run_empty_result_branch(stub, jid)
        _run_empty_result_branch(stub, jid)
        assert stub.requested_for == [jid]
        assert jid in stub._exhausted_chats


class TestBrowserFlagsStayRemoved:
    """The flags that break history sync must be gone from EVERY list.

    index.ts builds the effective config with `merge-deep`, which unions
    arrays instead of replacing them, so config.ts's browserArgs and the list
    start.js passes to initServer() are concatenated. start.js can therefore
    only ever *add* a flag — deleting one there while config.ts still lists it
    changes nothing, which is exactly how '--disable-notifications' survived
    its first removal and kept the Notification API undefined, and with it
    WhatsApp Web's persistent storage bucket.

    (Neither flag was the cause of the short-history bug — that was the blanket
    request interception, see TestDocumentOnlyInterception. They stay removed
    on their own merits: this page runs its backend in workers, and the
    Notification API is what Chrome's persistent-storage auto-grant keys off.)
    """

    FLAG_LISTS = (
        ("client/api_patches/start.js", r"^\s*'(--[^']+)',"),
        ("client/api_patches/src/config.ts", r"^\s*'(--[^']+)',"),
        ("client/api_patches/src/util/sessionUtil.ts", r"^\s*'(--[^']+)',"),
    )
    FORBIDDEN = ("--disable-notifications", "--disable-shared-workers")

    def _flags(self, relpath, pattern):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[1]
        text = (root / relpath).read_text(encoding="utf-8")
        # Only real array entries — the comments explaining the removal
        # legitimately name the flags.
        return re.findall(pattern, text, re.M)

    @pytest.mark.parametrize("relpath,pattern", FLAG_LISTS)
    def test_forbidden_flags_absent(self, relpath, pattern):
        flags = self._flags(relpath, pattern)
        assert flags, f"no flags parsed out of {relpath} — pattern went stale"
        for bad in self.FORBIDDEN:
            assert bad not in flags, f"{bad} is back in {relpath}"

    def test_lists_are_otherwise_intact(self):
        """Guards the parser above: a typo'd regex must not pass vacuously."""
        start = self._flags(*self.FLAG_LISTS[0])
        assert "--no-sandbox" in start
        assert "--disable-web-security" in start


class TestRoutesArePatched:
    """The endpoints these methods call must exist in the patched API."""

    def test_routes_declared(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        routes = (root / "client" / "api_patches" / "src" / "routes" / "index.ts").read_text(
            encoding="utf-8"
        )
        assert "/api/:session/request-older-messages/:phone" in routes
        assert "/api/:session/history-sync-status" in routes
        assert "/api/:session/unblock-history-sync" in routes

    def test_on_demand_requests_are_gated_on_recent_sync(self):
        """Sending one early deadlocks the queue — see TestUnblockHistorySync."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        controller = (
            root / "client" / "api_patches" / "src" / "controller" / "deviceController.ts"
        ).read_text(encoding="utf-8")
        send_at = controller.index("sendPeerDataOperationRequest(kind")
        guard_at = controller.index("out.recentCompleted !== true")
        assert guard_at < send_at, "the guard must run before the request goes out"

    def test_stale_recent_flag_is_repaired_only_for_an_empty_queue(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        controller = (
            root / "client" / "api_patches" / "src" / "controller" / "deviceController.ts"
        ).read_text(encoding="utf-8")

        empty_queue = controller.index("if (rows.length === 0)")
        repair = controller.index("recentCompleted: true", empty_queue)
        send = controller.index("sendPeerDataOperationRequest(kind")
        assert empty_queue < repair < send
        assert "out.unprocessed = rows.length" in controller

    def test_unchanged_short_pages_request_phone_history(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        main = (root / "client" / "main.py").read_text(encoding="utf-8")
        retry = main.index("An unchanged short page is not proof")
        request = main.index("self.request_older_messages(jid)", retry)
        keep = main.index("self._keep_backfill_pending(jid, now)", retry)
        completed = main.index("self._completed_backfill_targets(window)", retry)
        assert retry < keep < request < completed


class TestDocumentOnlyInterception:
    """Puppeteer's blanket request interception must not come back.

    page.setRequestInterception(true) — which is all WPPConnect's
    setWhatsappVersion() does to serve the pinned HTML — makes every CORS-mode
    request issued from a dedicated Worker hang forever, with no error on
    either side. Measured with plain puppeteer and no WhatsApp involved:
    interception off, a worker's cross-origin fetch answers 200 in ~560ms; on,
    it never returns (page-issued and no-cors requests are unaffected either
    way).

    WhatsApp Web boots its whole storage/decode backend in such a worker, whose
    init script imports its bundles from static.whatsapp.net. Those imports
    hung, the worker never signalled ready, and every history-sync chunk — each
    one awaiting that bridge — stayed undecoded. So start.js keeps the version
    pin but serves it through a raw-CDP Fetch.enable scoped to the document URL
    alone, and hands `version=undefined` on so WPPConnect installs nothing.
    """

    def _start_js(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        return (root / "client" / "api_patches" / "start.js").read_text(encoding="utf-8")

    def test_narrow_fetch_pattern_is_installed(self):
        start = self._start_js()
        assert "Fetch.enable" in start
        assert "urlPattern: WA_WEB_URL" in start
        assert "Fetch.fulfillRequest" in start

    def test_version_is_consumed_so_wppconnect_installs_nothing(self):
        """Passing the version through would re-enable the blanket interception."""
        start = self._start_js()
        assert "version = undefined;" in start
        assert "browserController.initWhatsapp = async function" in start

    def test_every_paused_request_is_answered(self):
        """A paused request nobody answers is the bug itself, not a fix for it."""
        start = self._start_js()
        for answer in ("Fetch.fulfillRequest", "Fetch.failRequest", "Fetch.continueRequest"):
            assert answer in start, f"{answer} branch missing from the handler"

    def test_dead_wapi_loader_is_gone(self):
        """WAPI.loadEarlierMessages calls chat.loadEarlierMsgs(), which current
        WhatsApp Web builds no longer have — every call threw immediately and
        the surrounding `catch { break; }` made it look like a working fix."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        controller = (
            root / "client" / "api_patches" / "src" / "controller" / "deviceController.ts"
        ).read_text(encoding="utf-8")
        # Match call sites only — the comment above the replacement explains
        # why the old call was wrong and legitimately names it.
        assert "await (window as any).WAPI.loadEarlierMessages(" not in controller
        assert "await (window as any).WPP.chat.loadEarlierMessages(" not in controller


class _InteractiveWaitStub:
    wait_for_older_messages = MainWindow.wait_for_older_messages
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self, responder):
        self._wa_connected = True
        self.offline_mode = False
        self.calls = []
        self._responder = responder

    def fetch_older_messages(
        self, jid, oldest, store_only=False, allow_phone_request=True
    ):
        self.calls.append(allow_phone_request)
        return self._responder(allow_phone_request, len(self.calls))


class TestInteractiveHistoryWait:
    def _clock(self, monkeypatch):
        clock = [0.0]
        monkeypatch.setattr("main.time.monotonic", lambda: clock[0])
        monkeypatch.setattr(
            "main.time.sleep",
            lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def test_polling_reads_passively_until_the_page_arrives(self, monkeypatch):
        self._clock(monkeypatch)
        stub = _InteractiveWaitStub(
            lambda allow, call: [{"key": {"id": "older"}}] if call == 2 else None)
        got = stub.wait_for_older_messages(
            "120363000000000000@g.us", {"key": {"id": "anchor"}},
            timeout=5, poll_interval=1, retry_request_every=10)
        assert got and got[0]["key"]["id"] == "older"
        assert stub.calls == [False, False]

    def test_temporarily_refused_request_is_retried_interactively(self, monkeypatch):
        self._clock(monkeypatch)
        stub = _InteractiveWaitStub(
            lambda allow, call: [{"key": {"id": "older"}}] if allow else None)
        got = stub.wait_for_older_messages(
            "5511999999999@s.whatsapp.net", {"key": {"id": "anchor"}},
            timeout=5, poll_interval=0.5, retry_request_every=1)
        assert got and got[0]["key"]["id"] == "older"
        assert stub.calls == [False, False, True]

    def test_switching_conversation_cancels_the_wait(self, monkeypatch):
        self._clock(monkeypatch)
        stub = _InteractiveWaitStub(lambda allow, call: None)
        got = stub.wait_for_older_messages(
            "chat@g.us", {"key": {"id": "anchor"}},
            timeout=5, should_continue=lambda: False)
        assert got is None
        assert stub.calls == []


class TestAPhoneWithNothingOlderIsNotAsked:
    """Issue #108: the iPhone flickering sync notifications while WinZapp works.

    Every on-demand request is a peer-data-operation the PHONE reacts to, and
    the phone tells its owner about it — iOS shows "Synchronizing WhatsApp with
    Google Chrome (Windows)…" on the lock screen and follows it, when the
    request yields nothing, with "Sync paused. Open WhatsApp to resume."

    WhatsApp Web already computes whether the phone has anything older
    (`primaryHasMoreMessagesReadyToLoad`), and deviceController.ts computed it
    and then sent the request anyway — `primaryHasMore` reached Python as a log
    field and nothing else. Measured on a real, fully-synced account: 34
    requests in one launch, SEVENTEEN answered primaryHasMore=false, and the
    same chats asked again in a later pass because nothing retired them.

    The verdict has to be False rather than None: False is the terminal answer
    _backfill_empty_chats() reads as "this chat has no older history", which is
    what drops it from the queue for good. None keeps it queued and asks again
    after the grace period — which is the loop being fixed.
    """

    def test_a_refusal_for_lack_of_older_history_is_terminal(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            "main.requests.post",
            lambda *a, **k: _Response(500, {"status": "error", "response": {
                "primaryHasMore": False,
                "error": "primary has no older messages for this chat",
            }}),
        )
        assert stub.request_older_messages("120363000000000000@g.us") is False

    def test_it_is_logged_as_the_ordinary_outcome_it_is(self, monkeypatch, caplog):
        """Roughly half the queue answers this way. A log full of 500s that are
        really "nothing to do" costs a diagnosis the next time something here
        is genuinely wrong."""
        stub = _Stub()
        monkeypatch.setattr(
            "main.requests.post",
            lambda *a, **k: _Response(500, {"status": "error", "response": {
                "primaryHasMore": False,
                "error": "primary has no older messages for this chat",
            }}),
        )
        with caplog.at_level("INFO"):
            stub.request_older_messages("120363000000000000@g.us")
        assert any("no older messages" in r.message for r in caplog.records)
        assert not any("did not go out" in r.message for r in caplog.records)

    def test_an_unknown_answer_is_not_read_as_nothing_older(self, monkeypatch):
        """null means the lookup failed, not that the phone is empty. Treating
        it as terminal would silently write off chats that do have history."""
        stub = _Stub()
        monkeypatch.setattr(
            "main.requests.post",
            lambda *a, **k: _Response(200, {"status": "success", "response": {
                "primaryHasMore": None, "requested": True,
            }}),
        )
        assert stub.request_older_messages("120363000000000000@g.us") is True

    def test_a_successful_send_still_reports_true(self, monkeypatch):
        """primaryHasMore true is the case the request exists for."""
        stub = _Stub()
        monkeypatch.setattr(
            "main.requests.post",
            lambda *a, **k: _Response(200, {"status": "success", "response": {
                "primaryHasMore": True, "requested": True,
            }}),
        )
        assert stub.request_older_messages("120363000000000000@g.us") is True

    def test_the_deferral_for_an_unfinished_recent_pass_still_wins(self, monkeypatch):
        """That one must stay None — the chat has to be asked again once RECENT
        finishes, so it may not be retired."""
        stub = _Stub()
        monkeypatch.setattr(
            "main.requests.post",
            lambda *a, **k: _Response(500, {"status": "error", "response": {
                "error": "recent history sync is not complete yet",
            }}),
        )
        assert stub.request_older_messages("120363000000000000@g.us") is None


class TestTheNodeSideRefusesBeforeSending:
    """The Python verdict above is only half of it: the point is that the
    request never reaches the phone, because the notification is raised by the
    send itself."""

    @staticmethod
    def _source():
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / "client" / "api_patches"
                / "src" / "controller" / "deviceController.ts").read_text(
                    encoding="utf-8")

    def test_the_refusal_precedes_the_send(self):
        # Scoped to requestOlderMessages' own body: the file mentions
        # sendPeerDataOperationRequest elsewhere, and a whole-file index would
        # compare against the wrong one.
        source = self._source()
        source = source[source.index("export async function requestOlderMessages("):]
        refusal = source.index("primary has no older messages for this chat")
        # The actual invocation, not the capability guard higher up that only
        # checks `typeof sender?.sendPeerDataOperationRequest`.
        send = source.index("await sender.sendPeerDataOperationRequest(")
        assert refusal < send, (
            "the primaryHasMore check must come before the send, or the phone "
            "is notified anyway and only the bookkeeping changes"
        )

    def test_only_an_explicit_false_refuses(self):
        """`null` is "the lookup failed". Refusing on it would silently stop
        all history backfill the day WhatsApp renames that internal module."""
        source = self._source()
        assert "if (out.primaryHasMore === false) {" in source


class TestBackfillPhoneRequestBudget:
    """The backfill may not ask the phone about one chat forever.

    Every request that actually goes out lights up the phone's lock screen
    ("Synchronizing WhatsApp with Google Chrome (Windows)…", then "Sync
    paused. Open WhatsApp to resume." when it yields nothing — issue #108).
    The primaryHasMore gate stopped the requests the phone itself refuses, but
    a chat whose endOfHistoryTransferType claims more history, that is asked,
    and that gains nothing, stays short of history_page_target() forever — so
    the every-15-minute re-ask never retires. Measured on a real account: the
    same four groups asked at 10:08, 10:23 and 10:38 in one run.
    """

    GRACE = MainWindow._OLDER_REQUEST_GRACE
    MAX = MainWindow._MAX_PHONE_HISTORY_REQUESTS

    def test_a_chat_never_asked_before_is_due(self):
        assert MainWindow._phone_history_request_due(
            None, 0, 1000.0, self.GRACE, self.MAX) is True

    def test_a_chat_asked_moments_ago_is_not_due(self):
        assert MainWindow._phone_history_request_due(
            1000.0, 1, 1000.0 + self.GRACE - 1, self.GRACE, self.MAX) is False

    def test_the_grace_elapsing_makes_a_second_ask_due(self):
        assert MainWindow._phone_history_request_due(
            1000.0, 1, 1000.0 + self.GRACE, self.GRACE, self.MAX) is True

    def test_the_attempt_budget_outranks_the_elapsed_grace(self):
        # This is the whole fix: without it the same chat is asked again every
        # _OLDER_REQUEST_GRACE for as long as the backfill runs.
        assert MainWindow._phone_history_request_due(
            1000.0, self.MAX, 1000.0 + self.GRACE * 100,
            self.GRACE, self.MAX) is False

    def test_the_budget_allows_one_genuine_retry(self):
        # A single lost request must not write the chat off, so the bound is a
        # retry rather than a one-shot.
        assert self.MAX >= 2
        assert MainWindow._phone_history_request_due(
            1000.0, self.MAX - 1, 1000.0 + self.GRACE,
            self.GRACE, self.MAX) is True

    def test_resetting_the_history_walk_clears_the_attempt_counters(self):
        # F5 / "resync everything" is the only escape from a wrong conclusion,
        # and it has to reach this bound too.
        stub = _Stub()
        stub._older_request_attempts = {"5511@s.whatsapp.net": 2}
        stub._persist_exhausted_chats = lambda: None
        stub._persist_older_requested = lambda: None
        MainWindow._forget_history_exhaustion(stub)
        assert stub._older_request_attempts == {}
