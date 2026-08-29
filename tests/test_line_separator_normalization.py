"""Tests for Unicode line/paragraph separator normalization.

The "artificial line breaks" report: pasting text from rich sources — Google
Docs, Word, websites, Apple apps — copies U+2028 LINE SEPARATOR / U+2029
PARAGRAPH SEPARATOR into the clipboard where a plain editor stores \n. The
native wx TextCtrl keeps them verbatim: it neither renders them as breaks
(a paste looks like a single long line) nor counts them in
GetNumberOfLines(), yet WhatsApp renders U+2029 as a paragraph break for the
recipient. So the same text looks fine (or collapses to one line) in the
field and arrives on the other side full of weird breaks.

The fix normalizes these to plain \n in two places: when pasting into the
message field (_on_text_field_paste) and again at send time
(on_send_message) as a safety net for text that reaches the field by any
other route. normalize_line_separators() in core/utils.py does the actual
mapping; the paste handler is exercised with a real wx.TextCtrl (WriteText
is what inserts the normalized text), same approach as
tests/test_shift_enter_newline.py.
"""

import pytest
import wx

from core.utils import normalize_line_separators
from tests.conftest import set_clipboard_text
from ui.conversations import ConversationsPanel


class TestNormalizeLineSeparators:
    @pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\u0085", "\x0b", "\x0c"])
    def test_every_unicode_separator_becomes_a_newline(self, sep):
        assert normalize_line_separators(f"A{sep}B") == "A\nB"

    def test_paragraph_separator_map_is_the_reported_bug(self):
        # The actual reproduction from the report: a multi-paragraph paste.
        text = "Primeiro par\u00e1grafo.\u2029Segundo par\u00e1grafo."
        assert normalize_line_separators(text) == (
            "Primeiro par\u00e1grafo.\nSegundo par\u00e1grafo."
        )

    def test_plain_newlines_are_left_alone(self):
        assert normalize_line_separators("a\nb\nc") == "a\nb\nc"

    def test_crlf_and_lone_cr_are_normalized(self):
        assert normalize_line_separators("a\r\nb") == "a\nb"
        assert normalize_line_separators("a\rb") == "a\nb"

    def test_mixed_separators_collapse_to_newlines(self):
        assert normalize_line_separators("a\u2028b\r\nc\u2029d") == "a\nb\nc\nd"

    def test_empty_and_none_are_safe(self):
        assert normalize_line_separators("") == ""
        assert normalize_line_separators(None) == ""

    def test_normal_text_is_unchanged(self):
        text = "Ol\u00e1, como vai? Tudo bem."
        assert normalize_line_separators(text) == text


class TestSendPathCallsNormalization:
    def test_on_send_message_normalizes_before_sending(self):
        """The paste handler fixes the common entry point, but text can also
        reach the field by other routes (drag-and-drop, scripts). The send
        path is the final gate and must normalize whatever it reads. Checked
        at source level because driving on_send_message whole needs a live
        wx.App and a WhatsApp session."""
        import inspect

        src = inspect.getsource(ConversationsPanel.on_send_message)
        assert "normalize_line_separators(self.message_field.GetValue())" in src, (
            "on_send_message reads the field without normalizing Unicode "
            "line/paragraph separators first"
        )


class _FakeKeyEvent:
    """The handler now works off the control that raised the event, so the
    fake has to carry one — that is what lets a single handler serve the
    message field and the attachment caption."""

    def __init__(self, target=None):
        self.skipped = False
        self._target = target

    def Skip(self):
        self.skipped = True

    def GetEventObject(self):
        return self._target


class _FakeMentionPanel:
    def IsShown(self):
        return False


class _Stub:
    _on_text_field_paste = ConversationsPanel._on_text_field_paste
    _paste_clipboard_as_attachment = ConversationsPanel._paste_clipboard_as_attachment

    def __init__(self, frame):
        self.message_field = wx.TextCtrl(
            frame, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER | wx.TE_DONTWRAP
        )
        self._caption_field = wx.TextCtrl(frame, style=wx.TE_PROCESS_ENTER)
        # No open conversation — _paste_clipboard_as_attachment() short-circuits
        # to False so these pre-existing text-paste tests keep exercising only
        # the Unicode-separator normalization path they're named for.
        self.conversation = None
        self._staged_attachments = []


