"""What an incremental delta round is allowed to conclude.

The incremental sync planner asks get-messages for a narrow window on chats
whose activity marker moved. Two of its outcomes needed sharper definitions
than "the fetch worked / it didn't", because the difference decides whether the
whole sync is allowed to declare itself complete — and _sync_completed gates
the chat-list snapshot save, the health checker's resync loop, and the live
chats.update unread path for the rest of the session.

MainWindow is a wx.Frame and cannot be instantiated without a running app, so
sync_chat_messages() is exercised against a stub carrying just the state it
touches — same shape as tests/test_deep_sync.py's _MessagesStub.
"""

import threading

import main as main_module
from main import MainWindow


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _FakeDb:
    def __init__(self):
        self.upserted = []
        self.inserted = []
        # A single ordered log of every durable write the round makes, the
        # repair-state ones included. Which writes happen is only half of the
        # contract here — see TestTheDurableWritesAreOrderedForACrash.
        self.calls = []

    def upsert_chat(self, jid, data):
        self.upserted.append(jid)
        self.calls.append("upsert_chat")

    def insert_messages_batch(self, jid, messages):
        self.inserted.append((jid, messages))
        self.calls.append("insert_messages_batch")


class _DeltaStub:
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _needs_display_page_refill = staticmethod(MainWindow._needs_display_page_refill)
    _backfill_state_guard = MainWindow._backfill_state_guard
    _canonical_backfill_jid = MainWindow._canonical_backfill_jid
    _counts_as_last_message = MainWindow._counts_as_last_message
    _is_backfill_pending = MainWindow._is_backfill_pending
    _is_conversation_open_jid = lambda self, jid: False
    _schedule_set_chats = lambda self: None
    # Read off MainWindow rather than left to sync_chat_messages()'s getattr
    # default, so the warm window under test is the shipped one.
    _INCREMENTAL_MESSAGE_WINDOW = MainWindow._INCREMENTAL_MESSAGE_WINDOW

    def __init__(self, normalized=None):
        self.settings = {"user_interface": {"messages_page_size": 200}}
        self._phone_to_lid = {}
        self._lid_to_phone = {}
        self._wa_connected = True
        self.chats = {}
        self._initial_sync_running = True
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6308
        self.token = "tok"
        self.db = _FakeDb()
        self._sync_failures_lock = threading.Lock()
        self._sync_failed_chats = set()
        self._delta_unsatisfied_chats = set()
        self._delta_unsatisfied_attempts = {}
        self._gap_chats = set()
        self._gap_candidate_chats = set()
        self._backfill_pending = set()
        self._backfill_retries = {}
        self._normalized = normalized if normalized is not None else []

    def _persist_history_gap_jids(self):
        self.db.calls.append("persist_history_gap_jids")

    def _persist_backfill_pending_state(self):
        self.db.calls.append("persist_backfill_pending_state")

    def _refetch_history_gap(self, remote_jid, fetch_jid, headers, page_size,
                             reference, hole_top_ts):
        # A widening that comes back with nothing: WhatsApp Web routinely never
        # decodes the missing stretch, so the gap stays open and the chat is
        # left queued. Reached only by the tests that saturate the window.
        return []

    def _extract_lid_mapping(self, msg):
        pass

    def _schedule_save(self, **kwargs):
        pass

    def _is_cleared_message(self, remote_jid, message):
        return False

    def _note_backfill_state(self, remote_jid, chat, api_ok):
        pass

    def _refresh_open_conversation_after_sync(self, remote_jid, chat):
        pass

    def _normalize_fetched_messages(self, raw_messages, remote_jid):
        return list(self._normalized)

    def _learn_sender_names_bulk(self, messages):
        return False

    def _jid_address_forms(self, jid):
        return MainWindow._jid_address_forms(self, jid)

    def _chat_jids_equivalent(self, left, right):
        return MainWindow._chat_jids_equivalent(self, left, right)


# sync_chat_messages() only takes the incremental branch for a chat that
# already holds local records — a cold chat always gets a full page.
def _seed_warm_chat(stub, jid="5511900000000@s.whatsapp.net"):
    stub.chats[jid] = {
        "remoteJid": jid,
        "t": 100,
        "messages": {"messages": {"records": [{
            "key": {"remoteJid": jid, "id": "old", "fromMe": False},
            "message": {"conversation": "anterior"},
            "messageType": "conversation",
            "messageTimestamp": 50,
        }]}},
    }


def _ok_empty(monkeypatch):
    monkeypatch.setattr(
        main_module.requests, "get",
        lambda url, **kwargs: _Resp(200, {"response": []}),
    )


