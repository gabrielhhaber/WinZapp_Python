"""Mono/stereo call contracts using only synthetic PCM and injected devices."""
from collections import deque

import numpy as np

from core.call_audio import CallAudioConfig, CallAudioSession
from core.call_audio_output import render_output


def _session():
    return CallAudioSession(object(), CallAudioConfig(session="synthetic"),
                            sounddevice_module=object())


def test_stereo_capture_preserves_frame_axis_and_copies_borrowed_memory():
    session = _session()
    data = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32)
    session._on_microphone_frame(48000)(data, 2, None, None)
    captured = session._mic_queue.get_nowait().samples
    data[:] = 0
    np.testing.assert_array_equal(captured, np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32))


def test_backlog_skip_counts_frames_independently_of_channel_count():
    session = _session()
    for frames in (448, 333, 777, 882, 100):
        session._mic_queue.put(np.zeros((frames, 2), dtype=np.float32))
    data, dropped, skipped = session._dequeue_fresh_microphone_frame()
    assert dropped == 2
    assert skipped == 448 + 333
    assert data.shape == (777, 2)


def test_stereo_output_writes_both_columns_but_drains_and_taps_only_once():
    calls = []
    class Buffer:
        def fill(self, view, *, owner):
            calls.append(owner)
            view[:] = np.linspace(-0.3, 0.3, len(view))
            return True
    out = np.full((32, 2), 7, dtype=np.float32)
    tap = deque()
    owner = object()
    render_output(out, Buffer(), owner, 48000, tap, True)
    assert calls == [owner]
    np.testing.assert_array_equal(out[:, 0], out[:, 1])
    assert len(tap) == 1
    np.testing.assert_array_equal(tap[0][0], out[:, 0])
    assert tap[0][0].ndim == 1

import base64
import pytest
from core.call_audio_channels import MicrophoneDownmix, shared_channel_counts


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("outcome", ["retired", "error", "tap_error"])
def test_output_failure_paths_define_every_column(channels, outcome):
    class Buffer:
        def fill(self, view, *, owner):
            view[:] = 0.25
            if outcome == "error":
                raise RuntimeError("synthetic")
            return outcome != "retired"
    class BadTap:
        def append(self, item):
            raise RuntimeError("synthetic tap")
    out = np.full((13, channels), 7, dtype=np.float32)
    render_output(out, Buffer(), object(), 48000, BadTap(), True)
    np.testing.assert_array_equal(out, 0.25 if outcome == "tap_error" else 0)


def test_retired_stereo_callback_does_not_consume_new_owner_audio():
    from core.call_audio import _OutputJitterBuffer
    buffer = _OutputJitterBuffer(48000)
    old, current = object(), object()
    buffer.set_owner(current)
    buffer.push(np.ones(4800, dtype=np.float32) * 0.25)
    before = buffer.buffered_samples
    out = np.full((960, 2), 7, dtype=np.float32)
    tap = deque()
    render_output(out, buffer, old, 48000, tap, True)
    np.testing.assert_array_equal(out, 0)
    assert buffer.buffered_samples == before
    assert not tap


def _tone(frames, rate=48000):
    return (0.4 * np.sin(2 * np.pi * 400 * np.arange(frames) / rate)).astype(np.float32)


@pytest.mark.parametrize("side", [0, 1])
def test_persistently_empty_side_keeps_live_channel_level_after_observation(side):
    signal = _tone(24000)
    data = np.zeros((len(signal), 2), dtype=np.float32)
    data[:, side] = signal
    result = MicrophoneDownmix().process(data, 48000)
    np.testing.assert_allclose(result[-4800:], signal[-4800:], atol=1e-7)
    assert result.shape == signal.shape
    assert np.max(np.abs(np.diff(result))) < 0.03


