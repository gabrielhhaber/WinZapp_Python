"""Tests for on_incoming_message()/_persist_reaction_record() persisting a
reaction from someone else into the chat's own records — not just the
in-memory _reaction_map.

Reported live: a reaction someone else left on a message in an open
conversation would disappear — both the inline marker on the message row
and the Reactions button — the next time anything repopulated the message
list (a history backfill, a background refresh, closing/reopening the
conversation, or an app restart), even though nothing about simply moving
focus between messages should touch reaction state at all.

Root cause: main.py's on_new_message() deliberately routes every
reactionMessage straight to ConversationsPanel.on_incoming_message() and
returns *without* ever appending it to the chat's own `records` — a
reactionMessage explicitly "must not be added to records" per that
handler's own comment, to avoid it being picked up as a chat's last-message
preview. on_incoming_message() then only ever updated the in-memory
_reaction_map, correct for the live view but with nothing left once
populate_messages()/refresh_active_conversation_messages() rebuilds
_reaction_map from scratch by scanning `records` — which never had the
reaction to find. Our OWN reactions already avoided this (their WebSocket
echo is suppressed, so _on_own_reaction_sent() has always explicitly
persisted a synthetic reactionMessage record) — this extends the exact same
mechanism to a reaction received from someone else.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so on_incoming_message() is exercised against a small stub
— same approach as tests/test_unread_separator_reuse.py.
"""

from ui.conversations import ConversationsPanel


class _FakeMessagesList:
    def __init__(self):
        self.items = []

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def InsertItem(self, pos, text):
        self.items.insert(pos, text)

    def DeleteItem(self, pos):
        del self.items[pos]

    def Append(self, row):
        self.items.append(row[0])

    def GetFocusedItem(self):
        return -1

    def SetItemText(self, pos, text):
        self.items[pos] = text


class _FakeDB:
    def __init__(self):
        self.inserted = []

    def queue_message(self, jid, record):
        self.inserted.append((jid, record))


class _FakeMainWindow:
    def __init__(self, chat):
        self._chat = chat
        self.db = _FakeDB()
        self.track_last_reaction_calls = []
        self.schedule_set_chats_calls = 0

    def _allow_ui_focus_changes(self):
        return False

    def get_chat(self, jid):
        return self._chat

    def _track_last_reaction(self, jid, record):
        self.track_last_reaction_calls.append((jid, record))

    def _schedule_set_chats(self):
        self.schedule_set_chats_calls += 1


class _Stub:
    on_incoming_message      = ConversationsPanel.on_incoming_message
    apply_incoming_reaction  = ConversationsPanel.apply_incoming_reaction
    _matches_open_conversation = ConversationsPanel._matches_open_conversation
    _persist_reaction_record = ConversationsPanel._persist_reaction_record
    _reactor_key_from_msg    = ConversationsPanel._reactor_key_from_msg
    _is_separator            = ConversationsPanel._is_separator
    _SELF_REACTOR_KEY        = ConversationsPanel._SELF_REACTOR_KEY

    def __init__(self, jid, existing_messages=None):
        chat = {"messages": {"messages": {"records": list(existing_messages or [])}}}
        self.main_window = _FakeMainWindow(chat)
        self.messages_list = _FakeMessagesList()
        self._sorted_messages = list(existing_messages or [])
        self.messages_list.items = [m["key"]["id"] for m in self._sorted_messages]
        self._unread_sep_idx = -1
        self._sep_from_open = False
        self._unread_sep_marked_read = False
        self.conversation = {"remoteJid": jid}
        self._current_audio_id = None
        self._reaction_map = {}
        self._render_separator = lambda count: f"__sep__{count}"
        self._render_message_line = lambda msg, *a, **kw: msg["key"]["id"]

    def _records(self):
        return self.main_window._chat["messages"]["messages"]["records"]


JID = "5511999999999@s.whatsapp.net"


def _orig_msg(mid="m1"):
    return {"key": {"id": mid, "fromMe": False},
            "messageType": "conversation", "message": {"conversation": "oi"}}


