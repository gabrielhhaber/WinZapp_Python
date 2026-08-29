"""Tests for MainWindow.mark_audio_message_played()/_send_mark_played_request().

Feature: playing a received voice message in-app to the end (the same
moment ConversationsPanel.on_audio_timer() hides the playback controls)
must mark it as played — locally (the status icon in the message list,
same as a real "played" receipt arriving over the WebSocket) and for real,
via a POST to WPPConnect's new /mark-played route (client/api_patches/src/
controller/messageController.ts — no equivalent existed anywhere in
WPPConnect Server before this).

Must never apply to a message we sent ourselves: "played" only ever
legitimately reflects the RECIPIENT's own playback, which for our own
sends already arrives the normal way via on_message_status_update().

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so the methods under test are exercised as plain functions against
a small stub — same approach as tests/test_serialize_msg_id.py.
"""

import pytest

from main import MainWindow


class _Stub:
    mark_audio_message_played = MainWindow.mark_audio_message_played
    _send_mark_played_request = MainWindow._send_mark_played_request
    _serialize_msg_id         = MainWindow._serialize_msg_id

    def __init__(self):
        # Configuracoes > Reproducao de audio > "Mudar status dos audios para
        # reproduzidos...". These tests exercise the default (on).
        self.settings = {"audio_playback": {"mark_audio_played_in_list": True}}
        self._phone_to_lid = {}
        self.my_jid = ""
        self.my_lid = ""
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "session:key"
        self.status_update_calls = []
        self.skip_panel_refresh_calls = []
        self.conversations_panel = type("P", (), {"conversation": None})()

    def on_message_status_update(self, update, skip_panel_refresh=False):
        self.status_update_calls.append(update)
        self.skip_panel_refresh_calls.append(skip_panel_refresh)


def _received_audio_msg(msg_id="ABC123", remote_jid="5521999999999@s.whatsapp.net"):
    return {
        "key": {"id": msg_id, "fromMe": False, "remoteJid": remote_jid},
        "messageType": "audioMessage",
        "message": {"audioMessage": {}},
    }


def _own_audio_msg(msg_id="OWN1", remote_jid="5521999999999@s.whatsapp.net"):
    return {
        "key": {"id": msg_id, "fromMe": True, "remoteJid": remote_jid},
        "messageType": "audioMessage",
        "message": {"audioMessage": {}},
    }


class TestMarkAudioMessagePlayed:
    def test_received_message_marks_locally_and_sends_receipt(self, monkeypatch):
        stub = _Stub()
        sent = []
        monkeypatch.setattr(
            stub, "_send_mark_played_request",
            lambda remote_jid, msg_key: sent.append((remote_jid, msg_key)),
        )

        stub.mark_audio_message_played(_received_audio_msg())

        assert stub.status_update_calls == [
            {"key": {"id": "ABC123", "remoteJid": "5521999999999@s.whatsapp.net"}, "status": "5"}
        ]
        assert sent == [("5521999999999@s.whatsapp.net",
                          {"id": "ABC123", "fromMe": False, "remoteJid": "5521999999999@s.whatsapp.net"})]

    def test_own_sent_message_is_never_marked(self, monkeypatch):
        """"Played" for our own send only ever legitimately arrives via a
        real messages.update event — marking it here would be us reporting
        our own playback of our own copy as if the recipient had listened."""
        stub = _Stub()
        monkeypatch.setattr(
            stub, "_send_mark_played_request",
            lambda *a, **k: pytest.fail("must not send a played receipt for our own message"),
        )

        stub.mark_audio_message_played(_own_audio_msg())

        assert stub.status_update_calls == []

    def test_missing_remote_jid_falls_back_to_the_open_conversation(self, monkeypatch):
        stub = _Stub()
        stub.conversations_panel.conversation = {"remoteJid": "5521888888888@s.whatsapp.net"}
        msg = _received_audio_msg(remote_jid="")
        sent = []
        monkeypatch.setattr(
            stub, "_send_mark_played_request",
            lambda remote_jid, msg_key: sent.append(remote_jid),
        )

        stub.mark_audio_message_played(msg)

        assert stub.status_update_calls[0]["key"]["remoteJid"] == "5521888888888@s.whatsapp.net"
        assert sent == ["5521888888888@s.whatsapp.net"]

    def test_skip_panel_refresh_is_passed_through_to_the_status_update(self, monkeypatch):
        """Set by the caller (ConversationsPanel.on_audio_timer()) whenever
        this voice note is about to auto-chain into the next one — see
        on_message_status_update()'s own docstring for why the row refresh
        must not fire here in that case (the caller fires it itself once
        it's safe to)."""
        stub = _Stub()
        monkeypatch.setattr(stub, "_send_mark_played_request", lambda *a, **k: None)

        stub.mark_audio_message_played(_received_audio_msg(), skip_panel_refresh=True)

        assert stub.skip_panel_refresh_calls == [True]

    def test_no_message_id_is_a_no_op(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(
            stub, "_send_mark_played_request",
            lambda *a, **k: pytest.fail("must not be called without a message id"),
        )

        stub.mark_audio_message_played(_received_audio_msg(msg_id=""))

        assert stub.status_update_calls == []


class TestSendMarkPlayedRequest:
    def test_posts_the_serialized_message_id(self, monkeypatch):
        stub = _Stub()
        posted = {}

        class _FakeResponse:
            status_code = 200
            text = "{}"

        def fake_post(url, json=None, headers=None, timeout=None):
            posted["url"] = url
            posted["json"] = json
            return _FakeResponse()

        import main
        monkeypatch.setattr(main.requests, "post", fake_post)

        stub._send_mark_played_request(
            "5521999999999@s.whatsapp.net",
            {"id": "ABC123", "fromMe": False, "remoteJid": "5521999999999@s.whatsapp.net"},
        )

        assert posted["url"].endswith("/mark-played")
        assert posted["json"]["messageId"] == "false_5521999999999@c.us_ABC123"

    def test_a_failed_request_is_logged_not_raised(self, monkeypatch):
        stub = _Stub()

        import main

        def boom(*a, **k):
            raise ConnectionError("offline")

        monkeypatch.setattr(main.requests, "post", boom)

        stub._send_mark_played_request(
            "5521999999999@s.whatsapp.net",
            {"id": "ABC123", "fromMe": False, "remoteJid": "5521999999999@s.whatsapp.net"},
        )  # must not raise
