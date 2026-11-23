"""Tests for copy/show-popup support on captioned photo/video/document
messages (Ctrl+C already copies the file for these types, so caption copy
got its own shortcut, Ctrl+Shift+C — see conversations.py's accelerator
table and on_messages_context_menu()).

ConversationsPanel is a wx.Panel and can't be instantiated without a
running wx.App, so the methods under test are bound onto a plain stub
carrying only the attributes they touch, matching the pattern used
throughout this test suite (see test_sender_names.py).
"""

from ui.conversations import ConversationsPanel


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.output_calls = []

    def output(self, text, interrupt=False):
        self.output_calls.append(text)


class _FakeMessagesList:
    def __init__(self, focused=0):
        self._focused = focused

    def GetFirstSelected(self):
        return self._focused


class _Stub:
    _CAPTIONABLE_TYPES      = ConversationsPanel._CAPTIONABLE_TYPES
    _get_message_caption   = ConversationsPanel._get_message_caption
    _on_menu_copy_message   = ConversationsPanel._on_menu_copy_message
    _on_menu_copy_caption   = ConversationsPanel._on_menu_copy_caption
    _on_accel_copy_caption  = ConversationsPanel._on_accel_copy_caption
    _show_message_text_popup = ConversationsPanel._show_message_text_popup
    _is_separator           = ConversationsPanel._is_separator

    def __init__(self, messages):
        self.main_window = _FakeMainWindow()
        self._sorted_messages = messages
        self.messages_list = _FakeMessagesList(focused=0)


def _image_msg(caption=""):
    return {
        "key": {"id": "m1"},
        "messageType": "imageMessage",
        "message": {"imageMessage": {"caption": caption}},
    }


def _video_msg(caption=""):
    return {
        "key": {"id": "m2"},
        "messageType": "videoMessage",
        "message": {"videoMessage": {"caption": caption}},
    }


def _document_msg(caption=""):
    return {
        "key": {"id": "m3"},
        "messageType": "documentMessage",
        "message": {"documentMessage": {"caption": caption}},
    }


def _audio_msg():
    return {
        "key": {"id": "m4"},
        "messageType": "audioMessage",
        "message": {"audioMessage": {}},
    }


class TestGetMessageCaption:
    def test_returns_caption_for_image(self):
        stub = _Stub([_image_msg("legenda da foto")])
        assert stub._get_message_caption(_image_msg("legenda da foto")) == "legenda da foto"

    def test_returns_caption_for_video(self):
        stub = _Stub([])
        assert stub._get_message_caption(_video_msg("legenda do video")) == "legenda do video"

    def test_returns_caption_for_document(self):
        stub = _Stub([])
        assert stub._get_message_caption(_document_msg("legenda do doc")) == "legenda do doc"

    def test_empty_when_no_caption(self):
        stub = _Stub([])
        assert stub._get_message_caption(_image_msg("")) == ""

    def test_empty_for_audio_message_type(self):
        # Audio/sticker messages never carry a caption in WhatsApp.
        stub = _Stub([])
        assert stub._get_message_caption(_audio_msg()) == ""

    def test_strips_whitespace(self):
        stub = _Stub([])
        assert stub._get_message_caption(_image_msg("  hi  ")) == "hi"


class TestCopyCaption:
    def test_copies_caption_to_clipboard(self, monkeypatch):
        import ui.conversations as conv_module

        copied = []
        monkeypatch.setattr(conv_module.pyperclip, "copy", lambda t: copied.append(t))

        stub = _Stub([_image_msg("legenda")])
        stub._on_menu_copy_caption(_image_msg("legenda"))

        assert copied == ["legenda"]
        assert stub.main_window.output_calls == ["msg_caption_copied"]

    def test_reports_error_when_no_caption(self, monkeypatch):
        import ui.conversations as conv_module

        monkeypatch.setattr(conv_module.pyperclip, "copy", lambda t: None)

        stub = _Stub([_image_msg("")])
        stub._on_menu_copy_caption(_image_msg(""))

        assert stub.main_window.output_calls == ["msg_copy_error"]

    def test_accel_copies_caption_of_focused_message(self, monkeypatch):
        import ui.conversations as conv_module

        copied = []
        monkeypatch.setattr(conv_module.pyperclip, "copy", lambda t: copied.append(t))

        msgs = [_document_msg("relatorio.pdf legenda")]
        stub = _Stub(msgs)
        stub._on_accel_copy_caption(None)

        assert copied == ["relatorio.pdf legenda"]

    def test_accel_does_nothing_on_separator_row(self, monkeypatch):
        import ui.conversations as conv_module

        copied = []
        monkeypatch.setattr(conv_module.pyperclip, "copy", lambda t: copied.append(t))

        stub = _Stub([{"_type": "unread_separator"}])
        stub._on_accel_copy_caption(None)

        assert copied == []
        assert stub.main_window.output_calls == []


class TestShowTextPopupFallsBackToCaption:
    def test_no_op_when_media_message_has_no_caption(self, monkeypatch):
        # _show_message_text_popup returns early (before building any wx
        # widgets) when there's no text/caption — safe to call headlessly.
        stub = _Stub([])
        stub._show_message_text_popup(_video_msg(""))
        # No exception, no wx widgets created — nothing else to assert
        # without a running wx.App.
