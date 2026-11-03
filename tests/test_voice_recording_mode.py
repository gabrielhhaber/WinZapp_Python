"""#677/#679: manual mono retains INPUT 1 and INPUT 2, without device probes."""

import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from core.voice_recording_mode import (
    RECORDING_MODES, normalize_recording_mode, voice_recording_mode,
    prepare_voice_pcm, recording_output_channels,
)
from core.utils import DEFAULT_SETTINGS, backfill_missing_defaults
from ui.dialogs.voice_recording_options import VoiceRecordingOptionsMixin


def pcm(*samples):
    return struct.pack('<' + 'h' * len(samples), *samples)


@pytest.mark.parametrize('value', [None, '', 'unknown', True, 1, [], {}])
def test_unknown_preferences_keep_device_based_behavior(value):
    assert normalize_recording_mode(value) == 'auto'


@pytest.mark.parametrize('mode', RECORDING_MODES)
def test_preferences_survive_default_backfill(mode):
    settings = {'general': {'voice_recording_mode': mode}}
    backfill_missing_defaults(settings, DEFAULT_SETTINGS)
    assert voice_recording_mode(settings) == mode


def test_new_and_legacy_settings_keep_auto_without_reviving_removed_boolean():
    defaults = json.loads((Path(__file__).parents[1] /
                          'client/data/settings_default.json').read_text(encoding='utf-8'))
    assert voice_recording_mode(defaults) == voice_recording_mode(DEFAULT_SETTINGS) == 'auto'
    for settings in ({}, None, {'general': None},
                     {'general': {'voice_message_stereo': True}},
                     {'general': {'voice_message_stereo': False}}):
        assert voice_recording_mode(settings) == 'auto'


@pytest.mark.parametrize('left,right,expected', [
    (20000, 0, 10000), (0, 20000, 10000), (-20000, 0, -10000),
    (0, -20000, -10000), (32767, 32767, 32767),
    (-32768, -32768, -32768), (10000, -10000, 0), (0, 0, 0),
])
def test_mono_mixes_both_inputs_with_headroom(left, right, expected):
    source = pcm(left, right) * 100
    result, channels = prepare_voice_pcm(source, 2, 'mono')
    assert (result, channels) == (pcm(expected) * 100, 1)
    assert len(result) * 2 == len(source)  # duration in frames is preserved
    assert source == pcm(left, right) * 100


@pytest.mark.parametrize('mode', ['auto', 'stereo', 'invalid'])
def test_stereo_is_not_changed_or_inferred_from_silence(mode):
    source = pcm(1000, 0, 0, -2000, 123, -456)
    assert prepare_voice_pcm(source, 2, mode) == (source, 2)
    assert recording_output_channels(2, mode) == 2


@pytest.mark.parametrize('mode', RECORDING_MODES)
def test_mono_only_device_remains_mono_even_when_stereo_is_requested(mode):
    source = pcm(-32768, 32767, 0)
    assert prepare_voice_pcm(source, 1, mode) == (source, 1)
    assert recording_output_channels(1, mode) == 1


def test_mono_rejects_partial_frames_instead_of_corrupting_channels():
    with pytest.raises(ValueError):
        prepare_voice_pcm(b'\x00\x00', 2, 'mono')
    assert prepare_voice_pcm(b'', 2, 'mono') == (b'', 1)


class Radio:
    def __init__(self, parent=None, label='', choices=(), **kwargs):
        self.selection, self.label, self.choices = 0, label, list(choices)

    def GetSelection(self):
        return self.selection

    def SetSelection(self, index):
        self.selection = index

    def SetLabel(self, label):
        self.label = label

    def SetItemLabel(self, index, label):
        self.choices[index] = label


