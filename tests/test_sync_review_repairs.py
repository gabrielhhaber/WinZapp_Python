"""Real sync consumers with fake I/O, clock, executor and callback queue."""

from types import MethodType, SimpleNamespace

import pytest

from core import sync_lifecycle as lifecycle
from core.message_edit import MESSAGE_EDIT
from main_window import backfill, chat_actions, conversation_sync, history, message_pins
from tests.test_deep_history_backfill import _fetch_stub, _make, _msg
from tests.test_history_sync_on_demand import _InteractiveWaitStub, _Stub as RequestStub
from tests.test_incremental_delta_outcomes import _DeltaStub, _seed_warm_chat
from tests.test_phone_side_sync import _ReconcileStub, _FakeConversationsPanel, _chat_with_records
from tests.test_sync_lifecycle_guards import Response, window


class TestBlockListOwnership:
    @pytest.mark.parametrize("change", ["http", "json", "queued", None])
    def test_stale_block_list_never_writes_or_refreshes(self, monkeypatch, change):
        writes, refreshed, queued = [], [], []
        stub = window(_blocked_contacts={"old"},
                      db=SimpleNamespace(set_metadata_json=lambda *a: writes.append(a)),
                      _schedule_set_chats=lambda: refreshed.append(True))
        class Reply(Response):
            def json(self):
                if change == "json":
                    stub.token = "new"
                return super().json()
        def get(*a, **k):
            if change == "http":
                stub._sync_run_id += 1
            return Reply({"response": []})
        monkeypatch.setattr(chat_actions, "api_get", get)
        monkeypatch.setattr(chat_actions.wx, "CallAfter", lambda fn: queued.append(fn))
        chat_actions.ChatActionsMixin.get_block_list(stub)
        if change in ("http", "json"):
            assert stub._blocked_contacts == {"old"}
            assert writes == queued == []
            return
        assert stub._blocked_contacts == set()
        assert writes == [("blocked_contacts", [])]
        if change == "queued":
            stub._sync_run_id += 1
        queued[0]()
        assert bool(refreshed) is (change is None)


class TestHistoryContextChain:
    @pytest.mark.parametrize("change", ["run", "token", None])
    def test_sleep_cannot_rebind_old_waiter_to_new_session(self, monkeypatch, change):
        clock, contexts = [0.0], []
        stub = _InteractiveWaitStub(lambda *a: None)
        stub._sync_run_id, stub.token = 1, "old"
        def sleep(seconds):
            clock[0] += seconds
            if change == "run":
                stub._sync_run_id += 1
            elif change == "token":
                stub.token = "new"
        def fetch(*a, **k):
            contexts.append(k)
            return [] if k["allow_phone_request"] else None
        stub.fetch_older_messages = fetch
        monkeypatch.setattr(history, "time", SimpleNamespace(
            monotonic=lambda: clock[0], sleep=sleep))
        result = stub.wait_for_older_messages(
            "chat@g.us", _msg(9), timeout=3, poll_interval=1, retry_request_every=1)
        if change:
            assert result is None
            assert contexts == []
        else:
            assert result == []
            assert [c["allow_phone_request"] for c in contexts] == [False, True]
            assert all(c["expected_context"].run == 1 and
                       c["expected_context"].token == "old" for c in contexts)

    def test_old_deep_visit_is_rejected_at_entry(self):
        stub = _make([[_msg(1)]], oldest=_msg(9))
        old = lifecycle.capture_sync_context(stub)
        stub.token = "replacement"
        assert stub.deep_backfill_chat("chat@g.us", expected_context=old) == 0
        assert stub.calls == stub.db.batches == []

    def test_old_phone_request_is_rejected_before_post(self, monkeypatch):
        stub = RequestStub()
        old = lifecycle.capture_sync_context(stub)
        stub.token = "replacement"
        posts = []
        monkeypatch.setattr(backfill, "api_post", lambda *a, **k: posts.append(a))
        assert stub.request_older_messages("chat@g.us", expected_context=old) is None
        assert posts == []


def edit_event():
    return {"key": {"id": "edit"}, "messageType": "protocolMessage",
            "message": {"protocolMessage": {"type": MESSAGE_EDIT}}}


def unreadable(_message):
    raise ValueError("unreadable synthetic record")


