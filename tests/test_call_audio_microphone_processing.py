"""Echo cancellation and noise suppression in the call's microphone path.

These run the real WebRTC module (livekit), not a stub: the previous canceller
passed tests that never measured what a listener hears, so these assert on
the audio that actually leaves through call:audio:mic.
"""

import base64
import threading

import numpy as np

from core.call_audio import (
    CALL_FRAME_SAMPLES, CALL_SAMPLE_RATE, CallAudioConfig, CallAudioSession,
)
from core.call_audio_processing import CallAudioProcessor
from tests.test_call_audio_session import _SoundDevice, _Socket, _wait_for


def _rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def _decoded(sio):
    sent = b"".join(base64.b64decode(event[1]["pcm"]) for event in sio.events)
    return np.frombuffer(sent, dtype="<i2").astype(np.float32) / 32768.0


def _send(config, mic, far=None):
    """Push *mic* (and *far*, as the speaker) through the real send loop."""
    sio = _Socket()
    session = CallAudioSession(sio, config)
    session._restart_echo_reference()  # what start() does when the mic opens
    thread = threading.Thread(target=session._send_microphone_loop, daemon=True)
    thread.start()
    for i in range(0, len(mic), CALL_FRAME_SAMPLES):
        if far is not None:
            session._echo_reference_tap.append((far[i:i + CALL_FRAME_SAMPLES], CALL_SAMPLE_RATE))
        session._mic_queue.put(mic[i:i + CALL_FRAME_SAMPLES].copy())
        _wait_for(lambda: session._mic_queue.qsize() == 0)
    session._stop_event.set()
    thread.join(timeout=2)
    return _decoded(sio)


def _echo_case(seconds=6, delay=2400, gain=0.8):
    rng = np.random.default_rng(7)
    far = (rng.standard_normal(CALL_SAMPLE_RATE * seconds) * 0.1).astype(np.float32)
    mic = np.zeros_like(far)
    mic[delay:] = far[:-delay] * gain
    return far, mic


def test_no_processor_until_the_microphone_opens_and_none_when_both_are_off():
    session = CallAudioSession(_Socket(), CallAudioConfig(session="s", echo_cancellation=True))
    assert session._processor is None  # the ringing monitor never builds one
    session._restart_echo_reference()
    assert isinstance(session._processor, CallAudioProcessor)

    plain = CallAudioSession(_Socket(), CallAudioConfig(session="s"))
    plain._restart_echo_reference()
    assert plain._processor is None


def test_send_loop_removes_the_echo_of_what_the_speaker_played():
    far, mic = _echo_case()
    plain = _send(CallAudioConfig(session="s"), mic, far)
    cancelled = _send(CallAudioConfig(session="s", echo_cancellation=True), mic, far)
    tail = slice(-CALL_SAMPLE_RATE, None)
    assert _rms(plain[tail]) > 0.9 * _rms(mic[tail])
    # 0.1 is -20 dB; the old numpy canceller managed about -10 dB here.
    assert _rms(cancelled[tail]) < 0.1 * _rms(plain[tail])


def test_the_speaker_is_tapped_only_when_echo_cancellation_is_on():
    def taps(echo_cancellation):
        session = CallAudioSession(
            _Socket(), CallAudioConfig(session="s", echo_cancellation=echo_cancellation))
        session._output_buffer.configure(CALL_SAMPLE_RATE)
        session._on_output_frames(np.zeros((480, 1), dtype=np.float32), 480, None, None)
        return len(session._echo_reference_tap)

    assert taps(True) == 1
    assert taps(False) == 0


def test_send_loop_lowers_steady_noise_when_enabled():
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(CALL_SAMPLE_RATE * 4) * 0.02).astype(np.float32)
    plain = _send(CallAudioConfig(session="s"), noise)
    suppressed = _send(CallAudioConfig(session="s", noise_suppression=True), noise)
    tail = slice(-CALL_SAMPLE_RATE, None)
    assert _rms(plain[tail]) > 0.9 * _rms(noise[tail])
    assert _rms(suppressed[tail]) < 0.6 * _rms(plain[tail])