@pytest.mark.parametrize('mode', RECORDING_MODES)
def test_native_selector_load_save_and_relabel_preserve_choice(mode, monkeypatch):
    from ui.dialogs import voice_recording_options as options
    monkeypatch.setattr(options.wx, 'RadioBox', Radio)
    dialog = SimpleNamespace(main_window=SimpleNamespace(
        settings={'general': {'voice_recording_mode': mode}},
        i18n=SimpleNamespace(t=lambda key: 'first:' + key)))
    sizer = SimpleNamespace(Add=Mock())
    VoiceRecordingOptionsMixin._build_voice_recording_options(dialog, object(), sizer)
    VoiceRecordingOptionsMixin._load_voice_recording_options(dialog)
    radio = dialog._voice_recording_mode_radio
    assert radio.GetSelection() == RECORDING_MODES.index(mode)
    dialog.main_window.i18n.t = lambda key: 'second:' + key
    VoiceRecordingOptionsMixin._relabel_voice_recording_options(dialog)
    assert radio.GetSelection() == RECORDING_MODES.index(mode)
    assert len(radio.choices) == 3 and all(x.startswith('second:') for x in radio.choices)
    VoiceRecordingOptionsMixin._save_voice_recording_options(dialog)
    assert voice_recording_mode(dialog.main_window.settings) == mode


def test_unselected_control_cannot_save_stereo_by_negative_index():
    dialog = SimpleNamespace(main_window=SimpleNamespace(settings={}),
                             _voice_recording_mode_radio=Radio())
    dialog._voice_recording_mode_radio.selection = -1
    VoiceRecordingOptionsMixin._save_voice_recording_options(dialog)
    assert voice_recording_mode(dialog.main_window.settings) == 'auto'


@pytest.fixture
def composer():
    # Reuse the existing real-send-handler harness with queued fake workers,
    # fake encoders, an isolated key and temporary files. No account/device.
    from tests import test_system_audio_m4a as mixed
    harness = mixed.TestMixedComposer(methodName='runTest')
    harness.setUp()
    try:
        yield harness
    finally:
        harness.doCleanups()


@pytest.mark.parametrize('mode,captured,expected', [
    ('mono', 2, 1), ('auto', 2, 2), ('stereo', 2, 2), ('stereo', 1, 1),
])
@pytest.mark.parametrize('noise_enabled', [False, True])
def test_send_snapshots_mode_and_queues_matching_pcm_and_mimetype(
        composer, mode, captured, expected, noise_enabled, monkeypatch):
    h, panel = composer, composer.p
    panel._recording_system_audio = False
    panel._system_audio_session = None
    panel._recording_channel_mode = mode
    panel.main_window.settings['general']['noise_reduction_enabled'] = noise_enabled
    panel._recording_actual_ch = captured
    source = pcm(0, 20000) * 48000 if captured == 2 else pcm(20000) * 48000
    panel._recording_frames = [source]
    wanted = pcm(10000) * 48000 if expected < captured else source
    reduce_noise = Mock(side_effect=lambda data, rate, channels: (data, 'neural'))
    monkeypatch.setitem(sys.modules, 'core.neural_noise_reduction',
                        SimpleNamespace(reduce_voice_noise=reduce_noise))
    encoded = h.folder / 'mono.ogg'
    encoded.write_bytes(b'OPUS')
    def encode_mono(path):
        with wave.open(path, 'rb') as wav:
            assert (wav.getnchannels(), wav.getnframes()) == (expected, 48000)
            assert wav.readframes(48000) == wanted
        return str(encoded)
    panel.main_window._convert_wav_to_ogg = Mock(side_effect=encode_mono)
    panel._encode_stereo_voice = Mock(return_value=b'AAC')
    panel._send_voice_message(None)
    row = panel._sorted_messages[0]['message']['audioMessage']
    assert row['seconds'] == 1 and row['ptt'] is True
    assert row.get('mimetype', '') == ('audio/mp4' if expected == 2 else '')
    # Starting another recording or changing Settings cannot alter this worker.
    panel._recording_channel_mode = 'stereo' if mode == 'mono' else 'mono'
    panel.main_window.settings['general']['voice_recording_mode'] = panel._recording_channel_mode
    h.threads.drain()
    queued = panel.main_window.message_queue.enqueue.call_args.args[0]
    assert queued.ogg_bytes == (b'AAC' if expected == 2 else b'OPUS')
    assert queued.voice_mimetype == ('audio/mp4' if expected == 2 else '')
    with wave.open(queued.audio_path, 'rb') as wav:
        assert wav.getnchannels() == expected
        assert wav.readframes(48000) == wanted
    if expected == 1:
        panel.main_window._convert_wav_to_ogg.assert_called_once()
        panel._encode_stereo_voice.assert_not_called()
    else:
        panel._encode_stereo_voice.assert_called_once()
        panel.main_window._convert_wav_to_ogg.assert_not_called()
    if noise_enabled:
        reduce_noise.assert_called_once_with(wanted, 48000, expected)
    else:
        reduce_noise.assert_not_called()


