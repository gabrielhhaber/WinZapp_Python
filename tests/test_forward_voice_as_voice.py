"""A forwarded voice message can stay a voice message.

WhatsApp turns a forwarded voice message into a plain audio (WhatsApp Web does
it in WAWebMediaForwardMediaMsg, before sending), and WinZapp forwards through
WhatsApp's own function, so it did the same. At the destination the copy then
falls out of the sequential playback of voice messages. Settings > User
Interface has an option (off by default) that asks the server to keep the type:
POST /forward-messages gets keepVoice, and the in-page runtime makes that one
conversion a no-op (tests/test_forward_messages_lazy_resources.py). The copy is
still marked as forwarded.

This file pins the Python half: who asks, and only for what.
"""

import pytest

from core.utils import DEFAULT_SETTINGS
from main import MainWindow
from tests.god_modules import patch_main_global

SOURCE = "120363000000000001@g.us"
TARGET = "120363000000000002@g.us"


def _voice():
    return {"key": {"id": "VOICE1", "remoteJid": SOURCE, "fromMe": False,
                    "participant": "5511900000000@s.whatsapp.net"},
            "messageType": "audioMessage",
            "message": {"audioMessage": {"ptt": True, "seconds": 3}}}


def _audio_file():
    msg = _voice()
    msg["message"]["audioMessage"]["ptt"] = False
    return msg


def _text():
    return {"key": {"id": "TXT1", "remoteJid": SOURCE, "fromMe": False},
            "messageType": "conversation", "message": {"conversation": "hi"}}


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

    def __init__(self, **ui_settings):
        self.settings = {"user_interface": ui_settings}
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


def _forward(window, msg):
    return window.forward_message(SOURCE, msg["key"], TARGET, source_msg=msg)


class TestWhoAsks:
    def test_off_by_default(self, posted):
        assert DEFAULT_SETTINGS["user_interface"]["forward_voice_as_voice"] is False
        assert _forward(_Window(), _voice()) is True
        assert "keepVoice" not in posted[0]

    def test_on_a_voice_message_asks_the_server_to_keep_it(self, posted):
        assert _forward(_Window(forward_voice_as_voice=True), _voice()) is True
        assert posted[0]["keepVoice"] is True
        assert posted[0]["phone"] == [TARGET] and len(posted[0]["messageId"]) == 1

    @pytest.mark.parametrize("msg", [_audio_file(), _text()], ids=["audio file", "text"])
    def test_on_anything_else_is_forwarded_as_before(self, posted, msg):
        assert _forward(_Window(forward_voice_as_voice=True), msg) is True
        assert "keepVoice" not in posted[0]

    def test_only_an_explicit_true_turns_it_on(self, posted):
        """A hand-edited "yes" must not change how messages are sent."""
        for value in ("yes", 1, None):
            assert _Window(forward_voice_as_voice=value)._keep_voice_on_forward(_voice()) is False

    def test_a_forward_without_the_source_message_asks_for_nothing(self, posted):
        window = _Window(forward_voice_as_voice=True)
        assert window.forward_message(SOURCE, _voice()["key"], TARGET) is True
        assert "keepVoice" not in posted[0]

    def test_the_copy_still_gets_its_duration(self, posted):
        """The copy comes back over the socket with no duration (issue #43),
        voice message or not; the length put aside at the forward is grafted
        onto it. Seen live while testing this over CDP, which bypasses
        forward_message(): the copy read "voice message" with no length."""
        window = _Window(forward_voice_as_voice=True)
        window.apply_forwarded_duration = MainWindow.apply_forwarded_duration.__get__(window)
        assert _forward(window, _voice()) is True
        echo = {"key": {"id": "3EB0COPY", "remoteJid": TARGET, "fromMe": True},
                "messageType": "audioMessage",
                "message": {"audioMessage": {"ptt": True}}}

        assert window.apply_forwarded_duration(echo) is True
        assert echo["message"]["audioMessage"] == {"ptt": True, "seconds": 3}

    def test_a_window_without_settings_yet_forwards_as_before(self):
        class _Bare:
            _keep_voice_on_forward = MainWindow._keep_voice_on_forward

        assert _Bare()._keep_voice_on_forward(_voice()) is False
