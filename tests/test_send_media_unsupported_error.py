"""Tests for MainWindow.send_media_attachment()'s handling of
MediaUnsupportedError — WhatsApp Web's own upload pipeline rejecting a
specific file as malformed/unprocessable (observed live as "video loaded
with duration but no dims" for one particular video whose container
metadata didn't carry proper track dimensions).

Reported live: sending that file retried 4 times (identical failure every
attempt — the same bytes, the same rejection) before finally showing a
generic "Erro ao enviar a mensagem" with no indication anything was wrong
with the file itself. Other, unrelated 5xx failures (e.g. WPPConnect's
"ProtocolError: Promise was collected" under load) genuinely are transient
and must keep retrying — only this specific, permanently-failing case
should stop early with a clear reason.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so send_media_attachment() is exercised as a plain function against a small
stub carrying just the attributes it touches, with requests.post monkeypatched
— same approach as tests/test_send_jid_resolution.py.
"""

import os

import pytest

from main import MainWindow


class _FakeI18n:
    def t(self, key):
        return {
            "audio_convert_failed": "Failed to convert audio.",
            "media_audio_convert_failed": "Could not convert audio for WhatsApp.",
            "media_unsupported_error": "the file appears to be corrupted or in a format WhatsApp cannot process",
        }.get(key, key)


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _Stub:
    send_media_attachment = MainWindow.send_media_attachment
    _check_wa_connection_closed = MainWindow._check_wa_connection_closed
    # video sends now probe for ffmpeg (see core/video_transcode.py) even
    # for an already-compatible .mp4, which short-circuits before actually
    # needing a working binary — real staticmethod is fine to reuse as-is.
    _find_api_ffmpeg = staticmethod(MainWindow._find_api_ffmpeg)

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "session:key"
        self.i18n = _FakeI18n()
        self._wa_connected = True

    def _resolve_jid_for_send(self, jid):
        return jid

    def _legacy_phone_for_send(self, jid):
        return ""

    def _serialize_quoted_id(self, quoted, fallback_jid=""):
        return ""


@pytest.fixture
def media_file(tmp_path):
    p = tmp_path / "video.mp4"
    p.write_bytes(b"fake video bytes")
    return str(p)


