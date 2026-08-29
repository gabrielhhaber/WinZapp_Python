"""Tests for the bounded boot message sync.

Every chat gets the configured first page before lower-priority deep history.

MainWindow is a wx.Frame and cannot be instantiated without a running app, so
the methods under test are exercised against a stub carrying just the state
they touch.
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
    def upsert_chat(self, jid, data):
        pass

    def insert_messages_batch(self, jid, messages):
        pass


class _SyncStub:
    """Minimal MainWindow stand-in for sync_remote_chats()."""

    def __init__(self, chats):
        self.chats = chats
        self.calls = []
        self.settings = {"user_interface": {"messages_page_size": 200}}
        # sync_remote_chats() reports the chats whose fetch exhausted its
        # retries — a failure that returns normally instead of raising, and so
        # was invisible until it was counted. Both fields live on
        # MainWindow.__init__.
        self._sync_failures_lock = threading.Lock()
        self._sync_failed_chats = set()

    def history_page_target(self):
        return int(
            self.settings.get("user_interface", {}).get("messages_page_size", 200)
        )

    def sync_chat_messages(self, chat):
        self.calls.append((chat.get("remoteJid"), chat.get("_sync_limit")))


def _chats(n, start_t):
    return {
        f"jid{i:04d}@c.us": {"remoteJid": f"jid{i:04d}@c.us", "t": start_t - i}
        for i in range(n)
    }


class TestSyncRemoteChatsBoundedWindow:
    def test_all_chats_use_the_normal_page_limit(self):
        stub = _SyncStub(_chats(15, start_t=100))
        MainWindow.sync_remote_chats(stub)
        limits = dict(stub.calls)
        assert len(limits) == 15
        assert all(limit is None for limit in limits.values())

    def test_invalid_jids_are_filtered_before_ranking(self):
        stub = _SyncStub({
            "bad0": {"remoteJid": "0", "t": 999},
            "bad1": {"remoteJid": "", "t": 998},
            f"jid0000@c.us": {"remoteJid": "jid0000@c.us", "t": 1},
            f"jid0001@c.us": {"remoteJid": "jid0001@c.us", "t": 0},
        })
        MainWindow.sync_remote_chats(stub)
        limits = dict(stub.calls)
        assert set(limits) == {"jid0000@c.us", "jid0001@c.us"}
        assert all(limit is None for limit in limits.values())


class _MessagesStub:
    """Minimal MainWindow stand-in for sync_chat_messages()."""

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _needs_display_page_refill = staticmethod(MainWindow._needs_display_page_refill)
    _backfill_state_guard = MainWindow._backfill_state_guard
    _canonical_backfill_jid = MainWindow._canonical_backfill_jid
    _counts_as_last_message = MainWindow._counts_as_last_message
    _is_backfill_pending = MainWindow._is_backfill_pending
    _persist_history_gap_jids = lambda self: None
    _persist_backfill_pending_state = lambda self: None
    _is_conversation_open_jid = lambda self, jid: False
    _schedule_set_chats = lambda self: None

    def __init__(self, get_urls):
        self.get_urls = get_urls
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
        self._gap_chats = set()
        self._gap_candidate_chats = set()
        self._backfill_pending = set()
        self._backfill_retries = {}

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
        return []

    def _learn_sender_names_bulk(self, messages):
        return False

    def _jid_address_forms(self, jid):
        return MainWindow._jid_address_forms(self, jid)

    def _chat_jids_equivalent(self, left, right):
        return MainWindow._chat_jids_equivalent(self, left, right)


class TestSyncChatMessagesHonorsPageLimit:
    def test_legacy_deep_tag_cannot_override_configured_count(self, monkeypatch):
        urls = []
        monkeypatch.setattr(
            main_module.requests, "get",
            lambda url, **kwargs: urls.append(url) or _Resp(200, {"response": []}),
        )
        stub = _MessagesStub(urls)
        chat = {"remoteJid": "jid0000@c.us", "t": 100, "_sync_limit": 1000}
        MainWindow.sync_chat_messages(stub, chat)
        assert len(urls) == 1
        assert "count=200" in urls[0]

    def test_untagged_chat_falls_back_to_messages_page_size(self, monkeypatch):
        urls = []
        monkeypatch.setattr(
            main_module.requests, "get",
            lambda url, **kwargs: urls.append(url) or _Resp(200, {"response": []}),
        )
        stub = _MessagesStub(urls)
        chat = {"remoteJid": "jid0000@c.us", "t": 100}
        MainWindow.sync_chat_messages(stub, chat)
        assert len(urls) == 1
        assert "count=200" in urls[0]


class TestSyncChatMessagesLidIdentity:
    def test_lid_response_is_kept_under_its_canonical_phone_chat(self, monkeypatch):
        phone = "5511999999999@s.whatsapp.net"
        lid = "123456789@lid"
        message = {
            "key": {"remoteJid": lid, "fromMe": False, "id": "MSG-1"},
            "message": {"conversation": "hello"},
            "messageTimestamp": 100,
            "messageType": "conversation",
        }
        urls = []
        monkeypatch.setattr(
            main_module.requests,
            "get",
            lambda url, **kwargs: urls.append(url) or _Resp(
                200, {"response": [message]}
            ),
        )
        stub = _MessagesStub(urls)
        stub._phone_to_lid = {phone: lid}
        stub._lid_to_phone = {lid: phone}
        stub._normalize_fetched_messages = lambda raw, _jid: list(raw)
        stub._extract_lid_mapping = lambda _message: None
        stub.chats = {phone: {"remoteJid": phone, "t": 100}}

        MainWindow.sync_chat_messages(stub, stub.chats[phone].copy())

        assert f"get-messages/{lid}?count=200" in urls[0]
        records = stub.chats[phone]["messages"]["messages"]["records"]
        assert [record["key"]["id"] for record in records] == ["MSG-1"]
        assert records[0]["key"]["remoteJid"] == phone

    def test_group_message_from_lid_lookup_is_still_rejected(self):
        phone = "5511999999999@s.whatsapp.net"
        lid = "123456789@lid"
        group = "120363000000000000@g.us"
        stub = _MessagesStub([])
        stub._phone_to_lid = {phone: lid}
        stub._lid_to_phone = {lid: phone}

        assert stub._chat_jids_equivalent(lid, phone) is True
        assert stub._chat_jids_equivalent(group, phone) is False