class TestUnreadablePages:
    @pytest.mark.parametrize("bad", [True, False])
    def test_pin_consumer_keeps_unknown_page_as_value_error(self, monkeypatch, bad):
        jid = "12345@g.us"
        raw = ["bad"] if bad else [{"key": {"id": "pin", "remoteJid": jid}}]
        stub = window(ws=SimpleNamespace(_normalize_wpp_message=lambda m: m),
                      _normalize_jid=lambda j: j, _chat_jids_equivalent=lambda a, b: a == b)
        stub._normalize_fetched_messages = MethodType(
            conversation_sync.ConversationSyncMixin._normalize_fetched_messages, stub)
        reply = Response({"status": "success", "response": raw})
        reply.raise_for_status = lambda: None
        monkeypatch.setattr(message_pins, "api_post", lambda *a, **k: reply)
        if bad:
            with pytest.raises(ValueError, match="Incomplete pinned messages response"):
                message_pins.MessagePinsMixin.get_pinned_messages(stub, jid)
        else:
            assert message_pins.MessagePinsMixin.get_pinned_messages(stub, jid) == [
                {"key": {"id": "pin", "remoteJid": jid}, "pinInChat": True}]

    @pytest.mark.parametrize("kind", ["raw", "exception", "empty", "edit", "normal"])
    def test_anchored_page_distinguishes_unknown_from_filtered_empty(self, monkeypatch, kind):
        raw = {"raw": ["bad"], "exception": [{}], "empty": [],
               "edit": [edit_event()], "normal": [_msg(1)]}[kind]
        stub = _fetch_stub(monkeypatch, raw)
        stub.ws = SimpleNamespace(_normalize_wpp_message=unreadable if kind == "exception" else lambda m: m)
        monkeypatch.setattr(history.wx, "CallAfter", lambda *a, **k: None)
        result = stub.fetch_older_messages("chat@g.us", _msg(9), store_only=True,
                                           allow_phone_request=False)
        if kind in ("raw", "exception"):
            assert result is None
            assert stub._exhausted_chats == set()
            assert stub.batched == []
        elif kind == "normal":
            assert result == [_msg(1)]
            assert stub.batched == [("chat@g.us", 1)]
        elif kind == "edit":
            assert result == []
            assert stub._exhausted_chats == set()
            assert stub.batched == []
        else:
            # Explicit empty pages still follow the existing phone-grace policy.
            assert result is None
            assert stub.batched == []

    @pytest.mark.parametrize("kind", ["raw", "exception", "empty", "edit", "normal"])
    def test_delta_does_not_accept_an_unreadable_activity_marker(self, monkeypatch, kind):
        stub = _DeltaStub()
        _seed_warm_chat(stub)
        jid = next(iter(stub.chats))
        record = stub.chats[jid]["messages"]["messages"]["records"][0]
        raw = {"raw": ["bad"], "exception": [{}], "empty": [],
               "edit": [edit_event()], "normal": [record]}[kind]
        stub.ws = SimpleNamespace(_normalize_wpp_message=unreadable if kind == "exception" else lambda m: m)
        stub._normalize_fetched_messages = MethodType(
            conversation_sync.ConversationSyncMixin._normalize_fetched_messages, stub)
        stub._remember_dropped_edit_events = lambda ids: None
        stub._purge_materialized_edit_rows = lambda *a: None
        stub._note_verified_activity = lambda *a: None
        stub._note_chat_verified_now = lambda *a: None
        monkeypatch.setattr(conversation_sync.wx, "CallAfter", lambda *a, **k: None)
        monkeypatch.setattr(conversation_sync, "api_get", lambda *a, **k: Response({"response": raw}))
        for _ in range(3):
            result = conversation_sync.ConversationSyncMixin.sync_chat_messages(
                stub, {"remoteJid": jid, "t": 101}, sync_mode="incremental")
            assert result is (kind not in ("raw", "exception"))
        if kind in ("raw", "exception"):
            assert stub.db.upserted == []
            assert jid in stub._sync_failed_chats
            assert jid not in stub._delta_unsatisfied_attempts
        elif kind == "normal":
            assert stub.db.upserted


class TestDeletionContentFreshness:
    @pytest.mark.parametrize("operation", ["clear", "delete", "anchor"])
    @pytest.mark.parametrize("change", ["edit", "star", None])
    def test_edits_and_stars_invalidate_observed_deletion(self, monkeypatch, operation, change):
        queued, mirrors = [], []
        jid = "j@s.whatsapp.net"
        stub = _ReconcileStub(chats={jid: _chat_with_records("A", "B", "C")},
                              conversations_panel=_FakeConversationsPanel(jid),
                              remote_ids=set() if operation == "clear" else {"A", "C"})
        records = stub.chats[jid]["messages"]["messages"]["records"]
        def mutate():
            if change == "edit":
                records[1]["message"]["conversation"] = "edited"
            elif change == "star":
                records[1]["isStarred"] = True
        stub._mirror_remote_clear = lambda j: mirrors.append((j, "clear"))
        stub._mirror_remote_deletions = lambda j, ids: mirrors.append((j, set(ids)))
        monkeypatch.setattr(conversation_sync.wx, "CallAfter", lambda fn: queued.append(fn))
        if operation == "anchor":
            records[0]["messageTimestamp"] = 800
            records[1]["messageTimestamp"] = 900
            records[2]["messageTimestamp"] = 1200
            stub._remote_ids, stub._remote_oldest_ts = {"C"}, 1100
            def page(*a):
                mutate()
                return {"A"}, 700, "older-anchor"
            stub._fetch_remote_messages_before = page
            for _ in range(3):
                # Each poll observes the original content before the fake I/O.
                records[1]["message"]["conversation"] = "x"
                records[1].pop("isStarred", None)
                stub._reconcile_active_conversation_with_remote()
        else:
            for _ in range(3 if operation == "clear" else 1):
                stub._reconcile_active_conversation_with_remote()
            assert len(queued) == 1
            mutate()
        for callback in queued:
            callback()
        assert bool(mirrors) is (change is None)
        if mirrors and operation != "clear":
            assert mirrors == [(jid, {"B"})]