class TestMediaUnsupportedErrorStopsRetrying:
    def test_negative_ack_is_failure_even_when_upload_returned_http_201(
        self, media_file, monkeypatch
    ):
        import main

        monkeypatch.setattr(
            main.requests,
            "post",
            lambda *a, **k: _FakeResponse(
                201,
                {
                    "response": {
                        "ack": -1,
                        "id": "true_5511999999999@c.us_REJECTED123",
                    }
                },
            ),
        )

        result = _Stub().send_media_attachment(
            "5511999999999@s.whatsapp.net", media_file, "video"
        )

        assert result == {
            "ok": False,
            "error": "the file appears to be corrupted or in a format WhatsApp cannot process",
            "retry": False,
        }

    def test_the_refusal_body_the_node_side_really_sends_reaches_the_same_message(
        self, media_file, monkeypatch
    ):
        """The body above is not one auditSendResult() can produce.

        describeSendRejection() (messageController.ts) checks the embedded
        error before the ACK and annotates the result with
        "send-file was rejected (ack=-1)", so a real refusal always carries
        both. accepted_message_id() used to read that error first and report
        reason="unconfirmed", which told the user to go and check a
        conversation the file never reached.
        """
        import main

        monkeypatch.setattr(
            main.requests,
            "post",
            lambda *a, **k: _FakeResponse(
                201,
                {
                    "status": "success",
                    "response": [
                        {
                            "id": "true_5511999999999@c.us_REJECTED123",
                            "ack": -1,
                            "error": "send-file was rejected (ack=-1)",
                        }
                    ],
                },
            ),
        )

        result = _Stub().send_media_attachment(
            "5511999999999@s.whatsapp.net", media_file, "video"
        )

        assert result == {
            "ok": False,
            "error": "the file appears to be corrupted or in a format WhatsApp cannot process",
            "retry": False,
        }

    def test_media_unsupported_error_is_not_retried_and_has_a_clear_message(self, media_file, monkeypatch):
        import main

        body = {
            "status": "Error",
            "message": "Erro ao enviar a mensagem.",
            "error": {
                "name": "MediaUnsupportedError",
                "level": "error",
                "message": "video loaded with duration but no dims",
            },
        }
        monkeypatch.setattr(
            main.requests, "post",
            lambda *a, **k: _FakeResponse(500, body),
        )

        result = _Stub().send_media_attachment("5511999999999@s.whatsapp.net", media_file, "video")

        assert result["ok"] is False
        assert result["retry"] is False
        assert result["error"] == "the file appears to be corrupted or in a format WhatsApp cannot process"

    def test_an_unrelated_5xx_still_retries(self, media_file, monkeypatch):
        """Sanity check the fix is scoped to MediaUnsupportedError — a
        generic transient 5xx (e.g. Puppeteer's own "Promise was collected"
        under load) must keep its existing retry behavior."""
        import main

        body = {"status": "Error", "message": "Erro ao enviar a mensagem.", "error": "ProtocolError: Promise was collected"}
        monkeypatch.setattr(
            main.requests, "post",
            lambda *a, **k: _FakeResponse(500, body),
        )

        result = _Stub().send_media_attachment("5511999999999@s.whatsapp.net", media_file, "video")

        assert result["ok"] is False
        assert result["retry"] is True

    def test_a_4xx_still_treated_as_non_retryable_as_before(self, media_file, monkeypatch):
        import main

        body = {"status": "Error", "message": "Bad request"}
        monkeypatch.setattr(
            main.requests, "post",
            lambda *a, **k: _FakeResponse(400, body),
        )

        result = _Stub().send_media_attachment("5511999999999@s.whatsapp.net", media_file, "video")

        assert result["ok"] is False
        assert result["retry"] is False


class TestConvertedAudioLifecycle:
    def test_successful_upload_removes_only_the_converted_temporary_file(
        self, tmp_path, monkeypatch
    ):
        import main
        import core.audio_transcode as audio_transcode

        source = tmp_path / "vorbis.ogg"
        source_bytes = b"OggS" + b"original vorbis audio"
        source.write_bytes(source_bytes)
        converted = tmp_path / "vorbis.ogg.opus.ogg"
        converted.write_bytes(b"OggS" + b"OpusHead" + b"converted audio")

        monkeypatch.setattr(
            audio_transcode,
            "prepare_audio_for_whatsapp",
            lambda ffmpeg, path: (str(converted), "audio/ogg; codecs=opus"),
        )
        monkeypatch.setattr(
            main.requests,
            "post",
            lambda *args, **kwargs: _FakeResponse(
                201, {"response": {"id": "true_5511999999999@c.us_AUDIO123"}}
            ),
        )

        result = _Stub().send_media_attachment(
            "5511999999999@s.whatsapp.net", str(source), "audio"
        )

        assert result == "AUDIO123"
        assert source.read_bytes() == source_bytes
        assert not converted.exists()

    def test_conversion_failure_uses_the_localized_error_and_never_uploads(
        self, tmp_path, monkeypatch
    ):
        import main
        import core.audio_transcode as audio_transcode

        source = tmp_path / "invalid.ogg"
        source.write_bytes(b"OggS")
        monkeypatch.setattr(
            audio_transcode,
            "prepare_audio_for_whatsapp",
            lambda ffmpeg, path: None,
        )
        monkeypatch.setattr(
            main.requests,
            "post",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("a failed conversion must not be uploaded")
            ),
        )

        result = _Stub().send_media_attachment(
            "5511999999999@s.whatsapp.net", str(source), "audio"
        )

        assert result == {
            "ok": False,
            "error": "Could not convert audio for WhatsApp.",
            "retry": False,
        }
