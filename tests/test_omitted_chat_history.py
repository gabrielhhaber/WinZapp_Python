"""Tests for loading older history in omitted conversations holding 0 messages."""

import threading
import types

from core import sync_lifecycle
from main import ConversationsPanel, MainWindow
from main_window import backfill as mw_backfill
from main_window import history as mw_history
from tests.god_modules import patch_main_global


PHONE = "5511999990001@s.whatsapp.net"
LID = "111222333@lid"


def _opened_stub(*, records=0, page_size=200, exhausted=False, connected=True):
    stub = types.SimpleNamespace(
        _sync_run_id=1,
        token="synthetic:key",
        wpp_server="http://synthetic.invalid",
        wpp_port=6300,
        _wa_connected=connected,
        offline_mode=False,
        _lid_to_phone={LID: PHONE},
        _phone_to_lid={PHONE: LID},
        _opened_conversations=set(),
        _exhausted_chats={PHONE} if exhausted else set(),
        _chats_awaiting_messages=set(),
        _partial_history_counts={},
        _backfill_state_lock=threading.RLock(),
        settings={"user_interface": {"messages_page_size": page_size}},
        chats={PHONE: {"remoteJid": PHONE, "messages": {"messages": {"records": [{"key": {"id": f"M{i}"}} for i in range(records)]}}}},
        persisted={},
        db=types.SimpleNamespace(
            set_metadata_json=lambda k, v: stub.persisted.update({k: v}),
        ),
    )
    stub._normalize_jid = MainWindow._normalize_jid
    stub._jid_address_forms = lambda jid: MainWindow._jid_address_forms(stub, jid)
    stub._canonical_backfill_jid = lambda jid: MainWindow._canonical_backfill_jid(stub, jid)
    stub._backfill_state_guard = lambda: MainWindow._backfill_state_guard(stub)
    stub._resolve_backfill_target = lambda jid: MainWindow._resolve_backfill_target(stub, jid)
    stub._local_record_count = lambda jid: MainWindow._local_record_count(stub, jid)
    stub.history_page_target = lambda: MainWindow.history_page_target(stub)
    stub._is_backfill_pending = lambda jid: MainWindow._is_backfill_pending(stub, jid)
    stub._keep_backfill_pending = lambda jid, count: MainWindow._keep_backfill_pending(stub, jid, count)
    stub._persist_backfill_pending_state = lambda: MainWindow._persist_backfill_pending_state(stub)
    stub._note_conversation_opened = lambda jid: MainWindow._note_conversation_opened(stub, jid)
    return stub


class TestOpeningOmittedChatWakesBackfill:
    def test_opening_an_empty_non_exhausted_chat_queues_it_and_schedules_backfill(self, monkeypatch):
        scheduled = []
        monkeypatch.setattr(mw_backfill, "schedule_backfill", scheduled.append)
        stub = _opened_stub(records=0, exhausted=False, connected=True)

        stub._note_conversation_opened(LID)

        assert PHONE in stub._chats_awaiting_messages
        assert stub.persisted["backfill_pending_v1"] == {PHONE: 0}
        assert scheduled == [stub]

    def test_opening_an_exhausted_or_full_chat_does_not_wake_backfill(self, monkeypatch):
        scheduled = []
        monkeypatch.setattr(mw_backfill, "schedule_backfill", scheduled.append)

        exhausted = _opened_stub(records=0, exhausted=True, connected=True)
        exhausted._note_conversation_opened(PHONE)
        assert exhausted._chats_awaiting_messages == set()
        assert scheduled == []

        full = _opened_stub(records=5, page_size=5, exhausted=False, connected=True)
        full._note_conversation_opened(PHONE)
        assert full._chats_awaiting_messages == set()
        assert scheduled == []


