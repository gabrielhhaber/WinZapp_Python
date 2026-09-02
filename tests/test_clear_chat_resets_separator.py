"""Test for ConversationsPanel._on_menu_clear_chat() resetting the unread-
separator bookkeeping when clearing the currently open conversation.

Reported live: "Limpar conversa" on the currently open chat reset
_sorted_messages to [] but left _unread_sep_idx/_sep_anchors_read_position pointing at
the pre-clear position. A live message arriving right after crashed with
"IndexError: pop from empty list" in on_incoming_message() (see
tests/test_unread_separator_reuse.py::TestStaleSeparatorIndexDoesNotCrash
for that side of the same bug). This covers the other half: the reset
itself actually happening at the clear site.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so the method is exercised against a small stub — same
approach as tests/test_pin_message.py.
"""

import wx

from ui.conversations import ConversationsPanel


class _FakeMessagesList:
    def __init__(self):
        self.delete_all_calls = 0

    def DeleteAllItems(self):
        self.delete_all_calls += 1


class _FakeMainWindow:
    def __init__(self):
        self.clear_chat_calls = []
        self.schedule_set_chats_calls = 0
        self.i18n = type("I18n", (), {"t": lambda self, key: key})()

    def clear_chat(self, jid):
        self.clear_chat_calls.append(jid)

    def _schedule_set_chats(self):
        self.schedule_set_chats_calls += 1


class _Stub:
    _on_menu_clear_chat = ConversationsPanel._on_menu_clear_chat

    def __init__(self, jid):
        self.main_window = _FakeMainWindow()
        self.messages_list = _FakeMessagesList()
        self.conversation = {"remoteJid": jid}
        self._sorted_messages = [{"key": {"id": "m1"}}, {"key": {"id": "m2"}}]
        self._unread_sep_idx = 1
        self._sep_anchors_read_position = True
        self._first_unread_msg_id = "m2"
        self._first_unread_count = 3


JID = "120363409931936700@g.us"


class TestClearChatResetsSeparatorBookkeeping:
    def test_resets_unread_sep_idx_and_sep_anchors_read_position(self, monkeypatch):
        monkeypatch.setattr("ui.conversations.wx.MessageBox", lambda *a, **kw: wx.YES)
        stub = _Stub(JID)

        stub._on_menu_clear_chat(JID)

        assert stub._sorted_messages == []
        assert stub._unread_sep_idx == -1
        assert stub._sep_anchors_read_position is False
        # A âncora vai junto: populate_messages() recria o separador a partir
        # dela, e um id que não existe mais deixaria um separador fantasma.
        assert stub._first_unread_msg_id is None
        assert stub._first_unread_count == 0
        assert stub.messages_list.delete_all_calls == 1
        assert stub.main_window.clear_chat_calls == [JID]

    def test_declining_the_confirmation_touches_nothing(self, monkeypatch):
        monkeypatch.setattr("ui.conversations.wx.MessageBox", lambda *a, **kw: wx.NO)
        stub = _Stub(JID)

        stub._on_menu_clear_chat(JID)

        assert stub._sorted_messages != []
        assert stub._unread_sep_idx == 1
        assert stub.main_window.clear_chat_calls == []

    def test_clearing_a_different_conversation_than_the_open_one_leaves_separator_alone(self, monkeypatch):
        """The clear applies to some other chat — the currently open
        conversation's own separator bookkeeping is unrelated and must not
        be touched."""
        monkeypatch.setattr("ui.conversations.wx.MessageBox", lambda *a, **kw: wx.YES)
        stub = _Stub(JID)

        stub._on_menu_clear_chat("some-other-chat@g.us")

        assert stub._sorted_messages != []
        assert stub._unread_sep_idx == 1
        assert stub.main_window.clear_chat_calls == ["some-other-chat@g.us"]
