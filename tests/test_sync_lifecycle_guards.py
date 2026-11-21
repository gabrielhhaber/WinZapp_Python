"""Old sync I/O and queued clears cannot repopulate or delete newer state.

All dependencies are plain stubs; no threads, timers, waits, wx windows or API.
"""

from types import SimpleNamespace
import pytest

from core import sync_lifecycle as lifecycle
from main_window import contacts, chats_store, conversation_sync, backfill
from tests.test_deep_history_backfill import _fetch_stub, _msg
from tests.test_incremental_delta_outcomes import _DeltaStub, _seed_warm_chat
from tests.test_phone_side_sync import _ReconcileStub, _FakeConversationsPanel, _chat_with_records
from tests.test_history_sync_on_demand import _Stub as _RequestStub


class Response:
    text = ""

    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def window(**kwargs):
    return SimpleNamespace(_sync_run_id=1, token="t", wpp_server="http://fake",
                           wpp_port=1, **kwargs)


class TestOwnership:
    @pytest.mark.parametrize("field,value", [
        ("_sync_run_id", 2), ("token", "new"), ("wpp_server", "http://new"),
        ("wpp_port", 2), ("_shutting_down", True), ("_user_offline", True),
    ])
    def test_context_rejects_supersession(self, field, value):
        stub = window()
        context = lifecycle.capture_sync_context(stub)
        assert lifecycle.sync_context_is_current(stub, context)
        setattr(stub, field, value)
        assert not lifecycle.sync_context_is_current(stub, context)

    @pytest.mark.parametrize("shutdown", [False, True])
    def test_new_backfill_request_is_handed_over_once(self, monkeypatch, shutdown):
        workers, visited = [], []

        class Worker:
            def __init__(self, target, **kwargs):
                self.target, self.alive = target, False
                workers.append(self)

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def finish(self):
                self.alive = False
                self.target()

        monkeypatch.setattr(lifecycle.threading, "Thread", Worker)
        stub = window(_backfill_thread=None)
        stub._backfill_empty_chats = lambda expected_context: visited.append(expected_context.run)
        lifecycle.schedule_backfill(stub)
        lifecycle.schedule_backfill(stub)
        assert len(workers) == 1
        stub._sync_run_id = 2
        lifecycle.schedule_backfill(stub)
        stub._shutting_down = shutdown
        workers[0].finish()
        if shutdown:
            assert len(workers) == 1
            assert visited == []
            return
        assert len(workers) == 2
        workers[1].finish()
        assert visited == [2]
        assert len(workers) == 2

    def test_old_worker_finally_cannot_clear_another_owner(self, monkeypatch):
        targets = []
        class Worker:
            def __init__(self, target, **kwargs):
                targets.append(target)
            def start(self):
                pass
        monkeypatch.setattr(lifecycle.threading, "Thread", Worker)
        stub = window(_backfill_thread=None)
        replacement = object()
        def supersede_owner(expected_context):
            stub._history_worker = replacement
            stub._backfill_thread = replacement
        stub._backfill_empty_chats = supersede_owner
        lifecycle.schedule_backfill(stub)
        targets[0]()
        assert stub._history_worker is replacement
        assert stub._backfill_thread is replacement

    def test_shutdown_prevents_backfill_takeover(self, monkeypatch):
        calls = []
        monkeypatch.setattr(lifecycle.threading, "Thread", lambda **kwargs: calls.append(kwargs))
        stub = window(_shutting_down=True)
        lifecycle.schedule_backfill(stub)
        assert calls == []


