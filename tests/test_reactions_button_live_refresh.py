"""Reported live (follow-up to issue #67): when a reaction landed on the
message row the user already had focused, the Reactions button stayed in
whatever state it was in before — it only ever refreshed on a focus-change
event (_update_reactions_button() was called only from the list's
EVT_LIST_ITEM_FOCUSED handler), so the user had to move focus away and back
just to make an already-focused message's Reactions button appear.

apply_incoming_reaction() (a reaction from someone else) and
_on_own_reaction_sent() (our own) both now also call
_update_reactions_button() directly when the reacted-to row happens to be
the one currently focused, in addition to the existing focus-driven path.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so both methods are exercised against a stub in the same
shape as tests/test_reaction_persisted_from_others.py and
tests/test_own_reaction_removal.py.
"""

from ui.conversations import ConversationsPanel


class _FakeMessagesList:
    def __init__(self, focused_item=-1):
        self._focused_item = focused_item

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def SetItemText(self, pos, text):
        pass

    def GetFocusedItem(self):
        return self._focused_item


class _FakeButton:
    def __init__(self):
        self.shown = False
        self.label = ""

    def SetLabel(self, text):
        self.label = text

    def Show(self):
        self.shown = True

    def Hide(self):
        self.shown = False


class _FakePanel:
    def Layout(self):
        pass


class _FakeI18n:
    def t(self, key):
        return key


class _FakeDB:
    def queue_message(self, jid, record):
        pass


class _FakeMainWindow:
    def __init__(self, chat):
        self._chat = chat
        self.db = _FakeDB()
        self.i18n = _FakeI18n()

    def get_chat(self, jid):
        return self._chat

    def _track_last_reaction(self, jid, record):
        pass

    def _schedule_set_chats(self):
        pass


class _Stub:
    apply_incoming_reaction   = ConversationsPanel.apply_incoming_reaction
    _on_own_reaction_sent     = ConversationsPanel._on_own_reaction_sent
    _persist_reaction_record  = ConversationsPanel._persist_reaction_record
    _reactor_key_from_msg     = ConversationsPanel._reactor_key_from_msg
    _reaction_counts          = ConversationsPanel._reaction_counts
    _matches_open_conversation = ConversationsPanel._matches_open_conversation
    _update_reactions_button  = ConversationsPanel._update_reactions_button
    _is_separator             = ConversationsPanel._is_separator
    _SELF_REACTOR_KEY         = ConversationsPanel._SELF_REACTOR_KEY

    def __init__(self, jid, existing_messages, focused_idx):
        chat = {"messages": {"messages": {"records": list(existing_messages)}}}
        self.main_window = _FakeMainWindow(chat)
        self.messages_list = _FakeMessagesList(focused_item=focused_idx)
        self._sorted_messages = list(existing_messages)
        self.conversation = {"remoteJid": jid}
        self._reaction_map = {}
        self._reactions_btn = _FakeButton()
        self._reactions_focused_msg_id = ""
        self.conversation_panel = _FakePanel()
        self._render_message_line = lambda msg, *a, **kw: msg["key"]["id"]


JID = "5511999999999@s.whatsapp.net"


def _orig_msg(mid="m1"):
    return {"key": {"id": mid, "fromMe": False}, "messageType": "conversation",
            "message": {"conversation": "oi"}}


def _reaction_from_other(orig_id="m1", emoji="👍", participant="5521888888888@s.whatsapp.net"):
    return {
        "key": {"id": "rxn-evt-1", "fromMe": False, "remoteJid": JID, "participant": participant},
        "messageType": "reactionMessage",
        "message": {"reactionMessage": {"key": {"id": orig_id}, "text": emoji}},
    }


class TestReactionsButtonRefreshesForTheFocusedRow:
    def test_a_reaction_on_the_currently_focused_message_shows_the_button(self):
        stub = _Stub(JID, [_orig_msg("m1")], focused_idx=0)

        stub.apply_incoming_reaction(JID, _reaction_from_other())

        assert stub._reactions_btn.shown is True
        assert stub._reactions_focused_msg_id == "m1"

    def test_a_reaction_on_a_row_that_is_not_focused_leaves_the_button_alone(self):
        stub = _Stub(JID, [_orig_msg("m1"), _orig_msg("m2")], focused_idx=1)

        stub.apply_incoming_reaction(JID, _reaction_from_other(orig_id="m1"))

        # Button was never touched — still in its initial hidden state.
        assert stub._reactions_btn.shown is False

    def test_removing_the_last_reaction_hides_the_button_immediately(self):
        stub = _Stub(JID, [_orig_msg("m1")], focused_idx=0)
        stub.apply_incoming_reaction(JID, _reaction_from_other(emoji="👍"))
        assert stub._reactions_btn.shown is True

        stub.apply_incoming_reaction(JID, _reaction_from_other(emoji=""))

        assert stub._reactions_btn.shown is False

    def test_own_reaction_sent_on_the_focused_message_shows_the_button(self):
        stub = _Stub(JID, [_orig_msg("m1")], focused_idx=0)

        stub._on_own_reaction_sent(JID, {"id": "m1"}, "❤️")

        assert stub._reactions_btn.shown is True

    def test_own_reaction_removed_on_the_focused_message_hides_the_button(self):
        stub = _Stub(JID, [_orig_msg("m1")], focused_idx=0)
        stub._on_own_reaction_sent(JID, {"id": "m1"}, "❤️")

        stub._on_own_reaction_sent(JID, {"id": "m1"}, "")

        assert stub._reactions_btn.shown is False
