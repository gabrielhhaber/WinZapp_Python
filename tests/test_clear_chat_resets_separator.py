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

from ui.conversations import ArchivedConversationsPanel, ConversationsPanel


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

    def clear_chat(self, jid, keep_starred=True):
        self.clear_chat_calls.append(jid)
        self.keep_starred = keep_starred

    def _schedule_set_chats(self):
        self.schedule_set_chats_calls += 1

    def add_chats_to_ui(self):
        pass

    def output(self, *a, **k):
        pass


class _Stub:
    _on_menu_clear_chat = ConversationsPanel._on_menu_clear_chat
    _on_mass_clear_chats = ConversationsPanel._on_mass_clear_chats
    _reset_view_after_chat_cleared = ConversationsPanel._reset_view_after_chat_cleared

    def __init__(self, jid):
        self.main_window = _FakeMainWindow()
        self.messages_list = _FakeMessagesList()
        self.conversation = {"remoteJid": jid}
        self._sorted_messages = [{"key": {"id": "m1"}}, {"key": {"id": "m2"}}]
        self._unread_sep_idx = 1
        self._sep_anchors_read_position = True
        self._first_unread_msg_id = "m2"
        self._first_unread_count = 3
        self.selected_messages = {"m1", "m2"}


JID = "120363409931936700@g.us"


class TestClearChatResetsSeparatorBookkeeping:
    def test_resets_unread_sep_idx_and_sep_anchors_read_position(self, monkeypatch):
        monkeypatch.setattr("ui.conversations.confirm_clear_chat", lambda *a, **kw: (True, True))
        stub = _Stub(JID)

        stub._on_menu_clear_chat(JID)

        assert stub._sorted_messages == []
        assert stub._unread_sep_idx == -1
        assert stub._sep_anchors_read_position is False
        # A âncora vai junto: populate_messages() recria o separador a partir
        # dela, e um id que não existe mais deixaria um separador fantasma.
        assert stub._first_unread_msg_id is None
        assert stub._first_unread_count == 0
        assert stub.selected_messages == set()
        assert stub.messages_list.delete_all_calls == 1
        assert stub.main_window.clear_chat_calls == [JID]

    def test_declining_the_confirmation_touches_nothing(self, monkeypatch):
        monkeypatch.setattr("ui.conversations.confirm_clear_chat", lambda *a, **kw: (False, True))
        stub = _Stub(JID)

        stub._on_menu_clear_chat(JID)

        assert stub._sorted_messages != []
        assert stub._unread_sep_idx == 1
        assert stub.selected_messages == {"m1", "m2"}
        assert stub.main_window.clear_chat_calls == []

    def test_clearing_a_different_conversation_than_the_open_one_leaves_separator_alone(self, monkeypatch):
        """The clear applies to some other chat — the currently open
        conversation's own separator bookkeeping is unrelated and must not
        be touched."""
        monkeypatch.setattr("ui.conversations.confirm_clear_chat", lambda *a, **kw: (True, True))
        stub = _Stub(JID)

        stub._on_menu_clear_chat("some-other-chat@g.us")

        assert stub._sorted_messages != []
        assert stub._unread_sep_idx == 1
        assert stub.selected_messages == {"m1", "m2"}
        assert stub.main_window.clear_chat_calls == ["some-other-chat@g.us"]


class TestKeepStarredChoiceIsForwarded:
    """The "keep starred messages" checkbox in the confirmation decides what
    clear_chat() is asked to do — it is no longer fixed in code."""

    def test_unticked_checkbox_clears_starred_messages_too(self, monkeypatch):
        monkeypatch.setattr("ui.conversations.confirm_clear_chat", lambda *a, **kw: (True, False))
        stub = _Stub(JID)

        stub._on_menu_clear_chat(JID)

        assert stub.main_window.keep_starred is False

    def test_ticked_checkbox_keeps_starred_messages(self, monkeypatch):
        monkeypatch.setattr("ui.conversations.confirm_clear_chat", lambda *a, **kw: (True, True))
        stub = _Stub(JID)

        stub._on_menu_clear_chat(JID)

        assert stub.main_window.keep_starred is True

    def test_the_checkbox_label_is_the_keep_starred_string(self, monkeypatch):
        seen = []

        def _fake(parent, message, title, label, **kw):
            seen.append(label)
            return False, True

        monkeypatch.setattr("ui.conversations.confirm_clear_chat", _fake)
        _Stub(JID)._on_menu_clear_chat(JID)

        assert seen == ["clear_chat_keep_starred"]


def _assert_open_view_reset(stub):
    assert stub._sorted_messages == []
    assert stub.selected_messages == set()
    assert stub._unread_sep_idx == -1
    assert stub._first_unread_msg_id is None
    assert stub.messages_list.delete_all_calls == 1


class TestEveryClearEntryPointResetsTheOpenConversation:
    """Clearing the open chat from the mass action or from the archived list
    left its rows and its marked messages behind — and the selection mode is
    on whenever anything is marked, so Space kept marking instead of playing
    and the first Esc announced "all unmarked" instead of closing."""

    def test_mass_clear_including_the_open_chat(self, monkeypatch):
        monkeypatch.setattr("ui.conversations.confirm_clear_chat", lambda *a, **kw: (True, True))
        stub = _Stub(JID)
        stub.selected_chats = {JID, "other@s.whatsapp.net"}

        stub._on_mass_clear_chats(None)

        _assert_open_view_reset(stub)
        assert sorted(stub.main_window.clear_chat_calls) == sorted([JID, "other@s.whatsapp.net"])

    def test_mass_clear_of_other_chats_leaves_the_open_one_alone(self, monkeypatch):
        monkeypatch.setattr("ui.conversations.confirm_clear_chat", lambda *a, **kw: (True, True))
        stub = _Stub(JID)
        stub.selected_chats = {"other@s.whatsapp.net"}

        stub._on_mass_clear_chats(None)

        assert stub.selected_messages == {"m1", "m2"}
        assert stub.messages_list.delete_all_calls == 0

    def test_archived_list_clear_of_the_open_chat(self, monkeypatch):
        monkeypatch.setattr("ui.conversations.confirm_clear_chat", lambda *a, **kw: (True, True))
        panel = _Stub(JID)
        archived = type("_Archived", (), {"_on_clear": ArchivedConversationsPanel._on_clear})()
        archived.main_window = panel.main_window
        panel.main_window.conversations_panel = panel

        archived._on_clear(JID)

        _assert_open_view_reset(panel)
        assert panel.main_window.clear_chat_calls == [JID]
