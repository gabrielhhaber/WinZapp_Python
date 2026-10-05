"""send_media_attachment()'s own refusals reach the user verbatim — through
_on_message_failed()'s media_send_failed dialog ("... Error: {error}") — so
they go through i18n like every other user-facing string.

Before: the size-limit refusal was a hardcoded English sentence (the
post-conversion one even said "1 GB" for a 2 GB document), and the video
conversion failure was hardcoded Portuguese, despite media_video_convert_failed
existing in every locale file, unused.

Same stub approach as tests/test_send_media_unsupported_error.py.
"""

import os

from main import MainWindow


class _FakeI18n:
    _STRINGS = {
        "media_exceeds_whatsapp_limit": "TOO BIG {size_mb}/{limit_gb}",
        "media_video_convert_failed": "VIDEO CONVERT FAILED",
        "media_audio_convert_failed": "AUDIO CONVERT FAILED",
    }

    def t(self, key):
        return self._STRINGS.get(key, key)


class _Stub:
    send_media_attachment = MainWindow.send_media_attachment

    def __init__(self):
        self.i18n = _FakeI18n()
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "session:key"

    def _resolve_jid_for_send(self, jid):
        return jid

    def _find_api_ffmpeg(self):
        return "ffmpeg.exe"

    def _serialize_quoted_id(self, quoted, fallback_jid=""):
        return ""


_GB = 1024 ** 3


def _fake_sizes(monkeypatch, sizes):
    real = os.path.getsize
    monkeypatch.setattr(os.path, "getsize", lambda p: sizes.get(p, real(p)))


def test_oversized_document_reports_translated_limit(tmp_path, monkeypatch):
    f = tmp_path / "big.pdf"
    f.write_bytes(b"x")
    _fake_sizes(monkeypatch, {str(f): 3 * _GB})

    result = _Stub().send_media_attachment("1@s.whatsapp.net", str(f), "document")

    assert result == {"ok": False, "error": "TOO BIG 3072.0/2", "retry": False}


def test_oversized_after_audio_conversion_reports_translated_limit(tmp_path, monkeypatch):
    src = tmp_path / "in.wav"
    src.write_bytes(b"x")
    converted = tmp_path / "out.ogg"
    converted.write_bytes(b"y")
    _fake_sizes(monkeypatch, {str(src): 10, str(converted): 2 * _GB})
    monkeypatch.setattr(
        "core.audio_transcode.prepare_audio_for_whatsapp",
        lambda ffmpeg, path: (str(converted), "audio/ogg"),
    )

    result = _Stub().send_media_attachment("1@s.whatsapp.net", str(src), "audio")

    assert result == {"ok": False, "error": "TOO BIG 2048.0/1", "retry": False}
    assert not converted.exists()  # the converted temp file is still cleaned up


def test_video_conversion_failure_uses_the_locale_key(tmp_path, monkeypatch):
    f = tmp_path / "clip.mkv"
    f.write_bytes(b"x")
    monkeypatch.setattr(
        "core.video_transcode.prepare_video_for_whatsapp", lambda ffmpeg, path: None)

    result = _Stub().send_media_attachment("1@s.whatsapp.net", str(f), "video")

    assert result == {"ok": False, "error": "VIDEO CONVERT FAILED", "retry": False}
