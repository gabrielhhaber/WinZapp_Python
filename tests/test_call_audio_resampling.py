"""Call conversion must not gain samples or restart its filter at packets.

Only synthetic PCM and injected sounddevice stubs; no audio hardware, account,
wx.App, live call, or real-time sleeps. These are signal/transport contracts,
not a claim about the quality of a real WhatsApp call.
"""

import base64

import numpy as np
import pytest

from core.call_audio import CALL_FRAME_SAMPLES, CALL_SAMPLE_RATE, CallAudioConfig, CallAudioSession
from core.call_audio_resampling import MicrophonePCMEncoder, StreamingMonoResampler
from core.call_media_ownership import CapturedPCM


def _stream(data, source, target, sizes):
    converter = StreamingMonoResampler()
    output = []
    offset = 0
    for size in sizes:
        output.append(converter.process(data[offset:offset + size], source, target))
        offset += size
    assert offset == len(data)
    return np.concatenate(output)


def _parts(length, block):
    return [block] * (length // block) + ([length % block] if length % block else [])


@pytest.mark.parametrize("source,target", [
    (48_000, 44_100), (44_100, 48_000), (48_000, 16_000), (8_000, 48_000),
    (32_000, 48_000), (96_000, 48_000), (192_000, 48_000), (44_101, 48_000),
    (48_000, 8_000), (384_000, 8_000),
])
def test_sample_count_and_waveform_do_not_depend_on_packet_boundaries(source, target):
    data = np.random.default_rng(40).normal(0, 0.1, 8193).astype(np.float32)
    whole = _stream(data, source, target, [len(data)])
    fragmented = _stream(data, source, target, _parts(len(data), 127))
    assert len(fragmented) == len(data) * target // source
    np.testing.assert_array_equal(fragmented, whole)
    assert fragmented.dtype == np.float32


def test_1024_sample_remote_packets_do_not_accumulate_rounding_error():
    converter = StreamingMonoResampler()
    total = 0
    for packet in range(100):
        total += len(converter.process(np.zeros(1024, dtype=np.float32), 48_000, 44_100))
        # Previously each packet became 941 samples and gained 20 samples
        # after these 100 packets, rather than preserving cumulative time.
        assert total == ((packet + 1) * 1024 * 44_100) // 48_000


def test_equal_rates_preserve_exact_samples_and_callback_boundaries():
    data = np.array([0.0, 1.0, -1.0, 0.1234567], dtype=np.float32)
    result = StreamingMonoResampler().process(data, 48_000, 48_000)
    np.testing.assert_array_equal(result, data)
    assert np.shares_memory(result, data)
    frames = MicrophonePCMEncoder(48_000, 960).process(data, 48_000)
    assert len(frames) == 1
    np.testing.assert_array_equal(frames[0], data)


def test_empty_packet_does_not_flush_or_reset_filter_history():
    converter = StreamingMonoResampler()
    first = converter.process(np.ones(1024, dtype=np.float32), 48_000, 44_100)
    assert converter.process(np.empty(0, dtype=np.float32), 48_000, 44_100).size == 0
    second = converter.process(np.ones(1024, dtype=np.float32), 48_000, 44_100)
    reference = StreamingMonoResampler().process(np.ones(2048, dtype=np.float32), 48_000, 44_100)
    np.testing.assert_array_equal(np.concatenate([first, second]), reference)


@pytest.mark.parametrize("change", ["reset", "source", "target"])
def test_reset_or_rate_change_cannot_leak_old_speech(change):
    converter = StreamingMonoResampler()
    converter.process(np.ones(1024, dtype=np.float32), 48_000, 44_100)
    source, target = 48_000, 44_100
    if change == "reset":
        converter.reset()
    elif change == "source":
        source = 32_000
    else:
        target = 16_000
    result = converter.process(np.zeros(1024, dtype=np.float32), source, target)
    assert np.count_nonzero(result) == 0
    assert len(result) == 1024 * target // source


def _tone(rate, frequency, duration=0.25):
    return np.sin(2 * np.pi * frequency * np.arange(int(rate * duration)) / rate).astype(np.float32)


def _amplitude(data, rate, frequency):
    # Fit both phases and DC; a fixed causal delay must not masquerade as
    # attenuation, and a partial sine period must not distort the estimate.
    phase = 2 * np.pi * frequency * np.arange(len(data)) / rate
    coefficients = np.linalg.lstsq(
        np.column_stack([np.sin(phase), np.cos(phase), np.ones(len(data))]), data, rcond=None,
    )[0]
    return np.hypot(*coefficients[:2])


@pytest.mark.parametrize("source,target,frequency", [
    (48_000, 44_100, 100), (48_000, 44_100, 1_000), (48_000, 44_100, 18_000),
    (48_000, 16_000, 6_000), (8_000, 48_000, 3_000), (44_100, 48_000, 1_000),
])
def test_passband_tones_keep_their_level_and_frequency(source, target, frequency):
    signal = _tone(source, frequency)
    result = _stream(signal, source, target, _parts(len(signal), 233))[int(target * 0.01):]
    assert _amplitude(result, target, frequency) == pytest.approx(1.0, abs=0.005)


def test_downsampling_rejects_out_of_band_audio_instead_of_aliasing_into_voice():
    signal = _tone(48_000, 13_000)
    result = _stream(signal, 48_000, 16_000, _parts(len(signal), 1024))[160:]
    # 13 kHz becomes a false 3 kHz tone without a proper anti-alias filter.
    assert _amplitude(result, 16_000, 3_000) < 0.0003


def test_upsampling_does_not_add_strong_image_frequencies():
    signal = _tone(8_000, 1_000)
    result = _stream(signal, 8_000, 48_000, _parts(len(signal), 160))[480:]
    assert _amplitude(result, 48_000, 7_000) < 0.0003
    assert _amplitude(result, 48_000, 9_000) < 0.0003


@pytest.mark.parametrize("source,target", [(8_000, 48_000), (48_000, 44_100), (48_000, 16_000)])
def test_filter_delay_is_bounded_by_four_ms(source, target):
    signal = np.zeros(2048, dtype=np.float32)
    signal[500] = 1
    result = StreamingMonoResampler().process(signal, source, target)
    delay = np.argmax(np.abs(result)) / target - 500 / source
    assert 0 <= delay <= 0.004 + 1 / target


@pytest.mark.parametrize("rate", [0, -1, 7999, 384001])
def test_malformed_rate_metadata_has_a_bounded_failure(rate):
    with pytest.raises(ValueError):
        StreamingMonoResampler().process(np.zeros(10, dtype=np.float32), rate, 48_000)


def test_converted_microphone_accumulates_only_complete_twenty_ms_frames():
    encoder = MicrophonePCMEncoder(48_000, 960)
    signal = _tone(44_100, 1_000, duration=0.04)
    frames = []
    for offset in range(0, len(signal), 73):
        frames.extend(encoder.process(signal[offset:offset + 73], 44_100))
    assert [len(frame) for frame in frames] == [960, 960]
    assert encoder.reset() == 0


def test_microphone_discontinuity_discards_partial_frame_and_filter_history():
    encoder = MicrophonePCMEncoder(48_000, 960)
    assert encoder.process(np.ones(441, dtype=np.float32), 44_100) == []
    assert encoder.reset() == 480
    frames = encoder.process(np.zeros(882, dtype=np.float32), 44_100)
    assert len(frames) == 1 and len(frames[0]) == 960
    assert np.count_nonzero(frames[0]) == 0


def test_backlog_skip_preserves_fractional_clock_and_counts_partial_audio():
    encoder = MicrophonePCMEncoder(48_000, 960)
    assert encoder.process(np.ones(441, dtype=np.float32), 44_101) == []
    skipped = encoder.skip(882, 44_101)
    # No frame has been emitted: the partial conversion and the dropped raw
    # capture together occupy this much of the call's absolute sample clock.
    assert skipped == (441 + 882) * 48_000 // 44_101
    result = encoder.process(np.zeros(882, dtype=np.float32), 44_101)
    assert [len(frame) for frame in result] == [960]
    assert np.count_nonzero(result[0]) == 0


def test_backlog_counts_actual_capture_lengths_instead_of_assuming_twenty_ms():
    session, _ = _session()
    for size in (448, 333, 777, 882, 100):
        session._mic_queue.put(np.zeros(size, dtype=np.float32))
    result, dropped, skipped = session._dequeue_fresh_microphone_frame()
    assert dropped == 2 and skipped == 448 + 333
    assert result.size == 777


class _Socket:
    def __init__(self):
        self.events = []
        self.session = None

    def emit(self, name, payload):
        self.events.append((name, payload))
        self.session._stop_event.set()


def _session():
    socket = _Socket()
    # Inject a sentinel: these tests never enumerate or open real devices.
    session = CallAudioSession(socket, CallAudioConfig(session="synthetic"), sounddevice_module=object())
    socket.session = session
    return session, socket


def test_microphone_callback_copies_float32_without_filtering_or_quantizing(monkeypatch):
    import core.call_audio as call_audio

    def unexpected(*args, **kwargs):
        pytest.fail("The realtime capture callback must not run a converter")

    monkeypatch.setattr(call_audio, "MicrophonePCMEncoder", unexpected)
    session, socket = _session()
    owner = 'a' * 32
    assert session.set_media_epoch(owner)
    signal = np.array([[0.1234567], [-0.2345678]], dtype=np.float32)
    session._on_microphone_frame(44_100)(signal, len(signal), None, None)
    captured = session._mic_queue.get_nowait()
    signal[:] = 0
    assert isinstance(captured, CapturedPCM) and captured.epoch == owner
    assert captured.samples.dtype == np.float32
    np.testing.assert_array_equal(captured.samples, [np.float32(0.1234567), np.float32(-0.2345678)])
    assert socket.events == []


@pytest.mark.parametrize("rate,frames", [(48_000, 960), (16_000, 320), (44_100, 882)])
def test_sender_converts_captured_audio_to_one_transport_frame(rate, frames):
    session, socket = _session()
    session._input_rate = rate
    signal = np.full((frames, 1), 0.25, dtype=np.float32)
    session._on_microphone_frame(rate)(signal, frames, None, None)
    session._send_microphone_loop()
    name, payload = socket.events[0]
    assert name == "call:audio:mic"
    assert payload["sampleRate"] == CALL_SAMPLE_RATE
    pcm = np.frombuffer(base64.b64decode(payload["pcm"]), dtype="<i2")
    assert len(pcm) == CALL_FRAME_SAMPLES
    assert np.all(np.abs(pcm[-480:] - 8191) <= 1)


def test_remote_player_uses_one_continuous_clock_for_successive_packets():
    session, _ = _session()
    session._output_rate = 44_100
    session._output_stream = object()
    chunks = []

    class _Buffer:
        underruns = 0

        def push(self, samples):
            chunks.append(samples.copy())
            if len(chunks) == 2:
                session._stop_event.set()

    session._output_buffer = _Buffer()
    for _ in range(2):
        session.enqueue_remote_audio(np.full(1024, 8192, dtype="<i2").tobytes(), 48_000)
    session._play_remote_loop()
    assert [len(chunk) for chunk in chunks] == [940, 941]
    assert np.max(np.abs(chunks[-1] - 0.25)) < 0.00001


def test_output_reopen_discards_filter_history_even_at_the_same_rate():
    from tests.test_call_audio_session import _SoundDevice, _OutputStream, _Socket as Socket

    session = CallAudioSession(Socket(), CallAudioConfig(session="synthetic"), sounddevice_module=_SoundDevice())
    session._output_stream = _OutputStream()
    session._output_resampler.process(np.ones(1024, dtype=np.float32), 44_100, 48_000)
    try:
        session._reopen_output_exclusive()
        result = session._output_resampler.process(np.zeros(1024, dtype=np.float32), 44_100, 48_000)
        assert np.count_nonzero(result) == 0
    finally:
        session.stop()
