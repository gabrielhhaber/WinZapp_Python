"""Changing L/R signals retain sample order, channels and duration across chunks.

Drive the actual paused-preview and send methods with irregular capture blocks,
including empty blocks and byte splits inside a sample. Encoders, workers and
playback are fake; WAV/retry/cache files are real files in the test directory.
"""

from array import array
from functools import lru_cache
import math
import sys
import wave

# Load the compiled dependency before the existing composer harness snapshots
# sys.modules; removing and re-importing NumPy's extension in one process fails.
import numpy  # noqa: F401
import pytest

from tests.test_voice_recording_mode import composer
from tests.test_recorded_audio_preview import _Panel, _FakeSound
from ui.conversation_panel import voice_recording


def little_endian(samples):
    data = array('h', samples)
    if sys.byteorder != 'little':
        data.byteswap()
    return data.tobytes()


@lru_cache(maxsize=4)
def changing_signal(rate):
    # Odd frame count and a partial final second expose duration/order errors.
    count = 2 * rate + 137
    stereo, mono = array('h'), array('h')
    for n in range(count):
        phase = (n * 8 // rate) % 4
        envelope = 0.55 + 0.4 * math.sin(2 * math.pi * 1.7 * n / rate)
        left = round(21000 * envelope * math.sin(2 * math.pi * 431 * n / rate))
        right = round(17000 * (1.1 - envelope) * math.sin(2 * math.pi * 997 * n / rate))
        if phase == 0:
            right = 0
        elif phase == 1:
            left = 0
        elif phase == 3:
            left = right = 0
        stereo.extend((left, right))
        mono.append((left + right) // 2)
    return little_endian(stereo), little_endian(mono), count


def split_capture(data, style):
    # Valid whole-frame callbacks and an adversarial byte-fragmented transport
    # must both reconstruct the same recording before channel conversion.
    sizes = (4, 68, 16384, 12, 4004) if style == 'frames' else (1, 7, 8191, 2, 509)
    blocks, pos, turn = [b''], 0, 0
    while pos < len(data):
        size = sizes[turn % len(sizes)]
        blocks.append(data[pos:pos + size])
        pos += size
        turn += 1
        if turn % 7 == 0:
            blocks.append(b'')
    return [*blocks, b'']


@pytest.mark.parametrize('rate', [8000, 16000, 44100, 48000])
@pytest.mark.parametrize('mode', ['mono', 'auto', 'stereo'])
@pytest.mark.parametrize('partition', ['frames', 'bytes'])
def test_dynamic_partitioned_preview_send_and_retry_agree(
        composer, monkeypatch, rate, mode, partition):
    source, mixed, count = changing_signal(rate)
    blocks = split_capture(source, partition)
    original_blocks = tuple(blocks)
    assert b''.join(blocks) == source
    expected_pcm, channels = (mixed, 1) if mode == 'mono' else (source, 2)

    monkeypatch.setattr(voice_recording.sl_stream, 'FileStream', lambda **kwargs: _FakeSound())
    preview = _Panel()
    preview._recording_paused = True
    preview._recording_actual_rate = rate
    preview._recording_actual_ch = 2
    preview._recording_channel_mode = mode
    preview._recording_frames = list(blocks)
    preview._toggle_play_recorded_audio(None)
    try:
        with wave.open(preview._recorded_audio_temp_path, 'rb') as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getnframes()) == (rate, channels, count)
            preview_pcm = wav.readframes(count)
            assert preview_pcm == expected_pcm
    finally:
        preview._stop_recorded_audio_preview()

    h, panel = composer, composer.p
    panel._recording_system_audio = False
    panel._system_audio_session = None
    panel._recording_channel_mode = mode
    panel.main_window.settings['general']['noise_reduction_enabled'] = False
    panel._recording_actual_rate = rate
    panel._recording_actual_ch = 2
    panel._recording_frames = list(blocks)
    encoded = h.folder / 'partitioned.ogg'
    encoded.write_bytes(b'OPUS')
    panel.main_window._convert_wav_to_ogg = lambda path: str(encoded)
    panel._encode_stereo_voice = lambda path: b'AAC'
    panel._send_voice_message(None)
    h.threads.drain()

    queued = panel.main_window.message_queue.enqueue.call_args.args[0]
    with wave.open(queued.audio_path, 'rb') as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getnframes()) == (rate, channels, count)
        assert wav.readframes(count) == preview_pcm
    row = panel._sorted_messages[0]['message']['audioMessage']
    assert row['seconds'] == count // rate
    assert row['ptt'] is True
    assert queued.voice_mimetype == ('audio/mp4' if channels == 2 else '')
    expected_encoded = b'AAC' if channels == 2 else b'OPUS'
    assert queued.ogg_bytes == expected_encoded
    cached = h.folder / 'voice_messages' / (queued.local_id + '.msv')
    assert h.decrypt(cached.read_bytes()) == expected_encoded
    assert tuple(blocks) == original_blocks
    assert b''.join(blocks) == source