class TestOlderPages:
    def test_wipe_during_get_never_inserts_an_old_page(self, monkeypatch):
        stub = _fetch_stub(monkeypatch, [_msg(1)])
        stub._sync_run_id = 1

        def fetch(*args, **kwargs):
            stub._sync_run_id = 2
            stub.chats = {}
            return Response({"response": [_msg(1)]})

        monkeypatch.setattr("main_window.history.api_get", fetch)
        assert stub.fetch_older_messages("chat@g.us", _msg(9), store_only=True) is None
        assert stub.batched == []

    def test_unchanged_older_page_still_reaches_disk(self, monkeypatch):
        stub = _fetch_stub(monkeypatch, [_msg(1)])
        assert stub.fetch_older_messages("chat@g.us", _msg(9), store_only=True)
        assert stub.batched == [("chat@g.us", 1)]

    @pytest.mark.parametrize("body", [{}, None, "bad", {"response": None}, {"response": {}}])
    def test_missing_envelope_is_not_end_of_history(self, monkeypatch, body):
        stub = _fetch_stub(monkeypatch, [])
        monkeypatch.setattr("main_window.history.api_get", lambda *a, **k: Response(body))
        assert stub.fetch_older_messages("chat@g.us", _msg(9), store_only=True) is None
        assert stub._exhausted_chats == set()

    @pytest.mark.parametrize("change", ["run", "token", None])
    def test_delta_response_does_not_commit_after_supersession(self, monkeypatch, change):
        stub = _DeltaStub()
        _seed_warm_chat(stub)
        jid = next(iter(stub.chats))
        stub._sync_run_id = 1
        def fetch(*a, **k):
            if change == "run":
                stub._sync_run_id += 1
            elif change == "token":
                stub.token = "new"
            return Response({"response": []})
        monkeypatch.setattr(conversation_sync, "api_get", fetch)
        result = conversation_sync.ConversationSyncMixin.sync_chat_messages(
            stub, stub.chats[jid], sync_mode="incremental")
        assert result is (change is None)
        if change is not None:
            assert stub.db.calls == []


class TestPhoneBudget:
    @pytest.mark.parametrize("body,status,expected,ambiguous", [
        ({"response": {"error": "recent history sync incomplete"}}, 500, None, False),
        ({"response": {"error": "module lookup failed"}}, 500, None, False),
        ({"response": {"error": "oldest message key unavailable"}}, 500, None, False),
        ({"response": {"error": "send failed"}}, 500, None, True),
        ({"response": {"phoneOnly": True}}, 500, False, False),
        ({"response": {"primaryHasMore": False}}, 500, False, False),
        ({"response": {"requested": True}}, 200, True, False),
        ({"response": {"requested": False}}, 200, None, False),
    ])
    def test_classification_and_notification_budget(self, monkeypatch, body, status, expected, ambiguous):
        stub = _RequestStub()
        stub._older_request_attempts = {}
        stub._older_requested_chats = {}
        stub._persist_older_requested = lambda: None
        monkeypatch.setattr(backfill, "api_post", lambda *a, **k: Response(body, status))
        outcome = {}
        result = stub.request_older_messages("120363000000000000@g.us", outcome_out=outcome)
        assert result is expected
        assert outcome["ambiguous"] is ambiguous
        for _ in range(2):
            lifecycle.record_phone_request_attempt(stub, "chat", result, outcome, 123)
        assert stub._older_request_attempts.get("chat", 0) == (2 if expected is True or ambiguous else 0)
        assert ("chat" in stub._older_requested_chats) is ambiguous


