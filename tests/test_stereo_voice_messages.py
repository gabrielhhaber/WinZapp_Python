"""Stereo voice messages (issue #82).

WinZapp recorded mono, or captured two channels and downmixed them in the
encoder (`-ac 1`), so a microphone with a real stereo mode lost its left/right
image. Stereo is now:

- a default in Settings > Dispositivos de áudio (general.voice_message_stereo);
- a second record button for the other mode, for one message;
- sent as an audio message rather than a voice message, in exactly the
  format of microphone + computer audio (Ctrl+Shift+H): AAC-LC M4A through
  the attachment route, which iPhone plays (stereo OGG/Opus it does not);
  tests/test_system_audio_m4a.py checks that route against the pipeline.

Stereo is only what the microphone really gave: without two channels the
capture falls back to mono and says so.

Panel methods run against stubs; nothing here opens a window.
"""

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
    alternate_mode_is_stereo, alternate_record_label_key, encode_as_stereo,
    fell_back_to_mono, opus_encode_args, recording_configs_preferring,
    sends_as_audio_file,
)
from main import MainWindow
from ui.conversations import ConversationsPanel
from tests.god_modules import conversations_source, patch_main_global


# ── The pure decisions ────────────────────────────────────────────────────────


class TestCaptureOrder:
    CONFIGS = [(48000, 1), (48000, 2), (44100, 1), (44100, 2)]

    def test_mono_keeps_the_order_it_always_had(self):
        assert recording_configs_preferring(self.CONFIGS, False) == self.CONFIGS

    def test_stereo_tries_two_channels_first_and_keeps_mono_as_the_tail(self):
        assert recording_configs_preferring(self.CONFIGS, True) == [
            (48000, 2), (44100, 2), (48000, 1), (44100, 1)]

    def test_a_device_asked_through_recording_configs_for(self, monkeypatch):
        class _Pa:
            def get_device_info_by_index(self, index):
                return {"defaultSampleRate": 48000.0, "maxInputChannels": 2}

        monkeypatch.setattr(audio_devices, "pyaudio", types.SimpleNamespace())
        configs = audio_devices.recording_configs_for(3, _Pa(), prefer_stereo=True)

        assert configs[0] == (48000, 2)
        assert (48000, 1) in configs  # still there to fall back on

    def test_a_mono_only_microphone_still_records(self, monkeypatch):
        class _Pa:
            def get_device_info_by_index(self, index):
                return {"defaultSampleRate": 16000.0, "maxInputChannels": 1}

        monkeypatch.setattr(audio_devices, "pyaudio", types.SimpleNamespace())
        configs = audio_devices.recording_configs_for(3, _Pa(), prefer_stereo=True)

        assert (16000, 1) in configs


class TestWhatGoesOut:
    def test_stereo_needs_both_the_request_and_two_real_channels(self):
        assert encode_as_stereo(True, 2) is True
        assert encode_as_stereo(True, 1) is False
        assert encode_as_stereo(False, 2) is False  # mono mode downmixes

    def test_the_fallback_is_detected_only_when_stereo_was_asked_for(self):
        assert fell_back_to_mono(True, 1) is True
        assert fell_back_to_mono(True, 2) is False
        assert fell_back_to_mono(False, 1) is False

    def test_the_opus_arguments_are_those_of_a_mono_voice_message(self):
        assert opus_encode_args() == ["-ac", "1", "-c:a", "libopus", "-b:a", "64k"]

    def test_the_second_button_offers_the_other_mode(self):
        assert alternate_mode_is_stereo(False) is True
        assert alternate_mode_is_stereo(True) is False
        assert alternate_record_label_key(False) == "record_voice_message_stereo"
        assert alternate_record_label_key(True) == "record_voice_message_mono"


def test_the_default_keeps_mono():
    assert DEFAULT_SETTINGS["general"]["voice_message_stereo"] is False


def test_the_iphone_warning_setting_is_gone():
    assert "warn_stereo_voice_iphone" not in DEFAULT_SETTINGS["user_interface"]
    root = Path(__file__).parents[1] / "client"
    defaults = json.loads((root / "data" / "settings_default.json").read_text(encoding="utf-8"))
    assert "warn_stereo_voice_iphone" not in defaults["user_interface"]
    assert not (root / "ui" / "dialogs" / "stereo_voice_warning.py").exists()


def test_an_old_settings_file_with_the_removed_key_still_loads():
    """The key is simply left where it is: nothing reads it, nothing fails."""
    stored = {"general": {"voice_message_stereo": True},
              "user_interface": {"warn_stereo_voice_iphone": False,
                                 "confirm_mark_all_read": False}}

    backfill_missing_defaults(stored, DEFAULT_SETTINGS)

    assert stored["general"]["voice_message_stereo"] is True
    assert stored["user_interface"]["confirm_mark_all_read"] is False
    assert "confirm_resync_all" in stored["user_interface"]


