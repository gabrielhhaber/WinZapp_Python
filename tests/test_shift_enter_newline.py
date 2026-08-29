"""Tests for Shift+Enter inserting a newline in the message composer
instead of sending (issue #16).

message_field is TE_MULTILINE | TE_PROCESS_ENTER, so plain Enter fires
EVT_TEXT_ENTER (send) rather than inserting a newline, and wx's native
multiline control only special-cases Ctrl+Enter for a literal newline — no
Shift+Enter equivalent existed before this fix.

ConversationsPanel is a wx.Panel and can't be instantiated without a running
wx.App, so the method under test is bound onto a plain stub carrying a real
wx.TextCtrl (needed for GetInsertionPoint()/ChangeValue() to behave
correctly) — same approach as tests/test_conversation_video_playback.py.
"""

import wx

from ui.conversations import ConversationsPanel
from tests.conftest import hidden_frame


class _FakeKeyEvent:
    def __init__(self, key_code, shift_down=False):
        self._key_code = key_code
        self._shift_down = shift_down
        self.skipped = False

    def GetKeyCode(self):
        return self._key_code

    def ShiftDown(self):
        return self._shift_down

    def Skip(self):
        self.skipped = True


class _FakeMentionPanel:
    def IsShown(self):
        return False


class _Stub:
    _on_message_field_key_down = ConversationsPanel._on_message_field_key_down
    on_change_message_field = ConversationsPanel.on_change_message_field

    def __init__(self, frame):
        self.message_field = wx.TextCtrl(frame, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER)
        self._mention_panel = _FakeMentionPanel()
        self._is_recording = False
        self._attachment_panel = _FakeMentionPanel()  # IsShown() -> False is enough
        self.conversation = None
        self.send_message_btn = wx.Panel(frame)
        self.record_voice_message_btn = wx.Panel(frame)
        self._on_text_changed_mention_check_calls = 0

    def _on_text_changed_mention_check(self):
        self._on_text_changed_mention_check_calls += 1

    def _schedule_link_preview_check(self):
        pass


class TestShiftEnterInsertsNewline:
    def test_shift_enter_inserts_newline_at_cursor(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame)
            stub.message_field.SetValue("hello world")
            stub.message_field.SetInsertionPoint(5)  # after "hello"

            event = _FakeKeyEvent(wx.WXK_RETURN, shift_down=True)
            stub._on_message_field_key_down(event)

            assert stub.message_field.GetValue() == "hello\n world"
            # Native position, not the "\n"-counted one GetValue() implies:
            # wxMSW stores the line break as \r\n internally, so the caret
            # sits 2 native characters after where the plain-\n insertion
            # started, not 1 — see issue #48's fix for why this distinction
            # matters (the old manual SetInsertionPoint(pos + 1) landed one
            # short, between the \r and the \n).
            assert stub.message_field.GetInsertionPoint() == 7
            assert not event.skipped
            assert stub._on_text_changed_mention_check_calls == 1
        finally:
            frame.Destroy()

    def test_plain_enter_is_left_alone_to_send(self, wx_app):
        """Plain Enter must not be consumed here — EVT_TEXT_ENTER (send)
        depends on the event propagating normally."""
        frame = hidden_frame()
        try:
            stub = _Stub(frame)
            stub.message_field.SetValue("hello")

            event = _FakeKeyEvent(wx.WXK_RETURN, shift_down=False)
            stub._on_message_field_key_down(event)

            assert stub.message_field.GetValue() == "hello"
            assert event.skipped
        finally:
            frame.Destroy()

    def test_typing_after_shift_enter_lands_on_the_new_line(self, wx_app):
        """The actual symptom in issue #48: after Shift+Enter, continuing to
        type kept landing on the PREVIOUS line, splitting the \\r\\n Windows
        silently expands a lone "\\n" into and producing a spurious EXTRA line
        break — confirmed against the old ChangeValue()+SetInsertionPoint()
        code, which turned this exact scenario into
        "hello\\nXYZ\\n world" instead of "hello\\nXYZ world". The old test
        only checked the value WE fed back into SetInsertionPoint(), so it
        never caught this — it needs a second WriteText() simulating the
        user's next keystrokes to actually observe where they land."""
        frame = hidden_frame()
        try:
            stub = _Stub(frame)
            stub.message_field.SetValue("hello world")
            stub.message_field.SetInsertionPoint(5)  # after "hello"

            event = _FakeKeyEvent(wx.WXK_RETURN, shift_down=True)
            stub._on_message_field_key_down(event)
            stub.message_field.WriteText("XYZ")

            assert stub.message_field.GetValue() == "hello\nXYZ world"
        finally:
            frame.Destroy()

    def test_shift_enter_also_works_with_numpad_enter(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame)
            stub.message_field.SetValue("ab")
            stub.message_field.SetInsertionPoint(1)

            event = _FakeKeyEvent(wx.WXK_NUMPAD_ENTER, shift_down=True)
            stub._on_message_field_key_down(event)

            assert stub.message_field.GetValue() == "a\nb"
            assert not event.skipped
        finally:
            frame.Destroy()