class TestAnEmptyDeltaIsNotAFailedFetch:
    """A 200 with nothing behind it is a routine, and often permanent, state.

    A reaction, a groupNotification, anything _normalize_fetched_messages()
    filters out entirely will bump the chat's activity marker with nothing
    fetchable behind it. Reported as a failure, one such chat holds
    message_sync_ok False, and _sync_completed never becomes True again for the
    rest of the session.
    """

    def test_the_round_is_still_reported_as_successful(self, monkeypatch):
        _ok_empty(monkeypatch)
        stub = _DeltaStub()
        _seed_warm_chat(stub)
        chat = {"remoteJid": "5511900000000@s.whatsapp.net", "t": 100}
        assert MainWindow.sync_chat_messages(stub, chat, sync_mode="incremental") is True

    def test_but_the_chat_is_queued_to_be_looked_at_again(self, monkeypatch):
        _ok_empty(monkeypatch)
        stub = _DeltaStub()
        _seed_warm_chat(stub)
        chat = {"remoteJid": "5511900000000@s.whatsapp.net", "t": 100}
        MainWindow.sync_chat_messages(stub, chat, sync_mode="incremental")
        assert stub._delta_unsatisfied_chats == {"5511900000000@s.whatsapp.net"}

    def test_and_the_activity_marker_is_not_committed_yet(self, monkeypatch):
        """The deferred-save invariant is unchanged: never persist a new `t`
        without the message that marker selected."""
        _ok_empty(monkeypatch)
        stub = _DeltaStub()
        _seed_warm_chat(stub)
        chat = {"remoteJid": "5511900000000@s.whatsapp.net", "t": 100}
        MainWindow.sync_chat_messages(stub, chat, sync_mode="incremental")
        assert stub.db.upserted == []

    def test_a_real_io_failure_is_still_a_failure(self, monkeypatch):
        # The retry backoff is real time; the decision under test is not.
        monkeypatch.setattr(main_module.time, "sleep", lambda *_: None)
        monkeypatch.setattr(
            main_module.requests, "get",
            lambda url, **kwargs: _Resp(500, {"error": "boom"}),
        )
        stub = _DeltaStub()
        _seed_warm_chat(stub)
        chat = {"remoteJid": "5511900000000@s.whatsapp.net", "t": 100}
        assert MainWindow.sync_chat_messages(stub, chat, sync_mode="incremental") is False

    def test_retrying_terminates_and_accepts_the_marker(self, monkeypatch):
        """Otherwise the chat is re-queried on every round of every session and
        never leaves the retry list — the marker can never be satisfied because
        there is nothing to fetch."""
        _ok_empty(monkeypatch)
        stub = _DeltaStub()
        _seed_warm_chat(stub)
        for _ in range(main_module._MAX_EMPTY_DELTA_RETRIES):
            MainWindow.sync_chat_messages(
                stub, {"remoteJid": "5511900000000@s.whatsapp.net", "t": 100}, sync_mode="incremental"
            )
        assert stub._delta_unsatisfied_chats == set()
        assert stub.db.upserted == ["5511900000000@s.whatsapp.net"]

    def test_a_later_non_empty_delta_clears_the_latch(self, monkeypatch):
        _ok_empty(monkeypatch)
        stub = _DeltaStub()
        _seed_warm_chat(stub)
        MainWindow.sync_chat_messages(
            stub, {"remoteJid": "5511900000000@s.whatsapp.net", "t": 100}, sync_mode="incremental"
        )
        assert stub._delta_unsatisfied_chats == {"5511900000000@s.whatsapp.net"}

        stub._normalized = [{
            "key": {"remoteJid": "5511900000000@s.whatsapp.net", "id": "m1", "fromMe": False},
            "message": {"conversation": "oi"},
            "messageType": "conversation",
            "messageTimestamp": 150,
        }]
        monkeypatch.setattr(
            main_module.requests, "get",
            lambda url, **kwargs: _Resp(200, {"response": [{"id": "m1"}]}),
        )
        MainWindow.sync_chat_messages(
            stub, {"remoteJid": "5511900000000@s.whatsapp.net", "t": 100}, sync_mode="incremental"
        )
        assert stub._delta_unsatisfied_chats == set()
        assert stub._delta_unsatisfied_attempts == {}


