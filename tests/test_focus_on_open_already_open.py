"""Choosing, in the conversation list, a conversation that is already open must
follow Settings > User Interface > "focus_on_open" like opening it does.

navigate_to_conversation() used to short-circuit the already-open case and put
focus on the message field unconditionally — ignoring the setting (a user who
chose "unread separator or last message" landed in the edit field) and
ignoring take_focus. ConversationsPanel is a wx.Panel, so the unbound methods
are bound onto a plain stub.
"""

import wx

from ui.conversations import ConversationsPanel
from ui.conversation_panel.pinned_messages import PinnedMessagesMixin

JID = "5511999999999@s.whatsapp.net"


class _Field:
    def __init__(self, enabled=True):
        self._enabled = enabled
        self.focused = 0

    def IsEnabled(self):
        return self._enabled

    def SetFocus(self):
        self.focused += 1


class _List:
    def __init__(self, count, focus_log):
        self._count = count
        self.log = focus_log
        self.focused_row = None
        self.selected_row = None
        self.visible_row = None
        self.set_focus_calls = 0

    def GetItemCount(self):
        return self._count

    def Focus(self, row):
        self.focused_row = row

    def Select(self, row, on=True):
        self.selected_row = row

    def EnsureVisible(self, row):
        self.visible_row = row

    def SetFocus(self):
        self.set_focus_calls += 1


class _Layout:
    def Layout(self):
        pass

    def Show(self, show=True):
        pass


class _Stub(PinnedMessagesMixin):
    def _start_contact_presence(self):
        pass  # Presence timers are tested separately without a native window.
    _load_pinned_messages = lambda self, **kwargs: None
    _open_focus_target = ConversationsPanel._open_focus_target
    _focus_already_open_conversation = ConversationsPanel._focus_already_open_conversation
    navigate_to_conversation = ConversationsPanel.navigate_to_conversation

    def __init__(self, setting="message_field", rows=5, sep_idx=-1, field_enabled=True):
        self.main_window = type("MW", (), {
            "settings": {"user_interface": {"focus_on_open": setting}},
        })()
        self.message_field = _Field(field_enabled)
        self.messages_list = _List(rows, [])
        self._unread_sep_idx = sep_idx
        self.conversation = {"remoteJid": JID}
        self.conversation_panel = _Layout()

    def Layout(self):
        pass

    def _begin_conversation_visit(self, conversation, origin=None):
        pass

    def _sync_voice_call_button(self, jid):
        pass


def _open_again(stub, monkeypatch, **kwargs):
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
    stub.navigate_to_conversation({"remoteJid": JID}, **kwargs)


class TestOpenFocusTarget:
    def test_message_field_by_default(self):
        assert _Stub()._open_focus_target() == "message_field"

    def test_unread_or_last_means_the_messages_list(self):
        assert _Stub(setting="unread_or_last")._open_focus_target() == "messages_list"

    def test_a_field_that_cannot_take_input_sends_focus_to_the_list(self):
        assert _Stub(field_enabled=False)._open_focus_target() == "messages_list"


class TestAnAlreadyOpenConversation:
    def test_message_field_setting_focuses_the_field(self, monkeypatch):
        stub = _Stub(setting="message_field")
        _open_again(stub, monkeypatch)
        assert stub.message_field.focused == 1
        assert stub.messages_list.set_focus_calls == 0

    def test_unread_or_last_focuses_the_last_message(self, monkeypatch):
        stub = _Stub(setting="unread_or_last", rows=5)
        _open_again(stub, monkeypatch)
        assert stub.message_field.focused == 0
        assert stub.messages_list.set_focus_calls == 1
        assert (stub.messages_list.focused_row, stub.messages_list.selected_row) == (4, 4)
        assert stub.messages_list.visible_row == 4

    def test_unread_or_last_prefers_the_unread_separator(self, monkeypatch):
        stub = _Stub(setting="unread_or_last", rows=8, sep_idx=3)
        _open_again(stub, monkeypatch)
        assert (stub.messages_list.focused_row, stub.messages_list.selected_row) == (3, 3)

    def test_a_separator_already_read_past_does_not_attract_focus(self, monkeypatch):
        stub = _Stub(setting="unread_or_last", rows=8, sep_idx=3)
        stub._sep_anchors_read_position = True
        _open_again(stub, monkeypatch)
        assert stub.messages_list.focused_row == 7

    def test_a_stale_separator_index_falls_back_to_the_last_message(self, monkeypatch):
        stub = _Stub(setting="unread_or_last", rows=3, sep_idx=9)
        _open_again(stub, monkeypatch)
        assert stub.messages_list.focused_row == 2

    def test_an_empty_list_still_gets_keyboard_focus(self, monkeypatch):
        stub = _Stub(setting="unread_or_last", rows=0)
        _open_again(stub, monkeypatch)
        assert stub.messages_list.focused_row is None
        assert stub.messages_list.set_focus_calls == 1

    def test_a_read_only_conversation_goes_to_the_list_whatever_the_setting(self, monkeypatch):
        stub = _Stub(setting="message_field", field_enabled=False)
        _open_again(stub, monkeypatch)
        assert stub.message_field.focused == 0
        assert stub.messages_list.set_focus_calls == 1

    def test_take_focus_false_moves_nothing(self, monkeypatch):
        stub = _Stub(setting="message_field")
        _open_again(stub, monkeypatch, take_focus=False)
        assert stub.message_field.focused == 0
        assert stub.messages_list.set_focus_calls == 0