class StopLoop(BaseException):
    pass


class TestOuterPhoneBudget:
    @pytest.mark.parametrize("ambiguous", [False, True])
    def test_short_backfill_retries_no_send_but_waits_after_ambiguous_send(self, monkeypatch, ambiguous):
        clock, posts, passes, persisted, retired = [1.0], [], [], [], []
        jid = "chat@g.us"
        stub = window(_wa_connected=True, _older_requested_chats={}, _older_request_attempts={},
                      _ui_ready_event=SimpleNamespace(is_set=lambda: True),
                      _exhausted_chats=set(), _history_still_landing=False,
                      _BACKFILL_BUDGET=10000, _BACKFILL_LANDING_BUDGET=10000,
                      _BACKFILL_FIRST_DELAY=1, _BACKFILL_MAX_DELAY=2,
                      _BACKFILL_CHUNK=1, _BACKFILL_WORKERS=1,
                      _OLDER_REQUESTS_PER_PASS=1, _PHONE_REQUEST_MIN_GAP=0,
                      _OLDER_REQUEST_GRACE=10000, _MAX_PHONE_HISTORY_REQUESTS=2)
        stub._initial_backfill_delay = lambda *a: 1
        stub._collapse_and_list_backfill_pending = lambda: [jid]
        stub._canonical_backfill_jid = lambda j: j
        stub._pending_name_resolution = stub._chats_needing_deep_history = lambda: []
        stub._voice_call_in_progress = lambda: False
        stub.refresh_history_still_landing = lambda **k: False
        stub._start_deferred_media_sync = lambda: None
        stub._background_backfill_work_allowed = lambda *a: False
        stub._chats_blocking_background_work = backfill.BackfillMixin._chats_blocking_background_work
        stub._resolve_backfill_target = lambda j: (j, {"remoteJid": j})
        stub._local_record_count = lambda j: 1
        stub.history_page_target = lambda: 200
        stub._oldest_stored_message = lambda j: _msg(9)
        stub._anchor_identity = history.HistoryMixin._anchor_identity
        stub._user_has_opened = lambda j: True
        stub._keep_backfill_pending = lambda *a: None
        stub._completed_backfill_targets = lambda *a: 0
        stub._retire_chat_without_older_history = lambda j: retired.append(j)
        stub._persist_older_requested = lambda: persisted.append(dict(stub._older_requested_chats))
        stub._persist_backfill_pending_state = lambda: None
        stub._persist_history_gap_jids = lambda: None
        stub._normalize_jid = lambda j: j
        stub.request_older_messages = MethodType(backfill.BackfillMixin.request_older_messages, stub)
        old = lifecycle.capture_sync_context(stub)
        def sync(chat, run, expected_context=None):
            assert run == 1 and expected_context == old
            passes.append(True)
            if len(passes) == 3:
                raise StopLoop()
        stub.sync_chat_messages = sync
        class Pool:
            def __init__(self, **k):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def submit(self, fn, *a, **k):
                fn(*a, **k)
                return SimpleNamespace(result=lambda: True)
        def post(*a, **k):
            posts.append(clock[0])
            if len(posts) == 1:
                if ambiguous:
                    raise TimeoutError("ambiguous synthetic send")
                return Response({"response": {"error": "module lookup failed"}}, 500)
            return Response({"response": {"requested": True}})
        monkeypatch.setattr(backfill, "ThreadPoolExecutor", Pool)
        monkeypatch.setattr(backfill, "as_completed", lambda futures: futures)
        monkeypatch.setattr(backfill, "api_post", post)
        monkeypatch.setattr(backfill, "time", SimpleNamespace(
            time=lambda: clock[0], monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
        with pytest.raises(StopLoop):
            backfill.BackfillMixin._backfill_empty_chats(stub, expected_context=old)
        assert len(posts) == (1 if ambiguous else 2)
        assert stub._older_request_attempts == {jid: 1}
        assert stub._older_requested_chats[jid] == posts[-1]
        assert persisted == [{jid: posts[-1]}]
        assert retired == []