class TestPasteNormalization:
    def test_paste_of_paragraph_separator_text_is_normalized(self, wx_app):
        frame = wx.Frame(None)
        try:
            stub = _Stub(frame)
            if not set_clipboard_text("A\u2029B\u2029C"):
                pytest.skip("clipboard unavailable")

            stub._on_text_field_paste(_FakeKeyEvent(stub.message_field))

            assert stub.message_field.GetValue() == "A\nB\nC"
            assert stub.message_field.GetNumberOfLines() == 3
        finally:
            frame.Destroy()

    def test_paste_of_plain_text_is_left_alone(self, wx_app):
        frame = wx.Frame(None)
        try:
            stub = _Stub(frame)
            if not set_clipboard_text("hello\nworld"):
                pytest.skip("clipboard unavailable")

            event = _FakeKeyEvent(stub.message_field)
            stub._on_text_field_paste(event)

            # Plain text is delegated to the native paste (Skip), which the
            # handler must not itself touch — the assertion is the delegation
            # itself, since no real paste event is being processed here.
            assert event.skipped
            assert stub.message_field.GetValue() == ""
        finally:
            frame.Destroy()


class TestEveryOtherFieldThatReachesWhatsApp:
    """The message field was only the first of five.

    Anything typed or pasted into these also arrives on someone else's
    WhatsApp, so a caption or a status pasted from Word reproduced the exact
    same report the message field had. Checked at source level because
    driving these whole needs a live wx.App and a WhatsApp session.
    """

    @pytest.mark.parametrize("module,method,expression", [
        ("ui.conversations", "ConversationsPanel._consume_attachment_caption",
         "normalize_line_separators"),
        ("status_panel", "StatusPanel._on_send_status_reply",
         "normalize_line_separators(self._reply_field.GetValue())"),
        ("status_panel", "StatusPanel._on_send_text_status",
         "normalize_line_separators(self._post_text_field.GetValue())"),
        ("status_panel", "StatusPanel._on_send_text_status",
         "normalize_line_separators(self._caption_field.GetValue())"),
    ])
    def test_send_path_normalizes_before_sending(self, module, method, expression):
        import importlib
        import inspect

        cls_name, attr = method.split(".")
        cls = getattr(importlib.import_module(module), cls_name)
        src = inspect.getsource(getattr(cls, attr))
        assert expression in src, (
            f"{method} reads a field that ends up on WhatsApp without "
            f"normalizing Unicode line/paragraph separators first"
        )

    def test_media_status_caption_is_normalized_too(self):
        import inspect
        from status_panel import StatusPanel

        src = inspect.getsource(StatusPanel)
        assert "normalize_line_separators(self._media_caption_field.GetValue())" in src


class TestPasteHandlerIsGeneric:
    def test_it_writes_to_the_control_that_raised_the_event(self, wx_app):
        """The generalization that lets one handler serve every field: the
        text goes to the control the paste came from, not to a hardcoded
        message_field."""
        frame = wx.Frame(None)
        try:
            stub = _Stub(frame)
            if not set_clipboard_text("A\u2029B"):
                pytest.skip("clipboard unavailable")

            stub._on_text_field_paste(_FakeKeyEvent(stub._caption_field))

            assert stub._caption_field.GetValue() == "A\nB"
            assert stub.message_field.GetValue() == "", "escreveu no campo errado"
        finally:
            frame.Destroy()

    def test_the_caption_field_is_bound_to_the_handler(self):
        """Binding is what makes the fix reach the field at paste time; the
        send path alone would fix the recipient but still show (and read
        aloud) a single run-on line while composing."""
        import inspect

        src = inspect.getsource(ConversationsPanel)
        assert "self._caption_field.Bind(wx.EVT_TEXT_PASTE, self._on_text_field_paste)" in src
