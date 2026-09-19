import base64
import time

import numpy as np

from core.call_audio import CallAudioConfig, CallAudioSession


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


class _SoundDevice:
    def __init__(self):
        self.input_streams = []
        self.output_streams = []
        self.devices = [
            {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000},
            {"name": "Speaker", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000},
        ]

    def query_devices(self, device=None):
        if device is None:
            return self.devices
        return self.devices[int(device)]

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

    pcm = (np.array([0.1, -0.1, 0.0], dtype=np.float32) * 32767).astype("<i2").tobytes()
    session.enqueue_remote_audio(pcm, 48000)

    output_stream = sounddevice.output_streams[0][1]
    assert _wait_for(lambda: bool(output_stream.writes))
    assert output_stream.writes[0].shape == (3, 1)

    session.stop()
