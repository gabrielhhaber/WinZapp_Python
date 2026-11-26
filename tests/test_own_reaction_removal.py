"""Issue #67: there was previously no way to remove your own reaction from
the UI at all — every path that could send a reaction only ever sent a
non-empty emoji. _on_menu_react()'s dialog now checks the emoji you already
sent and activating it again sends an empty one to remove it (same for the
"most used reactions" submenu's checked item); _on_own_reaction_sent() must
in turn actually drop the stale entry from _reaction_map, or the message
would keep showing your old reaction badge after "removing" it.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so _on_own_reaction_sent() is exercised against a stub in
the same shape as tests/test_reaction_persisted_from_others.py.
"""

from ui.conversations import ConversationsPanel


class _FakeMessagesList:
    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def SetItemText(self, pos, text):
        pass


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

    def get_chat(self, jid):
        return self._chat

    def _track_last_reaction(self, jid, record):
        self.track_last_reaction_calls.append((jid, record))

    def _schedule_set_chats(self):
        self.schedule_set_chats_calls += 1


class _Stub:
    _on_own_reaction_sent    = ConversationsPanel._on_own_reaction_sent
    _persist_reaction_record = ConversationsPanel._persist_reaction_record
    _matches_open_conversation = ConversationsPanel._matches_open_conversation
    _SELF_REACTOR_KEY        = ConversationsPanel._SELF_REACTOR_KEY

    def __init__(self, jid, existing_messages=None, reaction_map=None):
        chat = {"messages": {"messages": {"records": list(existing_messages or [])}}}
        self.main_window = _FakeMainWindow(chat)
        self.messages_list = _FakeMessagesList()
        self._sorted_messages = list(existing_messages or [])
        self.conversation = {"remoteJid": jid}
        self._reaction_map = reaction_map if reaction_map is not None else {}
        self._render_message_line = lambda msg, *a, **kw: msg["key"]["id"]

    def _records(self):
        return self.main_window._chat["messages"]["messages"]["records"]


JID = "5511999999999@s.whatsapp.net"


class TestOwnReactionRemoval:
    def test_sending_an_empty_emoji_drops_the_reaction_map_entry(self):
        stub = _Stub(JID, reaction_map={"m1": {ConversationsPanel._SELF_REACTOR_KEY: "❤️"}})

        stub._on_own_reaction_sent(JID, {"id": "m1"}, "")

        assert "m1" not in stub._reaction_map or stub._SELF_REACTOR_KEY not in stub._reaction_map["m1"]

    def test_sending_an_empty_emoji_does_not_crash_with_no_prior_entry(self):
        stub = _Stub(JID, reaction_map={})

        stub._on_own_reaction_sent(JID, {"id": "m1"}, "")  # must not raise

        assert stub._reaction_map.get("m1", {}) == {}

    def test_sending_a_real_emoji_still_sets_it(self):
        stub = _Stub(JID)

        stub._on_own_reaction_sent(JID, {"id": "m1"}, "👍")

        assert stub._reaction_map["m1"][stub._SELF_REACTOR_KEY] == "👍"

    def test_removal_persists_the_empty_state_so_a_rebuild_stays_removed(self):
        """Mirrors test_reaction_persisted_from_others.py's equivalent case
        for someone else's removal — our own removal must survive a
        populate_messages() rebuild the same way."""
        stub = _Stub(JID)
        stub._on_own_reaction_sent(JID, {"id": "m1"}, "👍")

        stub._on_own_reaction_sent(JID, {"id": "m1"}, "")

        rxn_records = [r for r in stub._records() if r.get("messageType") == "reactionMessage"]
        assert len(rxn_records) == 1
        assert rxn_records[0]["message"]["reactionMessage"]["text"] == ""

    def test_removal_still_updates_the_chat_list_preview(self):
        stub = _Stub(JID)

        stub._on_own_reaction_sent(JID, {"id": "m1"}, "")

        assert stub.main_window.schedule_set_chats_calls == 1
