"""Editing a message must never leave a second, unquoted copy of it behind.

Reported: editing a message that replies to another updates the row
correctly, and a few seconds later a second row appears under it with the
edited text, not quoting anything, visible only to whoever edited.

Reproduced on a real install (2026-09-14). The edit itself came back through
the live socket under the reply's original id and was handled correctly. The
next periodic get-messages, 43 s later, brought in a record never seen on the
socket: ``conversation``, fromMe, 12 s after the reply, no quote, text
identical to the edited reply. WPP.chat.editMessage() also creates a separate
``type: "protocol"`` / ``subtype: "message_edit"`` message with a new id and the
new text as ``body``; WhatsApp Web keeps it in the chat and get-messages returns
it. _normalize_wpp_message() had no ``protocol`` branch, so its "unmapped type
with body → conversation" fallback made it a text row, and the id-based merge
appended it. See core/message_edit.py.

What is pinned here: the event never becomes a row on any path, it is dropped
rather than applied, a copy already stored under its id is removed, the sync
merge cannot keep that copy alive, and an own edit keeps a reply's quote.
"""

import inspect

import pytest

from core.message_edit import (
    MESSAGE_EDIT,
    clean_message_id,
    edited_text_message,
    is_edit_event,
)
from core.websocket_client import WebSocketClient
from main import MainWindow
from ui.conversations import ConversationsPanel
from tests.god_modules import patch_main_global

ORIGINAL = "3EB037EC32C70B38BF05EB"
EDIT_ID = "3EB0BC2F9AA9CFAFBB5098"
GROUP = "120363427511142886@g.us"


class _Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


def _raw_edit(**overrides):
    raw = {
        "id": f"true_{GROUP}_{EDIT_ID}",
        "from": "5511999999999@c.us",
        "to": GROUP,
        "fromMe": True,
        "timestamp": 1789360406,
        "type": "protocol",
        "subtype": "message_edit",
        "protocolMessageKey": f"true_{GROUP}_{ORIGINAL}",
        "body": "texto editado",
    }
    raw.update(overrides)
    return raw


def _raw_reply(text="texto editado"):
    return {
        "id": f"true_{GROUP}_{ORIGINAL}", "from": "5511999999999@c.us", "to": GROUP,
        "fromMe": True, "timestamp": 1789360394, "type": "chat", "body": text,
        "quotedStanzaID": "3EB0QUOTEDQUOTEDQUOTE1",
        "quotedParticipant": "5511888888888@c.us",
    }


# ── The normaliser ───────────────────────────────────────────────────────────

class TestNormalizer:
    def test_an_edit_is_a_hidden_protocol_message_not_text(self):
        result = _Normalizer()._normalize_wpp_message(_raw_edit())

        assert result["messageType"] == "protocolMessage"
        assert "conversation" not in result["message"]
        assert is_edit_event(result)
        protocol = result["message"]["protocolMessage"]
        assert protocol == {"type": MESSAGE_EDIT, "key": ORIGINAL}

    def test_a_msgkey_shaped_target_is_understood_too(self):
        raw = _raw_edit(protocolMessageKey={
            "fromMe": True, "remote": GROUP, "id": ORIGINAL,
            "_serialized": f"true_{GROUP}_{ORIGINAL}",
        })
        result = _Normalizer()._normalize_wpp_message(raw)
        assert result["message"]["protocolMessage"]["key"] == ORIGINAL

    def test_an_edit_carrying_quote_fields_still_does_not_become_text(self):
        raw = _raw_edit(quotedStanzaID="3EB0QUOTEDQUOTEDQUOTE1",
                        quotedParticipant="5511888888888@c.us")
        result = _Normalizer()._normalize_wpp_message(raw)
        assert result["messageType"] == "protocolMessage"

    def test_any_other_protocol_subtype_is_handled_exactly_as_before(self):
        """Only the observed subtype changed. Another `protocol` subtype still
        takes the generic path — text fallback when it has a body, an unmapped
        empty message when it does not — so nothing unobserved gets hidden."""
        with_body = _Normalizer()._normalize_wpp_message(
            _raw_edit(subtype="some_other_subtype", body="something"))
        assert with_body["messageType"] == "conversation"
        assert with_body["message"] == {"conversation": "something"}
        assert not is_edit_event(with_body)

        without_body = _Normalizer()._normalize_wpp_message(
            _raw_edit(subtype="some_other_subtype", body=""))
        assert without_body["messageType"] == "protocol"
        assert without_body["message"] == {}


# ── Pure helpers ─────────────────────────────────────────────────────────────

