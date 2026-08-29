"""Tests for "preserve typed message as caption when attaching" (settings >
user interface, on by default). When something is already typed in
message_field, attaching a file moves that text into the attachment caption
field instead of leaving it stranded in the (now hidden) message field.
"""

import pytest
import wx

from ui.conversations import ConversationsPanel
from tests.conftest import hidden_frame


class _SettingsStub(dict):
    def __init__(self, preserve=True):
        super().__init__(
            user_interface={"preserve_typed_text_as_attachment_caption": preserve}
        )


class _MainWindowStub:
    def __init__(self, preserve=True):
        self.settings = _SettingsStub(preserve)


class _Stub:
    _apply_typed_text_as_caption = ConversationsPanel._apply_typed_text_as_caption

    def __init__(self, frame, preserve=True):
        self.main_window = _MainWindowStub(preserve)
        self.message_field = wx.TextCtrl(frame, style=wx.TE_MULTILINE)
        self._caption_field = wx.TextCtrl(frame)


class TestPreserveTypedTextAsCaption:
    def test_moves_typed_text_into_caption(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame)
            stub.message_field.SetValue("oi tudo bem")

            stub._apply_typed_text_as_caption()

            assert stub._caption_field.GetValue() == "oi tudo bem"
            assert stub.message_field.GetValue() == ""
        finally:
            frame.Destroy()

    def test_does_nothing_when_message_field_is_empty(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame)

            stub._apply_typed_text_as_caption()

            assert stub._caption_field.GetValue() == ""
        finally:
            frame.Destroy()

    def test_does_not_clobber_an_existing_caption(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame)
            stub._caption_field.SetValue("legenda já digitada")
            stub.message_field.SetValue("outra coisa")

            stub._apply_typed_text_as_caption()

            assert stub._caption_field.GetValue() == "legenda já digitada"
            # The typed text stays put since it wasn't moved anywhere.
            assert stub.message_field.GetValue() == "outra coisa"
        finally:
            frame.Destroy()

    def test_disabled_by_setting_leaves_both_fields_alone(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame, preserve=False)
            stub.message_field.SetValue("oi tudo bem")

            stub._apply_typed_text_as_caption()

            assert stub._caption_field.GetValue() == ""
            assert stub.message_field.GetValue() == "oi tudo bem"
        finally:
            frame.Destroy()

    def test_whitespace_only_text_is_not_moved(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame)
            stub.message_field.SetValue("   ")

            stub._apply_typed_text_as_caption()

            assert stub._caption_field.GetValue() == ""
        finally:
            frame.Destroy()

    def test_unicode_separators_are_normalized_when_moved(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame)
            stub.message_field.SetValue("A B")

            stub._apply_typed_text_as_caption()

            assert stub._caption_field.GetValue() == "A\nB"
        finally:
            frame.Destroy()


class TestShowAttachmentPanelCallsIt:
    def test_show_attachment_panel_applies_typed_text(self):
        import inspect

        src = inspect.getsource(ConversationsPanel._show_attachment_panel)
        assert "self._apply_typed_text_as_caption()" in src