def test_downmix_decisions_and_ramp_do_not_depend_on_callback_boundaries():
    signal = _tone(24000)
    data = np.column_stack((signal, np.zeros_like(signal)))
    whole = MicrophoneDownmix().process(data, 48000)
    mixer = MicrophoneDownmix()
    pieces = [mixer.process(data[i:i+137], 48000) for i in range(0, len(data), 137)]
    np.testing.assert_allclose(np.concatenate(pieces), whole, atol=1e-7)


def test_returning_second_source_is_mixed_after_a_full_observation_window():
    mixer = MicrophoneDownmix()
    signal = _tone(24000)
    mixer.process(np.column_stack((signal, np.zeros_like(signal))), 48000)
    other = _tone(24000) * 0.5
    result = mixer.process(np.column_stack((signal, other)), 48000)
    np.testing.assert_allclose(result[-4800:], (signal[-4800:] + other[-4800:]) / 2, atol=1e-7)


def test_dc_offset_is_not_mistaken_for_an_active_second_microphone():
    signal = _tone(24000)
    data = np.column_stack((signal, np.full_like(signal, 0.05)))
    result = MicrophoneDownmix().process(data, 48000)
    np.testing.assert_allclose(result[-4800:], signal[-4800:], atol=1e-7)


@pytest.mark.parametrize("change", ["reset", "rate"])
def test_downmix_does_not_carry_channel_selection_to_a_new_stream(change):
    mixer = MicrophoneDownmix()
    signal = _tone(24000)
    mixer.process(np.column_stack((signal, np.zeros_like(signal))), 48000)
    if change == "reset":
        mixer.reset()
    rate = 44100 if change == "rate" else 48000
    opposite = np.column_stack((np.zeros(100, dtype=np.float32), np.ones(100, dtype=np.float32)))
    np.testing.assert_array_equal(mixer.process(opposite, rate), np.full(100, 0.5, dtype=np.float32))


def test_both_silent_channels_retain_selection_without_inventing_sound():
    mixer = MicrophoneDownmix()
    signal = _tone(24000)
    mixer.process(np.column_stack((signal, np.zeros_like(signal))), 48000)
    np.testing.assert_array_equal(mixer.process(np.zeros((24000, 2), dtype=np.float32), 48000), 0)
    data = np.column_stack((signal[:100], np.zeros(100, dtype=np.float32)))
    np.testing.assert_array_equal(mixer.process(data, 48000), signal[:100])


def test_mono_mix_has_explicit_opposite_phase_cancellation():
    signal = _tone(4800)
    np.testing.assert_array_equal(MicrophoneDownmix().process(np.column_stack((signal, -signal)), 48000), 0)


@pytest.mark.parametrize("silent_gap", [0, 9600])
@pytest.mark.parametrize("blocksize", [1, 137, 960, 4800])
def test_short_speech_on_previously_empty_side_is_not_muted(silent_gap, blocksize):
    mixer = MicrophoneDownmix()
    signal = _tone(19200)
    mixer.process(np.column_stack((signal, np.zeros_like(signal))), 48000)
    if silent_gap:
        mixer.process(np.zeros((silent_gap, 2), dtype=np.float32), 48000)
    short = _tone(4800)
    incoming = np.column_stack((np.zeros_like(short), short))
    output = np.concatenate([
        mixer.process(incoming[i:i+blocksize], 48000)
        for i in range(0, len(short), blocksize)
    ])
    # New-side PCM is nonzero during the 20 ms ramp, then at normal
    # mixed level, without waiting for a 200 ms classification.
    assert np.dot(output[:960], output[:960]) > 0
    np.testing.assert_allclose(output[961:], short[961:] * 0.5, atol=1e-7)


@pytest.mark.parametrize("shape", [(31,), (31, 1)])
def test_mono_capture_values_are_unchanged(shape):
    data = np.linspace(-1, 1, 31, dtype=np.float32).reshape(shape)
    np.testing.assert_array_equal(MicrophoneDownmix().process(data, 48000), data.reshape(-1))