class TestHelpers:
    @pytest.mark.parametrize("raw, expected", [
        (f"true_{GROUP}_{ORIGINAL}", ORIGINAL),
        (f"true_{GROUP}_{ORIGINAL}_5511999999999@c.us", ORIGINAL),
        (ORIGINAL, ORIGINAL),
        ({"id": ORIGINAL}, ORIGINAL),
        (None, ""),
    ])
    def test_clean_message_id(self, raw, expected):
        assert clean_message_id(raw) == expected

    def test_a_normal_message_is_not_an_edit_event(self):
        assert not is_edit_event(
            {"messageType": "conversation", "message": {"conversation": "oi"}})
        assert not is_edit_event(
            {"messageType": "protocolMessage", "message": {"protocolMessage": {"type": 3}}})


class TestEditedTextMessage:
    def _synced_reply(self, **ctx_extra):
        ctx = {"stanzaId": "Q1", "participant": "5511888888888@s.whatsapp.net",
               "quotedMessage": {"conversation": "pergunta"}}
        ctx.update(ctx_extra)
        return {"messageType": "extendedTextMessage",
                "message": {"extendedTextMessage": {"text": "antes", "contextInfo": ctx}}}

    def test_a_synced_reply_keeps_its_quote(self):
        existing = self._synced_reply()
        message, mtype = edited_text_message(existing, "depois")
        assert mtype == "extendedTextMessage"
        assert message["extendedTextMessage"]["text"] == "depois"
        assert message["extendedTextMessage"]["contextInfo"]["stanzaId"] == "Q1"
        # A copy — the stored record is not touched by building it.
        assert existing["message"]["extendedTextMessage"]["text"] == "antes"

    def test_old_mentions_are_not_carried_into_the_new_text(self):
        existing = self._synced_reply(mentionedJid=["5511777777777@s.whatsapp.net"])
        message, _ = edited_text_message(existing, "depois")
        ctx = message["extendedTextMessage"]["contextInfo"]
        assert "mentionedJid" not in ctx
        assert ctx["stanzaId"] == "Q1"

    def test_plain_text_stays_plain(self):
        assert edited_text_message({"message": {"conversation": "antes"}}, "depois") == (
            {"conversation": "depois"}, "conversation")

    def test_extended_is_forced_when_asked(self):
        message, mtype = edited_text_message({"message": {"conversation": "a"}}, "b",
                                              extended=True)
        assert (message, mtype) == ({"extendedTextMessage": {"text": "b"}},
                                    "extendedTextMessage")

    def test_the_optimistic_edit_goes_through_it(self):
        src = inspect.getsource(ConversationsPanel._apply_message_edit)
        assert "edited_text_message(" in src
        assert 'edited["message"] = {"conversation": text}' not in src


# ── MainWindow: drop, purge, filters ─────────────────────────────────────────

class _Db:
    def __init__(self):
        self.deleted = []

    def delete_message(self, jid, mid):
        self.deleted.append((jid, mid))


class _Panel:
    def __init__(self, conversation=None):
        self.conversation = conversation
        self.removed = []

    def remove_messages_by_id(self, ids, focus_previous=False):
        self.removed.append(set(ids))


class _Stub:
    _drop_protocol_edit = MainWindow._drop_protocol_edit
    _remember_dropped_edit_events = MainWindow._remember_dropped_edit_events
    _purge_materialized_edit_rows = MainWindow._purge_materialized_edit_rows
    _normalize_fetched_messages = MainWindow._normalize_fetched_messages
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self, records=None, open_conversation=False):
        self.chats = {GROUP: {"remoteJid": GROUP,
                              "messages": {"messages": {"records": records or []}}}}
        self.db = _Db()
        self.ws = _Normalizer()
        self.conversations_panel = _Panel(
            {"remoteJid": GROUP} if open_conversation else None)
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self.recomputed = []
        self.set_chats_calls = 0

    def _recompute_chat_last_message(self, jid):
        self.recomputed.append(jid)

    def _schedule_set_chats(self):
        self.set_chats_calls += 1

    @property
    def records(self):
        return self.chats[GROUP]["messages"]["messages"]["records"]


def _row(mid, text="texto editado"):
    return {"key": {"id": mid, "fromMe": True, "remoteJid": GROUP},
            "messageType": "conversation", "message": {"conversation": text},
            "messageTimestamp": 1789360406}


@pytest.fixture
def call_after(monkeypatch):
    calls = []
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: calls.append((fn, a)))
    return calls