class TestTheActivityMarkerIsNeverLowered:
    """`t` is the server's activity clock, not the newest displayable message.

    It legitimately sits above the newest message the chat list can preview
    whenever the last thing that happened was a system event, which
    _counts_as_last_message() excludes by design. Writing the message's
    timestamp over it re-sorts the conversation against the server's own
    ordering, and leaves _capture_chat_sync_baseline() holding a marker that
    can never match the next list-chats snapshot — so that chat reports a
    change on every round and is never skipped again.
    """

    def _sync_with_message_at(self, monkeypatch, message_ts, server_t):
        stub = _DeltaStub(normalized=[{
            "key": {"remoteJid": "5511900000000@s.whatsapp.net", "id": "m1", "fromMe": False},
            "message": {"conversation": "oi"},
            "messageType": "conversation",
            "messageTimestamp": message_ts,
        }])
        monkeypatch.setattr(
            main_module.requests, "get",
            lambda url, **kwargs: _Resp(200, {"response": [{"id": "m1"}]}),
        )
        chat = {"remoteJid": "5511900000000@s.whatsapp.net", "t": server_t}
        MainWindow.sync_chat_messages(stub, chat)
        return stub.chats["5511900000000@s.whatsapp.net"]

    def test_a_newer_message_raises_it(self, monkeypatch):
        chat = self._sync_with_message_at(monkeypatch, message_ts=500, server_t=100)
        assert chat["t"] == 500

    def test_a_system_event_above_the_last_message_keeps_the_server_value(self, monkeypatch):
        chat = self._sync_with_message_at(monkeypatch, message_ts=100, server_t=500)
        assert chat["t"] == 500

    def test_the_preview_still_follows_the_newest_displayable_message(self, monkeypatch):
        chat = self._sync_with_message_at(monkeypatch, message_ts=100, server_t=500)
        assert chat["lastMessage"]["key"]["id"] == "m1"


class _RoundStub:
    """Stand-in for sync_remote_chats() — the level where an unsatisfied delta
    used to become a failed sync run."""

    def __init__(self, chats, unsatisfied=(), failing=()):
        self.chats = {c["remoteJid"]: c for c in chats}
        self.settings = {"user_interface": {"messages_page_size": 200}}
        self._sync_failures_lock = threading.Lock()
        self._sync_failed_chats = set()
        self._delta_unsatisfied_chats = set(unsatisfied)
        self._delta_unsatisfied_attempts = {}
        self._message_retry_jids = set()
        self._history_still_landing = False
        self._failing = set(failing)

    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def history_page_target(self):
        return 200

    def sync_chat_messages(self, chat, expected_run_id=None, sync_mode="full"):
        return chat.get("remoteJid") not in self._failing

    def _persist_message_retry_jids(self):
        pass

    def _persist_backfill_pending_state(self):
        pass

    def _persist_history_gap_jids(self):
        pass


A = "5511900000000@s.whatsapp.net"
B = "5511911111111@s.whatsapp.net"


class TestAnUnsatisfiedDeltaDoesNotFailTheRound:
    """The whole point of separating the two. sync_remote_chats()'s return is
    what _run_sync() reads as message_sync_ok, and message_sync_ok False keeps
    _sync_completed False — which never commits the list-chats snapshot of
    unread/pin/archive for EVERY chat, leaves the health checker resyncing on
    every cooldown, and drops every live chats.update unread event for the rest
    of the session. One chat with a reaction-only bump was enough.
    """

    def test_the_round_reports_no_failures(self):
        stub = _RoundStub(
            [{"remoteJid": A, "t": 100}, {"remoteJid": B, "t": 90}],
            unsatisfied={A},
        )
        assert MainWindow.sync_remote_chats(stub, incremental=True) == set()

    def test_but_the_chat_stays_on_the_durable_retry_list(self):
        stub = _RoundStub(
            [{"remoteJid": A, "t": 100}, {"remoteJid": B, "t": 90}],
            unsatisfied={A},
        )
        MainWindow.sync_remote_chats(stub, incremental=True)
        assert stub._message_retry_jids == {A}

    def test_a_real_failure_is_still_returned_as_one(self):
        stub = _RoundStub(
            [{"remoteJid": A, "t": 100}, {"remoteJid": B, "t": 90}],
            failing={B},
        )
        assert MainWindow.sync_remote_chats(stub, incremental=True) == {B}
        assert stub._message_retry_jids == {B}

    def test_a_satisfied_chat_leaves_the_retry_list(self):
        """Termination at the round level: once the delta lands, the latch has
        to drain or the chat is re-fetched forever."""
        stub = _RoundStub([{"remoteJid": A, "t": 100}])
        stub._message_retry_jids = {A}
        MainWindow.sync_remote_chats(stub, incremental=True)
        assert stub._message_retry_jids == set()