def test_answering_a_call_drops_the_reference_played_while_ringing():
    # The speaker runs from the first ring; the reference must start with the
    # microphone, so whatever the tap held while ringing is thrown away.
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(session="s", input_device_name="Mic", output_device_name="Speaker",
                        echo_cancellation=True),
        sounddevice_module=sounddevice,
    )
    session.start_output_only()
    for _ in range(40):
        session._echo_reference_tap.append((np.ones(480, dtype=np.float32), CALL_SAMPLE_RATE))

    seen = {}
    real_start = sounddevice.InputStream

    def input_stream(**kwargs):
        stream = real_start(**kwargs)
        start = stream.start

        def checked_start():
            seen["tap"] = len(session._echo_reference_tap)
            seen["processor"] = session._processor is not None
            start()
        stream.start = checked_start
        return stream

    sounddevice.InputStream = input_stream
    try:
        session.start()
    finally:
        session.stop()

    assert seen == {"tap": 0, "processor": True}
    assert session._processor is None  # stop() lets it go


def test_stream_latency_reads_what_portaudio_reports():
    class _Stream:
        latency = 0.012

    class _Duplex:
        latency = (0.02, 0.03)

    assert CallAudioSession._stream_latency(_Stream()) == 0.012
    assert CallAudioSession._stream_latency(_Duplex()) == 0.02
    assert CallAudioSession._stream_latency(None) == 0.0
    assert CallAudioSession._stream_latency(type("S", (), {"latency": "low"})()) == 0.0


def test_reference_resampling_keeps_the_clock_across_odd_device_periods():
    # 44.1 kHz in 448-frame periods: rounding each chunk on its own gains 0.34
    # samples per chunk (~780 ppm) and the reference slides off the microphone.
    from core.call_audio import _ReferenceResampler

    resampler = _ReferenceResampler()
    chunk = np.zeros(448, dtype=np.float32)
    total = sum(len(resampler.process(chunk, 44_100)) for _ in range(2000))
    assert abs(total - 2000 * 448 * CALL_SAMPLE_RATE / 44_100) <= 1


def test_reference_resampler_passes_the_call_rate_through_and_restarts_on_a_rate_change():
    from core.call_audio import _ReferenceResampler

    resampler = _ReferenceResampler()
    data = np.arange(960, dtype=np.float32)
    assert np.array_equal(resampler.process(data, CALL_SAMPLE_RATE), data)
    first = len(resampler.process(np.zeros(441, dtype=np.float32), 44_100))
    resampler.process(np.zeros(480, dtype=np.float32), 32_000)
    assert len(resampler.process(np.zeros(441, dtype=np.float32), 44_100)) == first


def test_the_saved_call_settings_reach_the_audio_session(monkeypatch):
    # Voice and video calls both build their audio here.
    import core.call_audio as call_audio
    from main_window.calls import CallsMixin

    built = []

    class _Session:
        def __init__(self, sio, config, **kwargs):
            built.append(config)

    monkeypatch.setattr(call_audio, "CallAudioSession", _Session)

    class _WS:
        sio = object()
        instance_name = "acct"

    class _Stub:
        ws = _WS()
        token = "acct:secret"
        settings = {"call_audio_devices": {"echo_cancellation": False, "noise_suppression": True}}

        def call_audio_device(self, kind):
            return ""

    CallsMixin._build_call_audio_session(_Stub())
    assert built[0].echo_cancellation is False
    assert built[0].noise_suppression is True
    # A missing key follows the shipped defaults: echo cancellation on.
    _Stub.settings = {"call_audio_devices": {}}
    CallsMixin._build_call_audio_session(_Stub())
    assert built[1].echo_cancellation is True
    assert built[1].noise_suppression is False