class TestDropProtocolEdit:
    def test_an_edit_event_is_dropped_and_remembered(self):
        stub = _Stub()
        msg = _Normalizer()._normalize_wpp_message(_raw_edit())

        assert stub._drop_protocol_edit(GROUP, msg) is True
        assert stub.records == []
        assert stub._dropped_edit_event_ids == {EDIT_ID}

    def test_a_copy_stored_before_the_fix_is_removed(self):
        stub = _Stub(records=[_row(ORIGINAL), _row(EDIT_ID)])
        msg = _Normalizer()._normalize_wpp_message(_raw_edit())

        stub._drop_protocol_edit(GROUP, msg)

        assert [r["key"]["id"] for r in stub.records] == [ORIGINAL]
        assert stub.db.deleted == [(GROUP, EDIT_ID)]
        assert stub.recomputed == [GROUP]
        assert stub.set_chats_calls == 1

    def test_an_ordinary_message_is_left_alone(self):
        stub = _Stub(records=[_row(ORIGINAL)])
        msg = _Normalizer()._normalize_wpp_message(_raw_reply())
        assert stub._drop_protocol_edit(GROUP, msg) is False
        assert stub.db.deleted == []


class TestPurge:
    def test_the_open_conversation_removes_the_row_through_the_panel(self):
        stub = _Stub(records=[_row(ORIGINAL), _row(EDIT_ID)], open_conversation=True)
        assert stub._purge_materialized_edit_rows(GROUP, {EDIT_ID}) == 1
        assert stub.conversations_panel.removed == [{EDIT_ID}]

    def test_a_copy_only_on_disk_is_still_deleted(self):
        """Resident records are capped and store-only history fetches never
        load what they write, so a stored copy can exist in the database
        without being in memory. The DELETE must not depend on finding it."""
        stub = _Stub(records=[_row(ORIGINAL)])
        assert stub._purge_materialized_edit_rows(GROUP, {EDIT_ID}) == 0
        assert (GROUP, EDIT_ID) in stub.db.deleted
        assert stub.set_chats_calls == 0

    def test_a_copy_stored_under_the_other_jid_form_is_found(self):
        lid = "150418378248211@lid"
        phone = "5511999999999@s.whatsapp.net"
        stub = _Stub()
        stub.chats = {phone: {"remoteJid": phone,
                              "messages": {"messages": {"records": [_row(EDIT_ID)]}}}}
        stub._lid_to_phone = {lid: phone}

        assert stub._purge_materialized_edit_rows(lid, {EDIT_ID}) == 1

        assert stub.chats[phone]["messages"]["messages"]["records"] == []
        assert (phone, EDIT_ID) in stub.db.deleted
        assert stub.recomputed == [phone]

    def test_the_real_message_is_never_touched(self):
        """Only the event's own id is removed — never the message it edits."""
        stub = _Stub(records=[_row(ORIGINAL)])
        stub._purge_materialized_edit_rows(GROUP, {EDIT_ID})
        assert [r["key"]["id"] for r in stub.records] == [ORIGINAL]


class TestFetchedPages:
    def test_get_messages_never_returns_the_edit_as_a_row(self, monkeypatch, call_after):
        patch_main_global(monkeypatch, "prune_message_record", lambda m: m)
        stub = _Stub(records=[_row(ORIGINAL), _row(EDIT_ID)])

        out = stub._normalize_fetched_messages([_raw_reply(), _raw_edit()], GROUP)

        assert [m["key"]["id"] for m in out] == [ORIGINAL]
        assert stub._dropped_edit_event_ids == {EDIT_ID}
        # Purge of the stored copy is handed to the main thread.
        assert [(fn.__name__, a) for fn, a in call_after] == [
            ("_purge_materialized_edit_rows", (GROUP, {EDIT_ID}))]

    def test_scrolling_up_filters_the_edit_too(self):
        src = inspect.getsource(MainWindow.fetch_older_messages)
        assert "is_edit_event(normalized)" in src
        assert "_remember_dropped_edit_events" in src

    def test_the_sync_merge_cannot_keep_a_stored_copy_alive(self):
        """sync_chat_messages keeps every local record the page lacks; both of
        its merges must exclude remembered edit-event ids, or the copy removed
        by the purge is merged straight back."""
        src = inspect.getsource(MainWindow.sync_chat_messages)
        assert src.count("not in dropped_edit_ids") == 2


class TestFunnelOrder:
    @pytest.mark.parametrize("method, later_marker", [
        ("on_new_message", "# ── Ensure the chat record exists"),
        ("on_historical_message", "records_wrapper = chat.setdefault"),
    ])
    def test_the_event_is_dropped_before_a_chat_can_be_created(self, method, later_marker):
        src = inspect.getsource(getattr(MainWindow, method))
        assert src.index("self._drop_protocol_edit(") < src.index(later_marker)