def _reaction_from_other(orig_id="m1", emoji="👍", participant="5521888888888@s.whatsapp.net"):
    return {
        "key": {"id": "rxn-evt-1", "fromMe": False, "remoteJid": JID, "participant": participant},
        "messageType": "reactionMessage",
        "message": {"reactionMessage": {"key": {"id": orig_id}, "text": emoji}},
    }


class TestReactionFromSomeoneElseIsPersisted:
    def test_a_record_is_appended_and_inserted_into_the_db(self):
        stub = _Stub(JID, existing_messages=[_orig_msg()])

        stub.on_incoming_message(JID, _reaction_from_other())

        records = stub._records()
        rxn_records = [r for r in records if r.get("messageType") == "reactionMessage"]
        assert len(rxn_records) == 1
        assert rxn_records[0]["message"]["reactionMessage"]["text"] == "👍"
        assert rxn_records[0]["message"]["reactionMessage"]["key"]["id"] == "m1"
        assert len(stub.main_window.db.inserted) == 1

    def test_persisted_record_is_excluded_from_last_reaction_and_chat_refresh_here(self):
        """main.py's on_new_message() already calls _track_last_reaction()/
        _schedule_set_chats() itself for every reaction from someone else —
        on_incoming_message() must not also call them, or the chat-list
        preview logic runs twice per reaction."""
        stub = _Stub(JID, existing_messages=[_orig_msg()])

        stub.on_incoming_message(JID, _reaction_from_other())

        assert stub.main_window.track_last_reaction_calls == []
        assert stub.main_window.schedule_set_chats_calls == 0

    def test_the_persisted_record_rebuilds_the_reaction_map_correctly(self):
        """The real-world recovery path: simulate what
        refresh_active_conversation_messages() does — rebuild _reaction_map
        purely from `records` — and confirm the persisted reaction survives
        that rebuild, which is exactly what was broken before this fix."""
        stub = _Stub(JID, existing_messages=[_orig_msg()])
        stub.on_incoming_message(JID, _reaction_from_other())

        # Simulate a full rebuild reading only from persisted records.
        rebuilt_map = {}
        for m in stub._records():
            if m.get("messageType") == "reactionMessage":
                reaction = m["message"]["reactionMessage"]
                orig_id = reaction["key"]["id"]
                sender_key = stub._reactor_key_from_msg(m)
                rebuilt_map.setdefault(orig_id, {})[sender_key] = reaction["text"]

        assert rebuilt_map == {"m1": {"5521888888888@s.whatsapp.net": "👍"}}

    def test_multiple_senders_reacting_to_the_same_message_do_not_collide(self):
        stub = _Stub(JID, existing_messages=[_orig_msg()])

        stub.on_incoming_message(JID, _reaction_from_other(participant="a@s.whatsapp.net", emoji="👍"))
        stub.on_incoming_message(JID, _reaction_from_other(participant="b@s.whatsapp.net", emoji="😂"))

        rxn_records = [r for r in stub._records() if r.get("messageType") == "reactionMessage"]
        assert len(rxn_records) == 2
        emojis = {r["key"]["participant"]: r["message"]["reactionMessage"]["text"] for r in rxn_records}
        assert emojis == {"a@s.whatsapp.net": "👍", "b@s.whatsapp.net": "😂"}

    def test_changing_the_same_senders_reaction_updates_in_place(self):
        stub = _Stub(JID, existing_messages=[_orig_msg()])

        stub.on_incoming_message(JID, _reaction_from_other(emoji="👍"))
        stub.on_incoming_message(JID, _reaction_from_other(emoji="😂"))

        rxn_records = [r for r in stub._records() if r.get("messageType") == "reactionMessage"]
        assert len(rxn_records) == 1
        assert rxn_records[0]["message"]["reactionMessage"]["text"] == "😂"

    def test_removing_a_reaction_still_persists_the_empty_state(self):
        stub = _Stub(JID, existing_messages=[_orig_msg()])
        stub.on_incoming_message(JID, _reaction_from_other(emoji="👍"))

        stub.on_incoming_message(JID, _reaction_from_other(emoji=""))

        rxn_records = [r for r in stub._records() if r.get("messageType") == "reactionMessage"]
        assert len(rxn_records) == 1
        assert rxn_records[0]["message"]["reactionMessage"]["text"] == ""
