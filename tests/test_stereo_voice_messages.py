"""Stereo voice messages (issue #82).

WinZapp recorded mono, or captured two channels and downmixed them in the
encoder (`-ac 1`), so a microphone with a real stereo mode lost its left/right
image. Stereo is now:

- a default in Settings > Dispositivos de áudio (general.voice_message_stereo);
- a second record button for the other mode, for one message;
- warned about before use, since iPhone cannot play a stereo voice message,
  with a "don't show again" that user_interface.warn_stereo_voice_iphone
  (Settings > Interface) mirrors.

Stereo is only what the microphone really gave: without two channels the
capture falls back to mono and says so.

Panel and dialog methods run against stubs; nothing here opens a window.
"""

import inspect
import types

import pytest

import main
from core import audio_devices
from core.message_queue import PendingMessage
from core.utils import DEFAULT_SETTINGS
from core.voice_stereo import (
    alternate_mode_is_stereo, alternate_record_label_key, encode_as_stereo,
    fell_back_to_mono, opus_encode_args, recording_configs_preferring,
)
from main import MainWindow
from ui import conversations
from ui.conversations import ConversationsPanel
from ui.dialogs import settings_dialog
from ui.dialogs.settings_dialog import SettingsDialog


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

    def test_the_encoder_arguments(self):
        assert opus_encode_args(False)[:2] == ["-ac", "1"]
        assert "64k" in opus_encode_args(False)
        assert opus_encode_args(True)[:2] == ["-ac", "2"]
        assert "96k" in opus_encode_args(True)

    def test_the_second_button_offers_the_other_mode(self):
        assert alternate_mode_is_stereo(False) is True
        assert alternate_mode_is_stereo(True) is False
        assert alternate_record_label_key(False) == "record_voice_message_stereo"
        assert alternate_record_label_key(True) == "record_voice_message_mono"


def test_the_defaults_keep_mono_and_warn():
    assert DEFAULT_SETTINGS["general"]["voice_message_stereo"] is False
    assert DEFAULT_SETTINGS["user_interface"]["warn_stereo_voice_iphone"] is True


# ── Encoding and sending ──────────────────────────────────────────────────────


class _Encoder:
    _convert_wav_to_ogg = MainWindow._convert_wav_to_ogg

    def _find_api_ffmpeg(self):
        return __file__  # any existing file stands in for ffmpeg


@pytest.mark.parametrize("stereo, channels", [(False, "1"), (True, "2")])
def test_the_encoder_is_asked_for_the_right_channel_count(monkeypatch, tmp_path, stereo, channels):
    calls = []

    def _run(args, **_kw):
        calls.append(args)
        open(args[-1], "wb").write(b"ogg")
        return types.SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(main.subprocess, "run", _run)
    wav = tmp_path / "voz.wav"
    wav.write_bytes(b"RIFF")

    assert _Encoder()._convert_wav_to_ogg(str(wav), stereo=stereo)

    args = calls[0]
    assert args[args.index("-ac") + 1] == channels


def test_a_retried_send_keeps_the_channels_of_the_first_try():
    assert PendingMessage("L1", "j@s.whatsapp.net", audio_path="a.wav", stereo=True).stereo is True
    assert PendingMessage("L1", "j@s.whatsapp.net", audio_path="a.wav").stereo is False
    from core import message_queue
    src = inspect.getsource(message_queue)
    assert 'stereo=getattr(msg, "stereo", False)' in src
    send_src = inspect.getsource(MainWindow.send_audio_message)
    assert "self._convert_wav_to_ogg(wav_path, stereo=stereo)" in send_src


# ── The second record button ──────────────────────────────────────────────────


class _PanelMainWindow:
    def __init__(self, default_stereo=False, warn=True):
        self.settings = {"general": {"voice_message_stereo": default_stereo},
                         "user_interface": {"warn_stereo_voice_iphone": warn}}
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
    state = {"answer": (True, False), "asked": 0}

    def _ask(parent, i18n):
        state["asked"] += 1
        return state["answer"]
    monkeypatch.setattr(conversations, "ask_stereo_voice", _ask)
    return state