class TestTheWarmWindowIsBoundedAndAdaptive:
    """A warm round must not re-download a full page per changed chat.

    get-messages has no `after_id`, so the closest available thing is to ask
    for a narrow newest-message window and widen it only when that window
    turns out not to touch anything already stored — which is the signal that
    the conversation outran the window while WinZapp was closed. Growth stops
    at the configured page size, so a long outage repairs itself and a common
    reconnect costs 50 messages instead of 200.

    The growth rule itself is a pure function covered by
    tests/test_incremental_sync.py::TestAdaptiveWindow; what is exercised here
    is that sync_chat_messages() actually asks with it.
    """

    def _counts_requested(self, monkeypatch, stub, response_size):
        counts = []

        def _get(url, **kwargs):
            count = int(url.rsplit("count=", 1)[1])
            counts.append(count)
            size = count if response_size == "saturated" else response_size
            return _Resp(200, {"response": [{"id": f"r{i}"} for i in range(size)]})

        monkeypatch.setattr(main_module.requests, "get", _get)
        MainWindow.sync_chat_messages(
            stub, {"remoteJid": "5511900000000@s.whatsapp.net", "t": 100},
            sync_mode="incremental",
        )
        return counts

    def test_the_first_request_is_the_narrow_window_not_the_page(self, monkeypatch):
        stub = _DeltaStub()
        _seed_warm_chat(stub)
        counts = self._counts_requested(monkeypatch, stub, response_size=0)
        assert counts == [MainWindow._INCREMENTAL_MESSAGE_WINDOW]

    def test_a_saturated_window_with_no_local_overlap_widens_to_the_page(self, monkeypatch):
        """Every message came back newer than everything stored and the window
        was full, so there is no way to know how much is missing behind it —
        keep doubling until the page size proves it either way."""
        stub = _DeltaStub(normalized=[{
            "key": {"remoteJid": "5511900000000@s.whatsapp.net", "id": "new", "fromMe": False},
            "message": {"conversation": "oi"},
            "messageType": "conversation",
            "messageTimestamp": 900,
        }])
        _seed_warm_chat(stub)
        counts = self._counts_requested(monkeypatch, stub, response_size="saturated")
        window = MainWindow._INCREMENTAL_MESSAGE_WINDOW
        page_size = stub.settings["user_interface"]["messages_page_size"]
        assert counts == [window, 2 * window, page_size]

    def test_a_window_that_overlaps_the_cache_never_widens(self, monkeypatch):
        """Overlap proves nothing is missing between the two blocks, which is
        the whole point of the narrow window — one 50-message request."""
        stub = _DeltaStub(normalized=[{
            "key": {"remoteJid": "5511900000000@s.whatsapp.net", "id": "old", "fromMe": False},
            "message": {"conversation": "anterior"},
            "messageType": "conversation",
            "messageTimestamp": 50,
        }])
        _seed_warm_chat(stub)
        counts = self._counts_requested(monkeypatch, stub, response_size="saturated")
        assert counts == [MainWindow._INCREMENTAL_MESSAGE_WINDOW]


class TestTheDurableWritesAreOrderedForACrash:
    """Every write this method makes is ordered against the activity marker.

    The marker is what the next launch reads to decide the chat needs nothing.
    So it is written last, and only once everything that would make the next
    launch come back for the chat is already on disk: the message rows, the
    known history gap, the short-history repair queue. A process killed
    anywhere in between then leaves at worst one redundant re-fetch, never a
    conversation that no future round will ever look at again.
    """

    def _sync_one(self, monkeypatch, stub):
        monkeypatch.setattr(
            main_module.requests, "get",
            lambda url, **kwargs: _Resp(200, {"response": [{"id": "m1"}]}),
        )
        MainWindow.sync_chat_messages(
            stub, {"remoteJid": "5511900000000@s.whatsapp.net", "t": 100})

    def _fetched_stub(self):
        return _DeltaStub(normalized=[{
            "key": {"remoteJid": "5511900000000@s.whatsapp.net", "id": "m1", "fromMe": False},
            "message": {"conversation": "oi"},
            "messageType": "conversation",
            "messageTimestamp": 150,
        }])

    def test_the_messages_land_before_the_marker(self, monkeypatch):
        stub = self._fetched_stub()
        self._sync_one(monkeypatch, stub)
        assert stub.db.calls[-2:] == ["insert_messages_batch", "upsert_chat"]

    def test_the_repair_state_lands_before_the_marker(self, monkeypatch):
        """A gap and a queued short history are both reasons to come back for
        this chat. Committing the marker first and dying would drop both."""
        stub = self._fetched_stub()
        stub._history_gap_jids = {"5511900000000@s.whatsapp.net"}
        stub._chats_awaiting_messages = {"5511900000000@s.whatsapp.net"}
        self._sync_one(monkeypatch, stub)

        calls = stub.db.calls
        marker = calls.index("upsert_chat")
        assert calls.index("persist_history_gap_jids") < marker
        assert calls.index("persist_backfill_pending_state") < marker
