"""The attachment caption accepts several lines: Shift+Enter and Ctrl+Enter
add one, plain Enter is left alone so EVT_TEXT_ENTER keeps sending."""

import wx

from ui.conversations import ConversationsPanel
from tests.conftest import hidden_frame


class _Key:
    def __init__(self, code, shift=False, ctrl=False):
        self.code, self.shift, self.ctrl, self.skipped = code, shift, ctrl, False

    def GetKeyCode(self):
        return self.code

    def ShiftDown(self):
        return self.shift

    def ControlDown(self):
        return self.ctrl

    def Skip(self):
        self.skipped = True


class _Stub:
    _on_caption_field_key_down = ConversationsPanel._on_caption_field_key_down

    def __init__(self, frame):
        self._caption_field = wx.TextCtrl(frame, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER)


def _run(wx_app, key, expected_value, expected_skip):
    frame = hidden_frame()
    try:
        stub = _Stub(frame)
        stub._caption_field.SetValue("ab")
        stub._caption_field.SetInsertionPoint(1)
        stub._on_caption_field_key_down(key)
        assert stub._caption_field.GetValue() == expected_value
        assert key.skipped is expected_skip
    finally:
        frame.Destroy()


def test_shift_enter_adds_line(wx_app):
    _run(wx_app, _Key(wx.WXK_RETURN, shift=True), "a\nb", False)


def test_ctrl_enter_adds_line(wx_app):
    _run(wx_app, _Key(wx.WXK_RETURN, ctrl=True), "a\nb", False)


def test_plain_enter_is_left_to_send(wx_app):
    _run(wx_app, _Key(wx.WXK_RETURN), "ab", True)
