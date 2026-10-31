"""Stereo voice messages (issue #82): one record button, no mode.

What the input device delivers decides: two channels send a stereo voice
message, anything else a mono one. The channels are never compared. iPhone
cannot play stereo OGG/Opus at any bitrate but plays stereo AAC-LC M4A, also as
a voice message, so stereo is M4A through the same /send-voice-base64 route;
tests/test_system_audio_m4a.py runs that through the real pipeline.

Panel methods run against stubs; nothing here opens a window.
"""

import base64
import inspect
import json
import types
from pathlib import Path
from tests.locales import load_strings, registered_locale_codes

import pytest

import main
from core import audio_devices
from core.message_queue import PendingMessage
from core.utils import DEFAULT_SETTINGS, backfill_missing_defaults
from core.voice_stereo import (
    STEREO_MIMETYPE, encode_as_stereo, opus_encode_args,
    recording_configs_preferring, voice_data_uri,
)
from main import MainWindow
from ui.conversations import ConversationsPanel
from tests.god_modules import conversations_source, patch_main_global


# ── The pure decisions ────────────────────────────────────────────────────────


class TestCaptureOrder:
    CONFIGS = [(48000, 1), (48000, 2), (44100, 1), (44100, 2)]

    def test_without_stereo_the_order_is_kept(self):
        assert recording_configs_preferring(self.CONFIGS, False) == self.CONFIGS

    def test_stereo_tries_two_channels_first_and_keeps_mono_as_the_tail(self):
        assert recording_configs_preferring(self.CONFIGS, True) == [
            (48000, 2), (44100, 2), (48000, 1), (44100, 1)]

    @pytest.mark.parametrize("max_channels", [2, 16])
    def test_a_device_with_two_or_more_channels_opens_with_two(self, monkeypatch, max_channels):
        """BlackHole 16ch: the system uses its first two channels."""
        class _Pa:
            def get_device_info_by_index(self, index):
                return {"defaultSampleRate": 48000.0, "maxInputChannels": max_channels}

        monkeypatch.setattr(audio_devices, "pyaudio", types.SimpleNamespace())
        configs = audio_devices.recording_configs_for(3, _Pa(), prefer_stereo=True)
        assert configs[0] == (48000, 2)
        assert all(channels <= 2 for _rate, channels in configs)

    def test_a_mono_only_microphone_still_records(self, monkeypatch):
        class _Pa:
            def get_device_info_by_index(self, index):
                return {"defaultSampleRate": 16000.0, "maxInputChannels": 1}

        monkeypatch.setattr(audio_devices, "pyaudio", types.SimpleNamespace())
        configs = audio_devices.recording_configs_for(3, _Pa(), prefer_stereo=True)
        assert (16000, 1) in configs


class TestWhatGoesOut:
    @pytest.mark.parametrize("channels, stereo", [
        (2, True), (1, False), (0, False), (None, False), ("x", False)])
    def test_the_captured_channels_alone_decide(self, channels, stereo):
        assert encode_as_stereo(channels) is stereo

    def test_the_opus_arguments_are_those_of_a_mono_voice_message(self):
        assert opus_encode_args() == ["-ac", "1", "-c:a", "libopus", "-b:a", "256k"]

    def test_the_data_uri_names_what_it_carries(self):
        assert voice_data_uri("QQ==") == "data:audio/ogg;codecs=opus;base64,QQ=="
        assert voice_data_uri("QQ==", STEREO_MIMETYPE) == "data:audio/mp4;base64,QQ=="


# ── The second button and its option are gone ─────────────────────────────────


def test_no_record_mode_is_left():
    src = conversations_source()
    for name in ("_record_voice_alt_btn", "_on_record_alternate_mode",
                 "_recording_stereo", "ID_CTRL_SHIFT_G"):
        assert name not in src, name
    assert "voice_message_stereo" not in DEFAULT_SETTINGS["general"]
    root = Path(__file__).parents[1] / "client"
    defaults = json.loads((root / "data" / "settings_default.json").read_text(encoding="utf-8"))
    assert "voice_message_stereo" not in defaults["general"]
    assert "voice_stereo" not in (root / "ui" / "dialogs" / "settings_dialog.py").read_text(encoding="utf-8")


