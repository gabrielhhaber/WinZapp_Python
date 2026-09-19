import base64
import time

import numpy as np

from core.call_audio import (
    CALL_DEVICE_LATENCY_SECONDS,
    CALL_FRAME_SAMPLES,
    CALL_MIC_TARGET_BACKLOG_FRAMES,
    CallAudioConfig,
    CallAudioSession,
)


class _Socket:
    def __init__(self):
        self.events = []

    def emit(self, name, payload):
        self.events.append((name, payload))


class _InputStream:
    def __init__(self, callback):
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self):
        self.started = True
        samples = np.array([[0.25], [-0.25], [0.0]], dtype=np.float32)
        self.callback(samples, len(samples), None, None)

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class _OutputStream:
    def __init__(self):
        self.started = False
        self.closed = False
        self.writes = []

    def start(self):
        self.started = True

    def write(self, samples):
        self.writes.append(np.asarray(samples).copy())

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


class _Defaults:
    device = (0, 1)


class _WasapiSettings:
    def __init__(self, *, exclusive=False, auto_convert=False):
        self.exclusive = exclusive
        self.auto_convert = auto_convert


class _SoundDevice:
    default = _Defaults()
    WasapiSettings = _WasapiSettings

    def __init__(self):
        self.input_streams = []
        self.output_streams = []
        self.devices = [
            {
                "name": "Mic",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000,
                "hostapi": 0,
            },
            {
                "name": "Speaker",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000,
                "hostapi": 0,
            },
        ]

    def query_devices(self, device=None):
        if device is None:
            return self.devices
        return self.devices[int(device)]

    def query_hostapis(self, index=None):
        return {"name": "Windows WASAPI"}

    def InputStream(self, **kwargs):
        stream = _InputStream(kwargs["callback"])
        self.input_streams.append((kwargs, stream))
        return stream

    def OutputStream(self, **kwargs):
        stream = _OutputStream()
        self.output_streams.append((kwargs, stream))
        return stream


def _wait_for(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_call_audio_session_sends_python_microphone_pcm_to_socket():
    sio = _Socket()
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp", input_device_name="Mic", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )

    session.start()

    assert _wait_for(lambda: any(name == "call:audio:mic" for name, _ in sio.events))
    name, payload = next(event for event in sio.events if event[0] == "call:audio:mic")
    assert name == "call:audio:mic"
    assert payload["session"] == "winzapp"
    assert payload["sampleRate"] == 48000
    assert payload["encoding"] == "base64"
    assert base64.b64decode(payload["pcm"])

    session.stop()
    assert sio.events[-1] == ("call:audio:stop", {"session": "winzapp"})


def test_call_audio_session_plays_remote_pcm_on_python_output_device():
    sio = _Socket()
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )
    session.start()

    samples = np.full(CALL_FRAME_SAMPLES * 3, 0.1, dtype=np.float32)
    pcm = (samples * 32767).astype("<i2").tobytes()
    session.enqueue_remote_audio(pcm, 48000)

    output_stream = sounddevice.output_streams[0][1]
    assert _wait_for(lambda: bool(output_stream.writes))
    assert output_stream.writes[0].shape == (CALL_FRAME_SAMPLES, 1)

    session.stop()


def test_call_audio_session_prebuffers_remote_pcm_before_playback():
    sio = _Socket()
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )
    session.start_output_only()
    output_stream = sounddevice.output_streams[0][1]

    samples = np.full(CALL_FRAME_SAMPLES, 0.1, dtype=np.float32)
    pcm = (samples * 32767).astype("<i2").tobytes()

    session.enqueue_remote_audio(pcm, 48000)
    session.enqueue_remote_audio(pcm, 48000)
    time.sleep(0.02)
    assert output_stream.writes == []

    session.enqueue_remote_audio(pcm, 48000)
    assert _wait_for(lambda: bool(output_stream.writes))
    assert output_stream.writes[0].shape == (CALL_FRAME_SAMPLES, 1)

    session.stop()


def test_call_audio_session_can_start_receive_only_without_opening_microphone():
    sio = _Socket()
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp", input_device_name="Mic", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )

    session.start_output_only()

    assert session.output_running is True
    assert session.running is False
    assert sounddevice.output_streams[0][1].started is True
    assert sounddevice.input_streams == []
    assert ("call:audio:start", {"session": "winzapp"}) in sio.events
    assert not any(name == "call:audio:mic" for name, _ in sio.events)

    pcm = (np.array([0.1, -0.1, 0.0], dtype=np.float32) * 32767).astype("<i2").tobytes()
    session.enqueue_remote_audio(pcm, 48000)
    assert _wait_for(lambda: bool(sounddevice.output_streams[0][1].writes))

    session.stop()


def test_receive_only_session_promotes_to_full_duplex_on_answer():
    sio = _Socket()
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp", input_device_name="Mic", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )

    session.start_output_only()
    output_stream = sounddevice.output_streams[0][1]
    session.start()

    assert session.running is True
    assert sounddevice.output_streams[0][1] is output_stream
    assert len(sounddevice.output_streams) == 1
    assert len(sounddevice.input_streams) == 1
    assert _wait_for(lambda: any(name == "call:audio:mic" for name, _ in sio.events))

    session.stop()


def test_microphone_backlog_skips_old_audio_instead_of_adding_delay():
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(session="winzapp"),
        sounddevice_module=_SoundDevice(),
    )
    frames = [bytes([marker]) * 8 for marker in range(8)]
    for frame in frames:
        session._mic_queue.put_nowait(frame)

    pcm, dropped = session._dequeue_fresh_microphone_frame()

    assert pcm == frames[5]
    assert dropped == 5
    assert session._mic_queue.qsize() == CALL_MIC_TARGET_BACKLOG_FRAMES



def test_call_audio_prefers_native_device_rate_and_safe_driver_latency():
    sio = _Socket()
    sounddevice = _SoundDevice()
    sounddevice.devices[0]["default_samplerate"] = 44100
    sounddevice.devices[1]["default_samplerate"] = 44100
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp"),
        sounddevice_module=sounddevice,
    )

    session.start()

    input_kwargs = sounddevice.input_streams[0][0]
    output_kwargs = sounddevice.output_streams[0][0]
    assert input_kwargs["device"] == 0
    assert output_kwargs["device"] == 1
    assert input_kwargs["samplerate"] == 44100
    assert output_kwargs["samplerate"] == 44100
    assert input_kwargs["latency"] == CALL_DEVICE_LATENCY_SECONDS
    assert output_kwargs["latency"] == CALL_DEVICE_LATENCY_SECONDS

    session.stop()


def test_windows_wasapi_settings_are_explicitly_shared(monkeypatch):
    import core.call_audio as call_audio

    monkeypatch.setattr(call_audio.sys, "platform", "win32")
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(session="winzapp"),
        sounddevice_module=sounddevice,
    )

    input_settings = session._stream_extra_settings(0, input_device=True)
    output_settings = session._stream_extra_settings(1, input_device=False)

    assert input_settings.exclusive is False
    assert input_settings.auto_convert is True
    assert output_settings.exclusive is False
    assert output_settings.auto_convert is True