class TestTheSecondButton:
    def test_with_mono_as_default_it_records_one_stereo_message(self, answer):
        panel = _Panel()

        panel._on_record_alternate_mode(None)

        assert answer["asked"] == 1
        assert panel.started == [True]

    def test_no_to_the_iphone_warning_records_nothing(self, answer):
        answer["answer"] = (False, False)
        panel = _Panel()

        panel._on_record_alternate_mode(None)

        assert panel.started == []

    def test_dont_show_again_with_yes_turns_the_warning_off(self, answer):
        answer["answer"] = (True, True)
        panel = _Panel()

        panel._on_record_alternate_mode(None)

        assert panel.main_window.settings["user_interface"]["warn_stereo_voice_iphone"] is False
        assert panel.main_window.saves == 1
        assert panel.started == [True]

    def test_with_the_warning_off_it_does_not_ask(self, answer):
        panel = _Panel(warn=False)

        panel._on_record_alternate_mode(None)

        assert answer["asked"] == 0
        assert panel.started == [True]

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


# ── Settings: warning when stereo is turned on ────────────────────────────────


class _Box:
    def __init__(self, value):
        self.value = value

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = value


class _SettingsDialog:
    _confirm_stereo_voice_if_newly_enabled = SettingsDialog._confirm_stereo_voice_if_newly_enabled

    def __init__(self, ticked=True, was_on=False, warn_box=True):
        self._voice_stereo_check = _Box(ticked)
        self._warn_stereo_voice_cb = _Box(warn_box)
        self.main_window = _PanelMainWindow(default_stereo=was_on)


@pytest.fixture
def settings_answer(monkeypatch):
    state = {"answer": (True, False), "asked": 0}

    def _ask(parent, i18n):
        state["asked"] += 1
        return state["answer"]
    monkeypatch.setattr(settings_dialog, "ask_stereo_voice", _ask)
    return state


class TestSavingSettings:
    def test_turning_stereo_on_asks(self, settings_answer):
        dialog = _SettingsDialog()

        dialog._confirm_stereo_voice_if_newly_enabled()

        assert settings_answer["asked"] == 1
        assert dialog._voice_stereo_check.value is True

    def test_no_leaves_stereo_off(self, settings_answer):
        settings_answer["answer"] = (False, False)
        dialog = _SettingsDialog()

        dialog._confirm_stereo_voice_if_newly_enabled()

        assert dialog._voice_stereo_check.value is False

    def test_dont_show_again_unticks_the_warning_box_too(self, settings_answer):
        settings_answer["answer"] = (True, True)
        dialog = _SettingsDialog()

        dialog._confirm_stereo_voice_if_newly_enabled()

        assert dialog._warn_stereo_voice_cb.value is False
        assert dialog.main_window.settings["user_interface"]["warn_stereo_voice_iphone"] is False

    def test_already_on_is_not_asked_again(self, settings_answer):
        _SettingsDialog(was_on=True)._confirm_stereo_voice_if_newly_enabled()
        assert settings_answer["asked"] == 0

    def test_the_warning_box_unticked_in_this_same_save_is_honoured(self, settings_answer):
        _SettingsDialog(warn_box=False)._confirm_stereo_voice_if_newly_enabled()
        assert settings_answer["asked"] == 0

    def test_stereo_left_off_is_not_asked(self, settings_answer):
        _SettingsDialog(ticked=False)._confirm_stereo_voice_if_newly_enabled()
        assert settings_answer["asked"] == 0

    def test_the_save_asks_before_writing_the_key(self):
        src = inspect.getsource(SettingsDialog._apply_values)
        assert src.index("self._confirm_stereo_voice_if_newly_enabled()") < src.index(
            '["voice_message_stereo"]')


# ── The recording pipeline ────────────────────────────────────────────────────


class TestThePipelineIsWired:
    def test_the_capture_prefers_two_channels_when_stereo_is_wanted(self):
        src = inspect.getsource(ConversationsPanel._start_voice_recording)
        assert "prefer_stereo=want_stereo" in src
        assert "fell_back_to_mono(want_stereo, ch)" in src
        assert 'i18n.t("voice_stereo_unavailable")' in src

    def test_the_message_is_encoded_and_queued_with_the_decision(self):
        src = inspect.getsource(ConversationsPanel)
        assert "stereo_out      = encode_as_stereo(self._recording_stereo, actual_ch)" in src
        assert "mw._convert_wav_to_ogg(wav_path, stereo=stereo_out)" in src
        assert "stereo=stereo_out)" in src

    def test_the_second_button_follows_the_first_everywhere(self):
        """Every Hide/Show/Enable/Disable of the record button is mirrored."""
        src = inspect.getsource(ConversationsPanel)
        for action in ("Hide", "Show", "Enable", "Disable"):
            assert (src.count(f"self.record_voice_message_btn.{action}()")
                    == src.count(f"self._record_voice_alt_btn.{action}()")), action