def test_an_old_settings_file_with_the_removed_key_still_loads():
    """The key is simply left where it is: nothing reads it, nothing fails."""
    stored = {"general": {"voice_message_stereo": True},
              "user_interface": {"confirm_mark_all_read": False}}

    backfill_missing_defaults(stored, DEFAULT_SETTINGS)

    assert stored["general"]["voice_message_stereo"] is True
    assert stored["user_interface"]["confirm_mark_all_read"] is False


def test_no_locale_keeps_the_mode_strings():
    for locale in registered_locale_codes():
        strings = load_strings(locale)
        for key in ("record_voice_message_stereo", "record_voice_message_mono",
                    "voice_stereo_default_label", "voice_stereo_unavailable",
                    "shortcut_ctrl_shift_g_label"):
            assert key not in strings, (locale, key)


# ── Encoding and sending ──────────────────────────────────────────────────────


class _Encoder:
    _convert_wav_to_ogg = MainWindow._convert_wav_to_ogg

    def _find_api_ffmpeg(self):
        return __file__  # any existing file stands in for ffmpeg


def test_the_opus_encoder_is_always_mono_at_256k(monkeypatch, tmp_path):
    calls = []

    def _run(args, **_kw):
        calls.append(args)
        open(args[-1], "wb").write(b"ogg")
        return types.SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(main.subprocess, "run", _run)
    wav = tmp_path / "voz.wav"
    wav.write_bytes(b"RIFF")

    assert _Encoder()._convert_wav_to_ogg(str(wav))

    args = calls[0]
    assert args[args.index("-ac") + 1] == "1"
    assert args[args.index("-b:a") + 1] == "256k"


def test_a_stereo_voice_message_asks_the_aac_encoder_for_256k(monkeypatch, tmp_path):
    from core import audio_transcode
    m4a = tmp_path / "out.m4a"
    m4a.write_bytes(b"M4A")
    seen = {}

    def _encode(ffmpeg, wav, bitrate="192k"):
        seen.update(ffmpeg=ffmpeg, wav=wav, bitrate=bitrate)
        return str(m4a)
    monkeypatch.setattr(audio_transcode, "encode_system_audio_to_m4a", _encode)
    panel = types.SimpleNamespace(main_window=types.SimpleNamespace(_find_api_ffmpeg=lambda: "ff"))

    assert ConversationsPanel._encode_stereo_voice(panel, "v.wav") == b"M4A"

    assert seen == {"ffmpeg": "ff", "wav": "v.wav", "bitrate": "256k"}
    assert not m4a.exists()


def test_microphone_plus_computer_audio_keeps_its_192k(monkeypatch, tmp_path):
    from core import audio_transcode
    calls = []

    def _run(args, **_kw):
        calls.append(args)
        return types.SimpleNamespace(returncode=1, stderr=b"")
    monkeypatch.setattr(audio_transcode.subprocess, "run", _run)
    wav = tmp_path / "v.wav"
    wav.write_bytes(b"RIFF")

    audio_transcode.encode_system_audio_to_m4a(__file__, str(wav))

    assert calls[0][calls[0].index("-b:a") + 1] == "192k"


class _Sender:
    send_audio_message = MainWindow.send_audio_message