class TestAnchorlessFetchOlderMessages:
    def test_fetch_older_messages_without_anchor_queries_latest_and_requests_phone(self, monkeypatch):
        urls = []
        asked = []

        stub = types.SimpleNamespace(
            _sync_run_id=1,
            token="synthetic:key",
            wpp_server="http://synthetic.invalid",
            wpp_port=6300,
            settings={"user_interface": {"messages_page_size": 50}},
            _exhausted_chats=set(),
            _older_requested_chats={},
            _phone_to_lid={PHONE: LID},
            chats={PHONE: {"remoteJid": PHONE, "messages": {"messages": {"records": []}}}},
            db=types.SimpleNamespace(
                set_metadata_json=lambda k, v: None,
            ),
            ws=types.SimpleNamespace(_normalize_wpp_message=lambda m: m),
        )
        stub._normalize_jid = MainWindow._normalize_jid
        stub._resolve_jid_for_msg_key = lambda jid: jid
        stub._persist_older_requested = lambda: None
        stub.request_older_messages = lambda jid, **kw: (asked.append((jid, kw.get("user_asked"))), True)[1]

        def fake_get(url, headers=None, timeout=30):
            urls.append(url)
            return types.SimpleNamespace(status_code=200, json=lambda: {"response": []})

        monkeypatch.setattr(mw_history, "api_get", fake_get)
        monkeypatch.setattr(mw_history.history_boundary, "is_only_on_phone", lambda s, jid: False)
        patch_main_global(monkeypatch, "time", types.SimpleNamespace(
            time=lambda: 100.0, time_ns=lambda: 100, monotonic=lambda: 100.0))

        ctx = sync_lifecycle.capture_sync_context(stub)
        result = MainWindow.fetch_older_messages(
            stub, PHONE, None, allow_phone_request=True, expected_context=ctx)

        assert urls == ["http://synthetic.invalid:6300/api/synthetic:key/get-messages/5511999990001@c.us?count=50"]
        assert asked == [(PHONE, True)]
        assert result is None


class TestEmptyConversationHomeAction:
    def test_load_older_messages_from_empty_chat_queries_db_then_server_with_none_anchor(self, monkeypatch):
        fetched_args = []
        done = threading.Event()

        class FakeList:
            def __init__(self):
                self.focused = None
                self.selected = None
                self.visible = None

            def Freeze(self):
                pass

            def Thaw(self):
                pass

            def Focus(self, idx):
                self.focused = idx

            def Select(self, idx, state):
                self.selected = (idx, state)

            def EnsureVisible(self, idx):
                self.visible = idx

        panel = types.SimpleNamespace(
            conversation={"remoteJid": PHONE, "messages": {"messages": {"records": []}}},
            _all_sorted_messages=[],
            _sorted_messages=[],
            _messages_offset=0,
            _is_loading_more=False,
            _reached_server_start={},
            _server_history_anchor={},
            _pinned_extra_history_ids=set(),
            _unread_sep_idx=-1,
            messages_list=FakeList(),
            main_window=types.SimpleNamespace(
                settings={"user_interface": {"messages_page_size": 50}},
                db=types.SimpleNamespace(get_messages=lambda jid, limit=50, offset=0: []),
                fetch_older_messages=lambda jid, anchor: (fetched_args.append((jid, anchor)), [])[1],
            ),
        )
        panel._history_storage_jid = lambda jid: jid
        panel._is_separator = lambda m: False
        panel._load_older_messages_from_server = lambda: ConversationsPanel._load_older_messages_from_server(panel)
        panel._set_reached_start = lambda jid: done.set()

        monkeypatch.setattr("ui.conversation_panel.history_loading.wx.CallAfter",
                            lambda fn, *a: fn(*a))

        ConversationsPanel._load_older_messages(panel)
        assert done.wait(timeout=3.0)
        assert fetched_args == [(PHONE, None)]

        # And when older messages arrive into an initially empty list, focus stays in bounds.
        panel._is_loading_more = False
        panel._merge_history_into_records = lambda msgs: None
        panel._remember_expanded_window = lambda: None
        panel._recompute_unread_sep_idx = lambda: None
        panel._sync_message_rows = lambda old, new: None
        panel._is_displayable_message = lambda m: True
        panel._extract_timestamp = lambda m: m.get("timestamp", 0)
        panel._deduplicate_messages = lambda msgs: list(msgs)

        msgs = [{"key": {"id": "M2"}, "timestamp": 200}, {"key": {"id": "M1"}, "timestamp": 100}]
        ConversationsPanel._on_older_messages_loaded(panel, msgs, PHONE, None)
        assert panel.messages_list.focused == 1
        assert panel.messages_list.selected == (1, True)
        assert panel.messages_list.visible == 1