def test_no_locale_keeps_the_iphone_warning_strings():
    for locale in registered_locale_codes():
        strings = load_strings(locale)
        for key in ("stereo_voice_iphone_warning", "stereo_voice_warning_title",
                    "ui_warn_stereo_voice_iphone"):
            assert key not in strings, (path.name, key)


# ── Encoding and sending ──────────────────────────────────────────────────────


class _Encoder:
    _convert_wav_to_ogg = MainWindow._convert_wav_to_ogg

    def _find_api_ffmpeg(self):
        return __file__  # any existing file stands in for ffmpeg


def test_the_voice_message_encoder_is_always_mono(monkeypatch, tmp_path):
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
    assert args[args.index("-b:a") + 1] == "64k"


def test_a_pending_voice_message_no_longer_carries_a_channel_choice():
    assert not hasattr(PendingMessage("L1", "j@s.whatsapp.net", audio_path="a.wav"), "stereo")
    from core import message_queue
    assert "stereo" not in inspect.getsource(message_queue.MessageQueue)


# ── Stereo goes out as an audio message, not a voice message ─────────────────


class _Sender:
    send_audio_message = MainWindow.send_audio_message


def test_only_stereo_sends_as_an_audio_file():
    assert sends_as_audio_file(True) is True
    assert sends_as_audio_file(False) is False


class TestVoiceSender:
    def test_the_voice_sender_only_posts_a_ptt_voice_message(self, monkeypatch, tmp_path):
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

        s.send_audio_message("j@s.whatsapp.net", str(tmp_path / "v.wav"),
                             ogg_bytes=b"OPUS-MONO")

        assert posted[0][0].endswith("/send-voice-base64")
        assert "base64Ptt" in posted[0][1]

    def test_the_old_stereo_ogg_file_route_is_gone(self):
        assert not hasattr(MainWindow, "_send_recording_as_audio_file")
        assert "stereo" not in inspect.signature(MainWindow.send_audio_message).parameters
        assert "stereo" not in inspect.signature(MainWindow._convert_wav_to_ogg).parameters

    def test_the_pending_row_already_reads_as_audio(self):
        """The row shown while sending must say what is going out. Checked
        through is_voice_message() itself: the row keeps _is_voice_recording
        (the sent sound needs it), and that flag used to win over ptt. That
        _send_voice_message writes ptt False for a stereo recording is
        checked by running it, in tests/test_system_audio_m4a.py."""
        from core.utils import is_voice_message

        def row(ptt):
            return {"_is_voice_recording": True, "messageType": "audioMessage",
                    "message": {"audioMessage": {"seconds": 3, "ptt": ptt}}}
        assert is_voice_message(row(False)) is False   # stereo / mixed: audio
        assert is_voice_message(row(True)) is True     # mono: voice message
        legacy = {"_is_voice_recording": True, "messageType": "audioMessage",
                  "message": {"audioMessage": {"seconds": 3}}}
        assert is_voice_message(legacy) is True        # no ptt stated: as before


# ── The second record button ──────────────────────────────────────────────────


class _PanelMainWindow:
    def __init__(self, default_stereo=False):
        self.settings = {"general": {"voice_message_stereo": default_stereo}}
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self.saves = 0

    def save_settings(self):
        self.saves += 1


class _Panel:
    _on_record_alternate_mode = ConversationsPanel._on_record_alternate_mode
    _default_recording_stereo = ConversationsPanel._default_recording_stereo
    _alternate_record_label_key = ConversationsPanel._alternate_record_label_key
    refresh_alternate_record_button = ConversationsPanel.refresh_alternate_record_button

    def __init__(self, **kw):
        self.main_window = _PanelMainWindow(**kw)
        self._is_recording = False
        self._recording_starting = False
        self.started = []

    def _start_voice_recording(self, stereo=None):
        self.started.append(stereo)


@pytest.fixture
def answer(monkeypatch):
    """The warning dialog is gone: any attempt to ask would fail the test."""
    from ui.dialogs import checkbox_confirm
    state = {"asked": 0}

    def _ask(*a, **kw):
        state["asked"] += 1
        pytest.fail("the second record button opened a confirmation")
    monkeypatch.setattr(checkbox_confirm, "confirm_with_checkbox", _ask)
    return state


