"""Tests that archiving/unarchiving a conversation from the list requires
Ctrl+Shift+Q, not plain Ctrl+Q.

Reported live/requested: plain Ctrl+Q sits right next to other single-Ctrl
combos a user can easily fat-finger while just navigating the conversation
list, and archiving isn't as trivially reversible as e.g. pinning. Moved to
Ctrl+Shift+Q everywhere the shortcut exists: the accelerator table entry,
the raw key handler ConversationsPanel._on_conv_list_key_down() also
duplicates it in, and the context-menu labels.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so the method under test is exercised as a plain function
against a stub — same approach as the rest of this test suite.
"""

import wx

from ui.conversations import ConversationsPanel


class _FakeKeyEvent:
    def __init__(self, key_code, ctrl=False, shift=False):
        self._key_code = key_code
        self._ctrl = ctrl
        self._shift = shift
        self.skipped = False

    def GetKeyCode(self):
        return self._key_code

    def ControlDown(self):
        return self._ctrl

    def ShiftDown(self):
        return self._shift

    def Skip(self):
        self.skipped = True


class _FakeConversationsList:
    def __init__(self, focused_item=0):
        self._focused_item = focused_item

    def GetFocusedItem(self):
        return self._focused_item


JID = "5511999999999@s.whatsapp.net"


class _Stub:
    _on_conv_list_key_down = ConversationsPanel._on_conv_list_key_down
    _handle_chat_selection_key = ConversationsPanel._handle_chat_selection_key
    _clear_chat_list_filters = lambda self: False  # Esc-clears-filters has its own tests

    def __init__(self, archived=False):
        self.conversations_list = _FakeConversationsList(0)
        self.chats_list = [{"remoteJid": JID}]
        self.main_window = type("MW", (), {
            "is_chat_archived": lambda self, j: archived,
            "is_chat_pinned": lambda self, j: False,
        })()
        self.archive_calls = []
        self.unarchive_calls = []

    def _on_menu_archive(self, jid):
        self.archive_calls.append(jid)

    def _on_menu_unarchive(self, jid):
        self.unarchive_calls.append(jid)

    def _on_menu_pin(self, jid):
        pass

    def _on_menu_unpin(self, jid):
        pass

    def _jump_list_by(self, lst, delta):
        pass

    def on_conversation_selected_by_index(self, idx):
        pass


class TestArchiveShortcutRequiresShift:
    def test_ctrl_q_alone_does_not_archive(self):
        stub = _Stub(archived=False)
        event = _FakeKeyEvent(ord("Q"), ctrl=True, shift=False)

        stub._on_conv_list_key_down(event)

        assert stub.archive_calls == []
        assert event.skipped is True

    def test_ctrl_shift_q_archives(self):
        stub = _Stub(archived=False)
        event = _FakeKeyEvent(ord("Q"), ctrl=True, shift=True)

        stub._on_conv_list_key_down(event)

        assert stub.archive_calls == [JID]
        assert event.skipped is False

    def test_ctrl_shift_q_unarchives_when_already_archived(self):
        stub = _Stub(archived=True)
        event = _FakeKeyEvent(ord("Q"), ctrl=True, shift=True)

        stub._on_conv_list_key_down(event)

        assert stub.unarchive_calls == [JID]
        assert event.skipped is False

    def test_shift_q_alone_without_ctrl_does_not_archive(self):
        stub = _Stub(archived=False)
        event = _FakeKeyEvent(ord("Q"), ctrl=False, shift=True)

        stub._on_conv_list_key_down(event)

        assert stub.archive_calls == []
        assert event.skipped is True
