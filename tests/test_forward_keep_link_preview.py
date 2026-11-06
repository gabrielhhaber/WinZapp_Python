"""Forwarding a link keeps its preview card when the user asks for it.

WhatsApp Web's own forward of a text message drops the card, so a forwarded
link arrived without the preview the original had. The forward dialog has a
box for it (on by default, Settings > Interface > Messages sets how it
starts) and the forward request carries the preview when the box is ticked.
This file pins the Python half and the shape of the two servers' half; the
Neonize side is in neonize_bridge/test_forwarding.py.
"""

import json
from pathlib import Path

import pytest

from core.link_preview import message_link_preview
from core.utils import DEFAULT_SETTINGS
from main import MainWindow
from tests.god_modules import patch_main_global

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "120363000000000001@g.us"
TARGET = "120363000000000002@g.us"


def _link(**ext):
    body = {"text": "https://example.com/a", "title": "Example", "description": "A page",
            "canonicalUrl": "https://example.com/a"}
    body.update(ext)
    return {"key": {"id": "LNK1", "remoteJid": SOURCE, "fromMe": False},
            "messageType": "extendedTextMessage", "message": {"extendedTextMessage": body}}


def _plain():
    return {"key": {"id": "TXT1", "remoteJid": SOURCE, "fromMe": False},
            "messageType": "conversation", "message": {"conversation": "hi"}}


class TestMessageLinkPreview:
    def test_a_message_with_a_card_gives_it_back(self):
        assert message_link_preview(_link()) == {
            "title": "Example", "description": "A page",
            "canonicalUrl": "https://example.com/a",
            "matchedText": "https://example.com/a"}

    def test_the_address_falls_back_to_matched_text(self):
        msg = _link(canonicalUrl="", matchedText="https://example.com/b")
        assert message_link_preview(msg)["canonicalUrl"] == "https://example.com/b"

    def test_the_stored_message_may_be_a_json_string(self):
        msg = _link()
        msg["message"] = json.dumps(msg["message"])
        assert message_link_preview(msg)["title"] == "Example"

    @pytest.mark.parametrize("msg", [
        _plain(), {}, None, "x",
        {"message": "not json"},
        {"message": {"extendedTextMessage": {"text": "just a reply"}}},
        {"message": {"extendedTextMessage": {"title": "  ", "description": ""}}},
    ])
    def test_no_card_gives_nothing(self, msg):
        assert message_link_preview(msg) == {}


class _Response:
    status_code = 201
    text = ""


class _Window:
    forward_message = MainWindow.forward_message
    _keep_voice_on_forward = MainWindow._keep_voice_on_forward
    _serialize_msg_id = MainWindow._serialize_msg_id
    _expect_forwarded_duration = MainWindow._expect_forwarded_duration
    _forget_forwarded_duration = MainWindow._forget_forwarded_duration
    media_kind_of = staticmethod(MainWindow.media_kind_of)
    media_duration_of = staticmethod(MainWindow.media_duration_of)
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _MAX_FORWARDED_DURATIONS = MainWindow._MAX_FORWARDED_DURATIONS

    def __init__(self):
        self.settings = {"user_interface": {}}
        self.wpp_server, self.wpp_port, self.token = "http://127.0.0.1", 6300, "sess"
        self._phone_to_lid = {}


@pytest.fixture
def posted(monkeypatch):
    payloads = []

    def _post(url, json=None, headers=None, timeout=None):
        payloads.append(json)
        return _Response()

    patch_main_global(monkeypatch, "api_post", _post)
    return payloads


class TestForwardRequest:
    def test_a_ticked_box_sends_the_card(self, posted):
        msg = _link()
        assert _Window().forward_message(SOURCE, msg["key"], TARGET, source_msg=msg,
                                         keep_link_preview=True) is True
        assert posted[0]["linkPreview"]["title"] == "Example"

    def test_an_unticked_box_sends_what_it_always_sent(self, posted):
        msg = _link()
        _Window().forward_message(SOURCE, msg["key"], TARGET, source_msg=msg)
        assert "linkPreview" not in posted[0]

    def test_a_message_without_a_card_has_nothing_to_keep(self, posted):
        msg = _plain()
        _Window().forward_message(SOURCE, msg["key"], TARGET, source_msg=msg,
                                  keep_link_preview=True)
        assert "linkPreview" not in posted[0]


class TestSetting:
    def test_it_starts_on(self):
        assert DEFAULT_SETTINGS["user_interface"]["forward_keep_link_preview"] is True
        shipped = json.loads((ROOT / "client/data/settings_default.json").read_text("utf-8"))
        assert shipped["user_interface"]["forward_keep_link_preview"] is True

    def test_the_dialog_box_starts_as_the_setting_says(self):
        from ui.conversation_panel.forwarding import ForwardingMixin

        class _Panel:
            _forward_keep_link_preview_default = ForwardingMixin._forward_keep_link_preview_default

            def __init__(self, ui):
                self.main_window = type("W", (), {"settings": {"user_interface": ui}})()

        assert _Panel({})._forward_keep_link_preview_default() is True
        assert _Panel({"forward_keep_link_preview": False})._forward_keep_link_preview_default() is False
        assert _Panel({"forward_keep_link_preview": True})._forward_keep_link_preview_default() is True


class TestServerContract:
    """The page half cannot run here; its request shape is what Python relies on."""

    def test_the_wppconnect_handler_passes_only_an_object(self):
        handler = (ROOT / "client/api_patches/src/controller/deviceController.ts").read_text("utf-8")
        assert "const { phone, messageId, keepVoice, linkPreview } = req.body;" in handler
        assert "typeof linkPreview === 'object' && !Array.isArray(linkPreview)" in handler

    def test_the_page_sends_the_text_with_the_card_as_an_object(self):
        runtime = (ROOT / "client/api_patches/src/util/forwardRuntime.ts").read_text("utf-8")
        assert "WPP.chat.sendTextMessage(chatId, body, {" in runtime
        assert "linkPreview: preview," in runtime
        assert "msg.type === 'chat'" in runtime
        # not a forward of its own: a retry-safe key keeps a card send apart
        assert "'|preview'" in runtime
        assert "`" not in runtime.split("String.raw`")[1].split("`;")[0]

    def test_the_bridge_reads_the_same_field(self):
        server = (ROOT / "neonize_bridge/server.py").read_text("utf-8")
        assert 'isinstance(body.get("linkPreview"), dict)' in server
