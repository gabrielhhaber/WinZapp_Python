"""An attached audio file must arrive as the audio the user picked — every
channel, the same sample rate — and never as a voice note.

Reported live: a 5.1 WAV (6 channels, 44.1 kHz) sent through Attach arrived
as a mono, voice-note-like clip. prepare_audio_for_whatsapp() re-encoded every
.wav/.wave and Vorbis .ogg with ``-ac 1 -c:a libopus -b:a 64k`` into
audio/ogg; codecs=opus — the voice-message format — throwing away five of the
six channels. It now encodes AAC-LC in M4A (audio/mp4) with no ``-ac``, a
bitrate scaled per channel and the source sample rate kept, and
send_media_attachment() tells /send-file ``isPtt: false`` outright. Voice
recordings use /send-voice-base64 and are not touched by any of this.
"""

import os
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.audio_transcode import (
    aac_encode_args,
    exceeds_aac_channel_limit,
    prepare_audio_for_whatsapp,
    read_audio_header_format,
)
from core import audio_transcode
from main import MainWindow

ROOT = Path(__file__).resolve().parents[1]


def _wav_header(channels, sample_rate, extra_chunk=b""):
    """A WAVE_FORMAT_EXTENSIBLE header like the reported 5.1 file's."""
    fmt = struct.pack("<HHIIHH", 0xFFFE, channels, sample_rate,
                      sample_rate * channels * 2, channels * 2, 16)
    fmt += b"\x16\x00" + b"\x10\x00" + struct.pack("<I", 0x3F) + b"\x01\x00" + b"\x00" * 14
    return (b"RIFF" + b"\x00" * 4 + b"WAVE" + extra_chunk
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", 4) + b"\x00" * 4)


def _vorbis_header(channels, sample_rate):
    ident = b"\x01vorbis" + struct.pack("<IBI", 0, channels, sample_rate) + b"\x00" * 14
    return b"OggS" + b"\x00" * 23 + ident


class TestReadAudioHeaderFormat:
    def test_reads_a_51_extensible_wav(self):
        assert read_audio_header_format(_wav_header(6, 44100)) == (6, 44100)

    def test_skips_chunks_before_fmt(self):
        junk = b"JUNK" + struct.pack("<I", 27) + b"\x00" * 28  # odd size → pad byte
        assert read_audio_header_format(_wav_header(2, 48000, junk)) == (2, 48000)

    def test_reads_rf64_and_bw64_wavs(self):
        assert read_audio_header_format(b"RF64" + _wav_header(6, 48000)[4:]) == (6, 48000)
        assert read_audio_header_format(b"BW64" + _wav_header(6, 48000)[4:]) == (6, 48000)

    def test_reads_a_vorbis_identification_header(self):
        assert read_audio_header_format(_vorbis_header(6, 48000)) == (6, 48000)

    def test_unknown_or_truncated_input_is_none(self):
        assert read_audio_header_format(b"not audio at all") is None
        assert read_audio_header_format(_wav_header(6, 44100)[:24]) is None


class TestAacEncodeArgs:
    @pytest.mark.parametrize("channels,bitrate", [(1, "96k"), (2, "192k"), (6, "576k"), (8, "768k")])
    def test_never_downmixes_and_scales_bitrate_per_channel(self, channels, bitrate):
        args = aac_encode_args(channels, 44100)
        assert "-ac" not in args
        assert args[args.index("-c:a") + 1] == "aac"
        assert args[args.index("-profile:a") + 1] == "aac_low"
        assert args[args.index("-b:a") + 1] == bitrate

    @pytest.mark.parametrize("source,target", [
        (44100, "44100"), (48000, "48000"),
        # Not an AAC rate: go up, never down to 32000 as ffmpeg would.
        (37800, "44100"),
        # Above 48 kHz, down to 48 kHz: phones only have to decode AAC-LC up to it.
        (88200, "48000"), (96000, "48000"), (192000, "48000"),
    ])
    def test_keeps_the_sample_rate_or_raises_it_to_the_next_aac_rate(self, source, target):
        args = aac_encode_args(2, source)
        assert args[args.index("-ar") + 1] == target

    def test_unreadable_header_still_keeps_channels_and_asks_high(self):
        args = aac_encode_args(None, None)
        assert "-ac" not in args and "-ar" not in args
        assert args[args.index("-b:a") + 1] == "768k"


class TestPrepareAudioForWhatsapp:
    @pytest.mark.parametrize("name,header", [
        ("5.1 Test.wav", _wav_header(6, 44100)),
        ("surround.ogg", _vorbis_header(6, 44100)),
    ])
    def test_surround_source_is_encoded_as_six_channel_aac_m4a(
        self, tmp_path, monkeypatch, name, header
    ):
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_bytes(b"binary")
        source = tmp_path / name
        source.write_bytes(header)
        seen = []

        def fake_run(command, **kwargs):
            seen.extend(command)
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x20ftypM4A converted")
            return SimpleNamespace(returncode=0, stderr=b"")

        monkeypatch.setattr("core.audio_transcode.subprocess.run", fake_run)

        output, mime = prepare_audio_for_whatsapp(str(ffmpeg), str(source))
        try:
            assert mime == "audio/mp4"
            assert output.endswith(".m4a")
            assert "-ac" not in seen
            assert "libopus" not in seen
            assert seen[seen.index("-b:a") + 1] == "576k"
            assert seen[seen.index("-ar") + 1] == "44100"
            assert source.read_bytes() == header
        finally:
            os.unlink(output)

    def test_failed_encode_removes_the_partial_output(self, tmp_path, monkeypatch):
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_bytes(b"binary")
        source = tmp_path / "surround.wav"
        source.write_bytes(_wav_header(6, 44100))
        outputs = []

        def fake_run(command, **kwargs):
            outputs.append(Path(command[-1]))
            outputs[-1].write_bytes(b"partial")
            return SimpleNamespace(returncode=1, stderr=b"encoder failed")

        monkeypatch.setattr("core.audio_transcode.subprocess.run", fake_run)

        assert prepare_audio_for_whatsapp(str(ffmpeg), str(source)) is None
        assert not outputs[0].exists()


class _FakeResponse:
    status_code = 201
    text = ""

    def json(self):
        return {"response": {"id": "true_5511999999999@c.us_AUDIO123"}}


class _Stub:
    send_media_attachment = MainWindow.send_media_attachment
    _find_api_ffmpeg = staticmethod(MainWindow._find_api_ffmpeg)

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "session:key"
        self.i18n = SimpleNamespace(t=lambda key: key)
        self._wa_connected = True

    def _resolve_jid_for_send(self, jid):
        return jid


class TestSendFileIsPtt:
    def _send(self, tmp_path, monkeypatch, media_type, name):
        import main

        source = tmp_path / name
        source.write_bytes(b"\x00\x00\x00\x20ftypM4A audio")
        bodies = []

        def fake_post(url, **kwargs):
            bodies.append(kwargs["data"])
            return _FakeResponse()

        monkeypatch.setattr(main.requests, "post", fake_post)
        assert _Stub().send_media_attachment(
            "5511999999999@s.whatsapp.net", str(source), media_type
        ) == "AUDIO123"
        return bodies[0]

    def test_an_audio_attachment_is_sent_with_isptt_false_as_audio_mp4(self, tmp_path, monkeypatch):
        body = self._send(tmp_path, monkeypatch, "audio", "music.m4a")
        assert b'name="isPtt"\r\n\r\nfalse\r\n' in body._prefix
        assert body.mime_type == "audio/mp4"

    def test_a_document_carries_no_isptt_field(self, tmp_path, monkeypatch):
        body = self._send(tmp_path, monkeypatch, "document", "music.m4a")
        assert b'name="isPtt"' not in body._prefix

    def test_sendfile_parses_the_multipart_string_instead_of_trusting_it(self):
        source = (ROOT / "client/api_patches/src/controller/messageController.ts").read_text(
            encoding="utf-8")
        send_file = source[source.index("export async function sendFile"):
                           source.index("export async function sendVoice(")]
        assert "isPtt === true || isPtt === 'true'" in send_file
        assert "...pttOption," in send_file


class TestMoreChannelsThanAac:
    """AAC stops at 8 channels (ffmpeg exits 234 beyond). Such a file is not
    downmixed: it goes untouched, as a document. The switch happens where the
    attachment is staged, so the pending row is a documentMessage and the
    document echo binds to it by type."""

    @pytest.mark.parametrize("name,header,expected", [
        ("nine.wav", _wav_header(9, 48000), True),
        # A 7.1.4 Atmos bed in a broadcast (BW64) WAV.
        ("atmos.wav", b"BW64" + _wav_header(12, 48000)[4:], True),
        ("sixteen.ogg", _vorbis_header(16, 48000), True),
        ("eight.wav", _wav_header(8, 48000), False),
        ("surround.wav", _wav_header(6, 44100), False),
        ("unreadable.wav", b"RIFF junk", False),
        # Opus is never re-encoded, so it never needs the fallback.
        ("opus.ogg", b"OggS" + b"\x00" * 24 + b"OpusHead" + bytes([1, 9]), False),
        ("music.mp3", _wav_header(9, 48000), False),
    ])
    def test_exceeds_aac_channel_limit(self, tmp_path, name, header, expected):
        path = tmp_path / name
        path.write_bytes(header)
        assert exceeds_aac_channel_limit(str(path)) is expected

    def test_a_nine_channel_wav_is_sent_as_the_original_document(self, tmp_path, monkeypatch):
        import mimetypes

        import ui.conversations as conversations_module
        from core.i18n import I18n
        from tests.test_sent_document_file_size import _SendStub

        class _InlineThread:
            def __init__(self, target=None, daemon=None, args=(), kwargs=None):
                self._target, self._args, self._kwargs = target, args, kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        monkeypatch.setattr(conversations_module.threading, "Thread", _InlineThread)
        header = _wav_header(9, 48000)
        path = tmp_path / "nine channels.wav"
        path.write_bytes(header)
        stub = _SendStub([{"path": str(path), "media_type": "audio"}], I18n("pt-BR"))

        stub._on_send_attachment()

        pending = stub._sorted_messages[0]
        assert pending["messageType"] == "documentMessage"
        body = pending["message"]["documentMessage"]
        assert body["fileName"] == "nine channels.wav"
        assert body["mimetype"] == mimetypes.guess_type(str(path))[0]
        queued = stub.enqueued[0]
        assert queued.media_type == "document"
        assert queued.media_path == str(path)
        assert path.read_bytes() == header

    def test_a_document_is_uploaded_byte_for_byte_with_no_conversion(self, tmp_path, monkeypatch):
        """send_media_attachment() only converts media_type 'audio'; a document
        is the original file, its own name and MIME type, and no isPtt."""
        import main
        import core.audio_transcode as audio_transcode

        path = tmp_path / "nine channels.wav"
        path.write_bytes(_wav_header(9, 48000))
        monkeypatch.setattr(
            audio_transcode, "prepare_audio_for_whatsapp",
            lambda *a: (_ for _ in ()).throw(AssertionError("a document is never converted")),
        )
        bodies = []
        monkeypatch.setattr(
            main.requests, "post",
            lambda url, **kwargs: bodies.append(kwargs["data"]) or _FakeResponse(),
        )

        assert _Stub().send_media_attachment(
            "5511999999999@s.whatsapp.net", str(path), "document"
        ) == "AUDIO123"
        body = bodies[0]
        assert body.file_path == str(path)
        assert body.filename == "nine channels.wav"
        assert body.mime_type.startswith("audio/")
        assert b'name="type"\r\n\r\ndocument\r\n' in body._prefix
        assert b'name="isPtt"' not in body._prefix


def _opus_header(channels):
    return (b"OggS" + b"\x00" * 24 + b"OpusHead" + bytes([1, channels]) + b"\x38\x01"
            + (48000).to_bytes(4, "little") + b"\x00\x00\x00")


class TestOpusOnIphone:
    """iPhone cannot play stereo Opus at any bitrate; measured 2026-10-30."""

    def test_the_header_gives_channels_and_rate(self):
        assert audio_transcode.read_audio_header_format(_opus_header(2)) == (2, 48000)

    @pytest.mark.parametrize("name", ["voice.ogg", "voice.opus"])
    def test_mono_opus_stays_untouched(self, tmp_path, name):
        source = tmp_path / name
        source.write_bytes(_opus_header(1))

        assert audio_transcode.prepare_audio_for_whatsapp(None, str(source)) == (
            str(source), "audio/ogg; codecs=opus")

    @pytest.mark.parametrize("name", ["music.ogg", "music.opus"])
    def test_stereo_opus_becomes_aac_keeping_both_channels(self, tmp_path, monkeypatch, name):
        source = tmp_path / name
        source.write_bytes(_opus_header(2))
        calls = []

        def _run(args, **_kw):
            calls.append(args)
            with open(args[-1], "wb") as output:
                output.write(b"m4a")
            return SimpleNamespace(returncode=0, stderr=b"")
        monkeypatch.setattr(audio_transcode.subprocess, "run", _run)

        path, mime = audio_transcode.prepare_audio_for_whatsapp(__file__, str(source))

        assert path.endswith(".m4a") and mime == "audio/mp4"
        assert "-ac" not in calls[0]
        assert "192k" in calls[0]
        os.unlink(path)