class TestTheSecondButton:
    def test_with_mono_as_default_it_records_one_stereo_message(self, answer):
        panel = _Panel()

        panel._on_record_alternate_mode(None)

        assert panel.started == [True]

    def test_with_the_default_mono_it_never_asks(self, answer):
        panel = _Panel()

        panel._on_record_alternate_mode(None)

        assert panel.started == [True]
        assert answer["asked"] == 0

    def test_with_stereo_as_default_it_records_one_mono_message_without_asking(self, answer):
        panel = _Panel(default_stereo=True)

        panel._on_record_alternate_mode(None)

        assert answer["asked"] == 0
        assert panel.started == [False]

    def test_it_does_nothing_while_a_recording_is_on(self, answer):
        panel = _Panel()
        panel._is_recording = True

        panel._on_record_alternate_mode(None)

        assert panel.started == []

    def test_its_label_follows_the_default(self):
        class _Button:
            label = None

            def __bool__(self):
                return True

            def SetLabel(self, label):
                self.label = label
        panel = _Panel(default_stereo=True)
        panel._record_voice_alt_btn = _Button()

        panel.refresh_alternate_record_button()

        assert panel._record_voice_alt_btn.label == "record_voice_message_mono"


# ── Settings ──────────────────────────────────────────────────────────────────


def test_the_settings_dialog_has_no_stereo_warning_left():
    from ui.dialogs.settings_dialog import SettingsDialog
    import ui.dialogs.settings_dialog as module
    source = inspect.getsource(module)
    assert "stereo_voice_warning" not in source
    assert "warn_stereo_voice" not in source
    assert "ui_warn_stereo_voice_iphone" not in source
    assert not hasattr(SettingsDialog, "_confirm_stereo_voice_if_newly_enabled")
    assert '["voice_message_stereo"]' in source  # the default itself stays


# ── The recording pipeline ────────────────────────────────────────────────────


class TestThePipelineIsWired:
    def test_the_capture_prefers_two_channels_when_stereo_is_wanted(self):
        src = inspect.getsource(ConversationsPanel._start_voice_recording)
        assert "prefer_stereo=want_stereo" in src
        assert "fell_back_to_mono(want_stereo, ch)" in src
        assert 'i18n.t("voice_stereo_unavailable")' in src

    def test_the_message_is_encoded_and_queued_with_the_decision(self):
        src = conversations_source()
        assert "stereo_out      = encode_as_stereo(self._recording_stereo, actual_ch)" in src
        assert "mw._convert_wav_to_ogg(wav_path)" in src
        assert "if as_audio_file:\n                self._enqueue_system_audio_file(" in src.replace("\r\n", "\n")

    def test_the_second_button_follows_the_first_everywhere(self):
        """Every Hide/Show/Enable/Disable of the record button is mirrored."""
        src = conversations_source()
        for action in ("Hide", "Show", "Enable", "Disable"):
            assert (src.count(f"self.record_voice_message_btn.{action}()")
                    == src.count(f"self._record_voice_alt_btn.{action}()")), action


# ── Ctrl+Shift+G ──────────────────────────────────────────────────────────────


class _EnabledButton:
    def __init__(self, enabled):
        self.enabled = enabled

    def IsEnabled(self):
        return self.enabled


class TestTheShortcut:
    def test_it_is_in_the_conversation_accelerators(self):
        src = conversations_source()
        assert 'ord("G"),          self.ID_CTRL_SHIFT_G)' in src
        assert "self._on_record_alternate_mode,     id=self.ID_CTRL_SHIFT_G)" in src

    def test_the_screen_reader_announces_it_on_the_button(self):
        src = conversations_source()
        assert ('self._record_voice_alt_btn.SetAccessible(\n'
                '            AccessibleRecordVoiceMessage("Ctrl+Shift+G", ("messages.ID_CTRL_SHIFT_G",))'
                ) in src.replace("\r\n", "\n")

    def test_it_is_listed_with_the_other_shortcuts(self):
        from ui.dialogs import shortcuts_dialog
        src = inspect.getsource(shortcuts_dialog)
        assert src.index('i18n.t("shortcut_ctrl_r_label")') < src.index(
            'i18n.t("shortcut_ctrl_shift_g_label")')

    def test_it_does_nothing_where_the_button_is_disabled(self, answer):
        """A channel, or a group only admins can post in: Ctrl+Shift+G reaches
        the handler anyway, since accelerators ignore the button's state."""
        panel = _Panel()
        panel._record_voice_alt_btn = _EnabledButton(False)

        panel._on_record_alternate_mode(None)

        assert panel.started == []
        assert answer["asked"] == 0

    def test_it_records_where_the_button_is_enabled(self, answer):
        panel = _Panel()
        panel._record_voice_alt_btn = _EnabledButton(True)

        panel._on_record_alternate_mode(None)

        assert panel.started == [True]


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
