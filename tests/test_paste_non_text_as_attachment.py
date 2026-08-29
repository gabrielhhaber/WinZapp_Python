"""Tests for Ctrl+V of non-text clipboard content (files, images) inside a
conversation: skip the file picker and go straight to the attachment panel,
same shortcut the official WhatsApp client offers.
"""

import os
import tempfile
import time

import pytest
import wx

from tests.conftest import set_clipboard_data, set_clipboard_text
from ui.conversations import ConversationsPanel


_SENTINEL = object()


class _Stub:
    _paste_clipboard_as_attachment = ConversationsPanel._paste_clipboard_as_attachment
    _paste_from_messages_list = ConversationsPanel._paste_from_messages_list

    def __init__(self, conversation=_SENTINEL):
        self.conversation = (
            {"remoteJid": "jid1"} if conversation is _SENTINEL else conversation
        )
        self._staged_attachments = []
        self.panel_shown_calls = 0
        self.message_field = _FakeMessageField()

    def _show_attachment_panel(self):
        self.panel_shown_calls += 1


# The retrying writer lives in conftest so both clipboard-using test modules
# share one implementation — see conftest.set_clipboard_data's docstring.
_set_clipboard_data = set_clipboard_data


class _FakeMessageField:
    def __init__(self):
        self.value = ""
        self.focused = False

    def SetFocus(self):
        self.focused = True

    def WriteText(self, text):
        self.value += text


class _FakeMessagesList:
    def GetFocusedItem(self):
        return 0

    def GetItemCount(self):
        return 1


class _FakeMainWindow:
    settings = {}


class _FakeKeyEvent:
    def __init__(self, key, ctrl=False, shift=False):
        self._key = key
        self._ctrl = ctrl
        self._shift = shift
        self.skipped = False

    def GetKeyCode(self):
        return self._key

    def ControlDown(self):
        return self._ctrl

    def ShiftDown(self):
        return self._shift

    def Skip(self):
        self.skipped = True


class _MessagesListPasteStub(_Stub):
    _on_messages_list_key_down = ConversationsPanel._on_messages_list_key_down

    def __init__(self):
        super().__init__()
        self.messages_list = _FakeMessagesList()
        self.main_window = _FakeMainWindow()
        self._is_loading_more = False
        self._messages_offset = 0
def _set_clipboard_files(paths):
    def make():
        data = wx.FileDataObject()
        for p in paths:
            data.AddFile(p)
        return data

    return _set_clipboard_data(make)


def _set_clipboard_bitmap():
    bmp = wx.Bitmap(4, 4)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(255, 0, 0)))
    dc.Clear()
    dc.SelectObject(wx.NullBitmap)

    return _set_clipboard_data(lambda: wx.BitmapDataObject(bmp))


# Was the one helper here that still ignored SetData()'s return value, three
# lines below the one that fixed it. It backs the tests asserting that plain
# text is NOT staged as an attachment — so a stale FileDataObject left by an
# earlier test made exactly those assertions fail.
_set_clipboard_text = set_clipboard_text





class TestPasteFiles:
    def test_pasted_files_are_staged_by_extension(self, wx_app, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG\r\n")
        doc = tmp_path / "report.pdf"
        doc.write_bytes(b"%PDF")

        if not _set_clipboard_files([str(img), str(doc)]):
            pytest.skip("clipboard unavailable")

        stub = _Stub()
        if not wx.TheClipboard.Open():
            pytest.skip("clipboard unavailable")
        try:
            handled = stub._paste_clipboard_as_attachment()
        finally:
            wx.TheClipboard.Close()

        assert handled is True
        types = {a["media_type"] for a in stub._staged_attachments}
        assert {"image", "document"} <= types
        assert stub.panel_shown_calls == 1

    def test_no_open_conversation_does_nothing(self, wx_app, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"\x89PNG\r\n")
        if not _set_clipboard_files([str(f)]):
            pytest.skip("clipboard unavailable")

        stub = _Stub(conversation=None)
        if not wx.TheClipboard.Open():
            pytest.skip("clipboard unavailable")
        try:
            handled = stub._paste_clipboard_as_attachment()
        finally:
            wx.TheClipboard.Close()

        assert handled is False
        assert stub._staged_attachments == []
        assert stub.panel_shown_calls == 0


class TestPasteImage:
    def test_pasted_bitmap_is_staged_as_image(self, wx_app):
        if not _set_clipboard_bitmap():
            pytest.skip("clipboard unavailable")

        stub = _Stub()
        if not wx.TheClipboard.Open():
            pytest.skip("clipboard unavailable")
        try:
            handled = stub._paste_clipboard_as_attachment()
        finally:
            wx.TheClipboard.Close()

        assert handled is True
        assert len(stub._staged_attachments) == 1
        entry = stub._staged_attachments[0]
        assert entry["media_type"] == "image"
        assert os.path.isfile(entry["path"])
        assert stub.panel_shown_calls == 1
        os.unlink(entry["path"])


class TestPasteTextIsUnaffected:
    def test_plain_text_is_not_treated_as_attachment(self, wx_app):
        if not _set_clipboard_text("hello world"):
            pytest.skip("clipboard unavailable")

        stub = _Stub()
        if not wx.TheClipboard.Open():
            pytest.skip("clipboard unavailable")
        try:
            handled = stub._paste_clipboard_as_attachment()
        finally:
            wx.TheClipboard.Close()

        assert handled is False
        assert stub._staged_attachments == []
        assert stub.panel_shown_calls == 0


class TestPasteFromMessagesList:
    def test_ctrl_v_text_moves_focus_to_composer_and_pastes(self, wx_app):
        if not _set_clipboard_text("texto\u2029colado"):
            pytest.skip("clipboard unavailable")

        stub = _MessagesListPasteStub()
        event = _FakeKeyEvent(ord("V"), ctrl=True)
        stub._on_messages_list_key_down(event)

        assert stub.message_field.focused is True
        assert stub.message_field.value == "texto\ncolado"
        assert event.skipped is False

    def test_ctrl_v_copied_file_uses_attachment_not_text_path(self, wx_app, tmp_path):
        file_path = tmp_path / "arquivo.pdf"
        file_path.write_bytes(b"%PDF")
        if not _set_clipboard_files([str(file_path)]):
            pytest.skip("clipboard unavailable")

        stub = _MessagesListPasteStub()
        event = _FakeKeyEvent(ord("V"), ctrl=True)
        stub._on_messages_list_key_down(event)

        assert stub.panel_shown_calls == 1
        assert stub._staged_attachments == [
            {"path": str(file_path), "media_type": "document"}
        ]
        assert stub.message_field.value == ""
        assert event.skipped is False