class TestVoiceSender:
    def _sent(self, monkeypatch, tmp_path, **kwargs):
        posted = []

        def _post(url, json=None, **_kw):
            posted.append((url, json))
            return types.SimpleNamespace(status_code=200, text="{}",
                                         json=lambda: {"response": [{"id": "R1"}]})
        patch_main_global(monkeypatch, "api_post", _post)
        s = _Sender()
        s.wpp_server, s.wpp_port, s.token = "http://127.0.0.1", 6300, "tok"
        s._resolve_jid_for_send = lambda jid: jid
        s._set_wa_connected = lambda *a, **kw: None
        s.send_audio_message("j@s.whatsapp.net", str(tmp_path / "v.wav"), **kwargs)
        return posted[0]

    def test_mono_goes_out_as_opus(self, monkeypatch, tmp_path):
        url, body = self._sent(monkeypatch, tmp_path, ogg_bytes=b"OPUS")

        assert url.endswith("/send-voice-base64")
        assert body["base64Ptt"].startswith("data:audio/ogg;codecs=opus;base64,")

    def test_stereo_goes_out_as_m4a_on_the_same_route(self, monkeypatch, tmp_path):
        url, body = self._sent(monkeypatch, tmp_path, ogg_bytes=b"M4A",
                               voice_mimetype=STEREO_MIMETYPE)

        assert url.endswith("/send-voice-base64")
        expected = base64.b64encode(b"M4A").decode()
        assert body["base64Ptt"] == f"data:audio/mp4;base64,{expected}"


def test_the_queue_hands_the_mimetype_to_the_sender():
    from core import message_queue
    assert PendingMessage("L1", "j", audio_path="a.wav").voice_mimetype == ""
    assert "voice_mimetype=msg.voice_mimetype" in inspect.getsource(message_queue)


def test_the_pending_row_says_what_goes_out():
    """Checked through is_voice_message() itself: the row keeps
    _is_voice_recording (the sent sound needs it), and that flag used to win
    over ptt."""
    from core.utils import is_voice_message

    def row(ptt):
        return {"_is_voice_recording": True, "messageType": "audioMessage",
                "message": {"audioMessage": {"seconds": 3, "ptt": ptt}}}
    assert is_voice_message(row(False)) is False   # microphone + computer audio
    assert is_voice_message(row(True)) is True     # mono or stereo voice message
    legacy = {"_is_voice_recording": True, "messageType": "audioMessage",
              "message": {"audioMessage": {"seconds": 3}}}
    assert is_voice_message(legacy) is True        # no ptt stated: as before


# ── The name the recipient sees ───────────────────────────────────────────────


class _MediaSender:
    send_media_attachment = MainWindow.send_media_attachment

    def __init__(self):
        self.wpp_server, self.wpp_port, self.token = "http://127.0.0.1", 6300, "tok"
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self._resolve_jid_for_send = lambda jid: jid
        self._find_api_ffmpeg = lambda: None
        self._set_wa_connected = lambda *a, **kw: None


def test_a_recorded_audio_upload_carries_the_localized_name_and_audio_mp4(monkeypatch, tmp_path):
    bodies = []

    def _post(url, headers=None, data=None, **_kw):
        bodies.append(data)
        return types.SimpleNamespace(status_code=200, text="{}",
                                     json=lambda: {"response": [{"id": "R1"}]})
    patch_main_global(monkeypatch, "api_post", _post)
    m4a = tmp_path / "winzapp-mixed-abc123.m4a"
    m4a.write_bytes(b"\x00\x00\x00\x20ftypM4A " + b"x" * 64)

    _MediaSender().send_media_attachment(
        "j@s.whatsapp.net", str(m4a), "audio",
        custom_filename="default_filename_audio.m4a")

    body = bodies[0]
    assert body.filename == "default_filename_audio.m4a"
    assert body.mime_type == "audio/mp4"
    sent = b"".join(body)
    assert b'name="filename"' in sent
    assert sent.count(b"default_filename_audio.m4a") == 2  # the field and the file part
    assert b"winzapp-mixed" not in sent


def test_the_pending_message_carries_the_custom_filename_to_the_sender():
    from core import message_queue
    pm = PendingMessage("L1", "j@s.whatsapp.net", media_path="x.m4a", media_type="audio",
                        custom_filename="Audio.m4a")
    assert pm.custom_filename == "Audio.m4a"
    assert PendingMessage("L2", "j", media_path="x.bin").custom_filename == ""
    assert "custom_filename=msg.custom_filename" in inspect.getsource(message_queue)