class TestClearCallbacks:
    @pytest.mark.parametrize("change", ["message", "run", "token", "shutdown", None])
    def test_queued_clear_belongs_to_observed_content_and_session(self, monkeypatch, change):
        queued = []
        monkeypatch.setattr(conversation_sync.wx, "CallAfter", lambda fn, *a, **k: queued.append(lambda: fn(*a, **k)))
        jid = "j@s.whatsapp.net"
        stub = _ReconcileStub(chats={jid: _chat_with_records("A", "B", "C")},
                              conversations_panel=_FakeConversationsPanel(jid), remote_ids=set())
        stub._sync_run_id, stub.token = 1, "old"
        for _ in range(3):
            stub._reconcile_active_conversation_with_remote()
        assert len(queued) == 1
        if change == "message":
            stub.chats[jid]["messages"]["messages"]["records"].append(
                {"key": {"id": "new"}, "messageTimestamp": 999})
        elif change == "run":
            stub._sync_run_id += 1
        elif change == "token":
            stub.token = "new"
        elif change == "shutdown":
            stub._shutting_down = True
        queued[0]()
        assert bool(stub.clear_calls) is (change is None)

    @pytest.mark.parametrize("body", [{}, None, "bad", {"response": None}, {"response": []}])
    def test_remote_fetch_only_explicit_empty_list_proves_empty(self, monkeypatch, body):
        stub = window(ws=SimpleNamespace(_normalize_wpp_message=lambda m: m),
                      _phone_to_lid={}, settings={})
        monkeypatch.setattr(conversation_sync, "api_get", lambda *a, **k: Response(body))
        answer = conversation_sync.ConversationSyncMixin._get_remote_messages(stub, "12345@g.us")
        assert (answer == ([], 0, None)) is (body == {"response": []})
        if body != {"response": []}:
            assert answer is None


class StopPoll(BaseException):
    pass


class TestPeriodicPoll:
    def test_stale_list_response_is_rejected_inside_store_before_mutation(self, monkeypatch):
        stub = window(chats={})
        def fetch(*a, **k):
            stub._sync_run_id += 1
            return Response([{"id": "12345@g.us", "unreadCount": 9}])
        monkeypatch.setattr(chats_store, "api_post", fetch)
        assert chats_store.ChatsStoreMixin.get_remote_chats(stub, {}) is None
        assert stub.chats == {}
        # The fetch resets its diagnostic counters before issuing I/O; the
        # superseded response must not commit its nine-unread snapshot.
        assert stub._last_chat_fetch_count == 0

    @pytest.mark.parametrize("supersede", [True, False])
    def test_list_result_is_owned_before_assignment_and_delta(self, monkeypatch, supersede):
        targets, sync_calls, saves = [], [], []

        class Thread:
            def __init__(self, target, **kwargs):
                targets.append(target)

            def start(self):
                pass

        ticks = []
        def sleep(seconds):
            if ticks:
                raise StopPoll()
            ticks.append(seconds)

        monkeypatch.setattr(contacts.threading, "Thread", Thread)
        monkeypatch.setattr(contacts, "time", SimpleNamespace(sleep=sleep))
        monkeypatch.setattr(contacts.wx, "CallAfter", lambda fn, *a, **k: None)
        stub = window(_wa_connected=True, chats={}, settings={})
        stub._background_work_paused = lambda: False
        stub._capture_chat_sync_baseline = lambda: {}
        def fetch(old, **kwargs):
            assert kwargs["expected_context"].run == 1
            if supersede:
                stub._sync_run_id += 1
            return {"12345@g.us": {"remoteJid": "12345@g.us"}}
        stub.get_remote_chats = fetch
        stub._plan_message_sync = lambda *a, **k: (list(stub.chats.values()), [], 0, {})
        stub.sync_remote_chats = lambda *a, **k: sync_calls.append(k) or set()
        stub._normalize_jid = lambda jid: jid
        stub._schedule_save = lambda: saves.append(True)
        stub._schedule_set_chats = lambda: None
        stub.sync_media_for_all_chats = lambda *a, **k: None
        stub._reconcile_active_conversation_with_remote = lambda: None
        stub._maybe_refresh_profile_snapshot_live = lambda: None
        contacts.ContactsMixin.start_periodic_contacts_sync(stub)
        with pytest.raises(StopPoll):
            targets[0]()
        assert bool(stub.chats) is (not supersede)
        assert bool(sync_calls) is (not supersede)
        assert bool(saves) is (not supersede)
        if sync_calls:
            assert sync_calls[0]["expected_run_id"] == 1
            assert sync_calls[0]["expected_context"].token == "t"