@pytest.mark.parametrize("data", [np.zeros((8, 8)), np.zeros((3, 0)), np.array([np.nan]), np.array([[0, np.inf]])])
def test_unsupported_or_nonfinite_capture_is_rejected(data):
    with pytest.raises(ValueError):
        MicrophoneDownmix().process(data, 48000)


@pytest.mark.parametrize("count,expected", [(1, (1,)), (2, (2, 1)), (8, (2, 1)), (None, (1,)), ("bad", (1,))])
def test_shared_candidates_are_capacity_checks_not_multichannel_mapping(count, expected):
    assert shared_channel_counts({"max_input_channels": count}, input_device=True) == expected


class Devices:
    """No threads or PortAudio; record requested formats and invoke callbacks manually."""
    class default:
        device = (0, 1)
    class WasapiSettings:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def __init__(self, *, api="Windows WASAPI", refuse_stereo=False, refuse_all=False):
        self.api, self.refuse_stereo, self.refuse_all = api, refuse_stereo, refuse_all
        self.attempts = []
        self.devices = [
            dict(name="Mic", max_input_channels=2, max_output_channels=0, hostapi=0, default_samplerate=48000),
            dict(name="Speaker", max_input_channels=0, max_output_channels=2, hostapi=0, default_samplerate=48000),
        ]

    def query_devices(self, index=None):
        return self.devices if index is None else self.devices[index]

    def query_hostapis(self, index=None):
        return {"name": self.api}

    def _open(self, direction, **kwargs):
        self.attempts.append((direction, kwargs))
        if self.refuse_all or (self.refuse_stereo and kwargs["channels"] == 2):
            raise RuntimeError("synthetic refusal")
        return object()

    def InputStream(self, **kwargs):
        return self._open("input", **kwargs)

    def OutputStream(self, **kwargs):
        return self._open("output", **kwargs)


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("refuse_stereo", [False, True])
def test_shared_device_opens_stereo_or_same_rate_mono_fallback(direction, refuse_stereo):
    devices = Devices(refuse_stereo=refuse_stereo)
    session = CallAudioSession(object(), CallAudioConfig(session="synthetic"), sounddevice_module=devices)
    getattr(session, "_open_" + direction + "_stream")()
    attempts = devices.attempts
    assert [kw["channels"] for _, kw in attempts] == ([2, 1] if refuse_stereo else [2])
    assert all(kw["samplerate"] == 48000 for _, kw in attempts)
    assert all(not kw["extra_settings"].exclusive and kw["extra_settings"].auto_convert for _, kw in attempts)


@pytest.mark.parametrize("exclusive,api", [(True, "Windows WASAPI"), (False, "MME")])
def test_exclusive_or_non_wasapi_preserves_mono_device_contract(exclusive, api):
    devices = Devices(api=api)
    session = CallAudioSession(object(), CallAudioConfig(session="synthetic", exclusive_input=exclusive,
                                                        exclusive_output=exclusive), sounddevice_module=devices)
    session._open_input_stream()
    session._open_output_stream()
    assert all(kw["channels"] == 1 for _, kw in devices.attempts)


@pytest.mark.parametrize("rate,frames", [(48000, 960), (44100, 882), (16000, 320)])
def test_stereo_sender_emits_one_mono_transport_frame_at_the_right_rate(rate, frames):
    class Socket:
        def emit(self, name, payload):
            self.payload = payload
            session._stop_event.set()
    socket = Socket()
    session = CallAudioSession(socket, CallAudioConfig(session="synthetic"), sounddevice_module=object())
    session._input_rate = rate
    signal = np.full((frames, 2), 0.25, dtype=np.float32)
    session._on_microphone_frame(rate)(signal, frames, None, None)
    session._send_microphone_loop()
    pcm = np.frombuffer(base64.b64decode(socket.payload["pcm"]), dtype="<i2")
    assert socket.payload["sampleRate"] == 48000
    assert len(pcm) == 960
    assert np.all(np.abs(pcm[-480:] - 8191) <= 1)