def test_mixed_capture_ignores_the_previous_microphone_channel_preference(composer):
    h = composer
    h.p._recording_channel_mode = 'mono'
    h.p._send_voice_message(None)
    h.threads.drain()
    h.drain_ui()
    queued = h.p.main_window.message_queue.enqueue.call_args.args[0]
    assert queued.media_type == 'audio'
    assert h.encoder.call_args.kwargs.get('bitrate', '192k') == '192k'
    assert h.p._sorted_messages[0]['message']['audioMessage']['ptt'] is False


def test_conversion_failure_is_visible_and_never_queues_wrong_channels(composer, monkeypatch):
    h, panel = composer, composer.p
    panel._recording_system_audio = False
    panel._system_audio_session = None
    panel._recording_channel_mode = 'mono'
    def broken_conversion(*args):
        raise ValueError('incomplete PCM frame')
    monkeypatch.setitem(panel._send_voice_message.__func__.__globals__,
                        'prepare_voice_pcm', broken_conversion)
    panel._send_voice_message(None)
    h.threads.drain()
    h.drain_ui()
    panel.main_window.message_queue.enqueue.assert_not_called()
    local_id = panel._sorted_messages[0]['_local_id']
    panel.main_window._on_message_failed.assert_called_once_with(
        local_id, 'media_audio_convert_failed', True)


@pytest.mark.parametrize('mode', RECORDING_MODES)
def test_start_freezes_preference_and_keeps_both_interface_inputs(mode, monkeypatch):
    from tests.test_recording_open_failure import _Stub, _FakeStream
    from tests.test_system_audio_ui import QueuedThreads
    from tests.god_modules import patch_conversations_global
    from ui.conversation_panel import voice_recording
    queue = QueuedThreads()
    attempts = []
    class Device:
        def open(self, **kwargs):
            attempts.append(kwargs)
            return _FakeStream()
    panel = _Stub()
    panel._recording_pa = Device()
    panel.main_window.settings['general'] = {'voice_recording_mode': mode}
    patch_conversations_global(monkeypatch, 'pyaudio', SimpleNamespace(paInt16=8, paContinue=0))
    patch_conversations_global(monkeypatch, 'find_input_device_index', lambda *args: 1)
    patch_conversations_global(monkeypatch, 'recording_configs_for',
                              lambda *args, **kw: [(48000, 2), (48000, 1)])
    patch_conversations_global(monkeypatch, 'threading', queue)
    delivered = []
    monkeypatch.setattr(voice_recording.wx, 'CallAfter', lambda *args: delivered.append(args))
    panel._start_voice_recording()
    panel.main_window.settings['general']['voice_recording_mode'] = 'invalid'
    queue.drain()
    assert panel._recording_channel_mode == mode
    assert attempts[0]['channels'] == 2
    assert delivered[0][2:4] == (48000, 2)


@pytest.mark.parametrize('mixed_audio,expected', [(False, 1), (True, 2)])
def test_paused_preview_uses_the_same_channel_rule(monkeypatch, mixed_audio, expected):
    from tests.test_recorded_audio_preview import _Panel, _FakeSound
    from ui.conversation_panel import voice_recording
    monkeypatch.setattr(voice_recording.sl_stream, 'FileStream', lambda **kwargs: _FakeSound())
    panel = _Panel()
    panel._recording_paused = True
    panel._recording_system_audio = mixed_audio
    panel._recording_channel_mode = 'mono'
    panel._recording_actual_ch = 2
    panel._recording_frames = [pcm(0, 20000) * 20]
    panel._toggle_play_recorded_audio(None)
    try:
        with wave.open(panel._recorded_audio_temp_path, 'rb') as wav:
            assert wav.getnchannels() == expected
            assert wav.readframes(20) == (pcm(10000) * 20 if expected == 1 else panel._recording_frames[0])
    finally:
        panel._stop_recorded_audio_preview()
