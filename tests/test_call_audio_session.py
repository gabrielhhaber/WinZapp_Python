import base64
import time

import numpy as np
import pytest

from core.call_audio import (
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
        # PortAudio's OutputStream.write() returns a bool signalling an
        # underrun on that call; tests flip this to exercise the counter.
        self.report_underflow = False

    def start(self):
        self.started = True

    def write(self, samples):
        self.writes.append(np.asarray(samples).copy())
        return self.report_underflow

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

    def __init__(self, *, refuse_exclusive=False, is_windows=True):
        self.input_streams = []
        self.output_streams = []
        # Simulates a device that refuses exclusive access (e.g. already held
        # exclusively by another application): opening fails only when the
        # caller asked for exclusive=True.
        self.refuse_exclusive = refuse_exclusive
        self.is_windows = is_windows
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
        self._maybe_refuse(kwargs.get("extra_settings"))
        stream = _InputStream(kwargs["callback"])
        self.input_streams.append((kwargs, stream))
        return stream

    def OutputStream(self, **kwargs):
        self._maybe_refuse(kwargs.get("extra_settings"))
        stream = _OutputStream()
        self.output_streams.append((kwargs, stream))
        return stream

    def _maybe_refuse(self, extra_settings):
        if self.refuse_exclusive and extra_settings is not None and extra_settings.exclusive:
            raise RuntimeError("device refused exclusive access")


class _NoMicrophoneSoundDevice(_SoundDevice):
    """The speaker opens fine; the microphone is held by another application."""

    def InputStream(self, **kwargs):
        raise RuntimeError("microphone is in use")


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
    assert output_stream.writes[0].shape == (CALL_FRAME_SAMPLES * 3, 1)

    session.stop()


def test_call_audio_session_writes_remote_pcm_immediately_without_prebuffering():
    """Diagnostic bisection (2026-09-20): a prebuffer/reservoir rewrite of
    _play_remote_loop was suspected of causing severe choppy/high-latency
    call audio on a real Bluetooth headset (draining several queued frames
    in a burst once "primed", relying on stream.write() to block for the
    right amount of time between them — which only holds if the device's own
    buffer is close to one frame deep). Reverting the whole file to main's
    original version fixed it; reintroducing sample-rate priority, "low"
    latency, exclusive mode and a wall-clock write-pacing layer on top of the
    reservoir version individually did not. So this loop stays exactly as
    simple as it was before any of that: one packet in, one write out,
    immediately, no reservoir to drain."""
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


def test_start_closes_the_output_it_opened_when_the_microphone_fails():
    """REGRESSION: start() opens the receive side first (so an answered call can
    reuse the ringing monitor's speaker), but its error path only closed the
    INPUT stream. An outgoing call whose microphone is taken by another app
    therefore stranded a live OutputStream and its player thread for the life
    of the process -- unreachable, since the session never becomes
    _call_audio_session. Under exclusive_mode that held the output device and
    silenced the screen reader until restart."""
    sio = _Socket()
    sounddevice = _NoMicrophoneSoundDevice()
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp", input_device_name="Mic", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )

    with pytest.raises(Exception):
        session.start()

    assert sounddevice.output_streams, "the speaker was opened before the mic failed"
    assert sounddevice.output_streams[0][1].closed is True
    assert session.output_running is False


def test_start_leaves_the_ringing_monitors_output_alone_when_the_microphone_fails():
    """The other half of the rule above: a speaker opened by the ringing
    monitor is not this call's to close. Its owner (_start_voice_call_audio)
    stops that session on the same failure, and closing it here too would be a
    double close."""
    sio = _Socket()
    sounddevice = _NoMicrophoneSoundDevice()
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp", input_device_name="Mic", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )

    session.start_output_only()
    output_stream = sounddevice.output_streams[0][1]

    with pytest.raises(Exception):
        session.start()

    assert output_stream.closed is False
    assert session.output_running is True

    session.stop()
    assert output_stream.closed is True


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



def test_call_audio_prefers_the_call_transport_rate_when_the_device_supports_it():
    """A device that can open at 48 kHz directly must not be forced through
    resampling just because its OS-reported default happens to be 44100 —
    that reintroduced audibly choppy call audio despite healthy delivery
    (see _candidate_rates)."""
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
    assert input_kwargs["samplerate"] == 48000
    assert output_kwargs["samplerate"] == 48000
    assert input_kwargs["latency"] == "low"
    assert output_kwargs["latency"] == "low"

    session.stop()


def test_call_audio_falls_back_to_native_rate_when_48k_is_refused():
    """An HFP-only Bluetooth microphone that cannot open at 48 kHz must still
    reach its own native rate (e.g. 8000/16000 Hz) rather than failing the
    call outright."""
    sio = _Socket()
    sounddevice = _SoundDevice()
    sounddevice.devices[0]["default_samplerate"] = 16000

    class _RefusingSoundDevice(_SoundDevice):
        def InputStream(self, **kwargs):
            if kwargs["samplerate"] == 48000:
                raise RuntimeError("device refuses 48000 Hz")
            return super().InputStream(**kwargs)

    sounddevice = _RefusingSoundDevice()
    sounddevice.devices[0]["default_samplerate"] = 16000
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp"),
        sounddevice_module=sounddevice,
    )

    session.start()

    input_kwargs = sounddevice.input_streams[0][0]
    assert input_kwargs["samplerate"] == 16000

    session.stop()


def test_exclusive_mode_default_is_disabled():
    """Both exclusive flags default off, per direction.

    Exclusive access was suspected, then ruled out, as the cause of the
    choppy-audio regression. It stays an opt-in because the two directions
    have very different costs: an exclusive MICROPHONE takes a device nothing
    else is using mid-call, while an exclusive SPEAKER silences every other
    application on it -- the screen reader included, for the whole call, which
    for WinZapp's users means losing the call window's own controls. Hence two
    checkboxes in settings_dialog.py, and a spoken warning on the output one.
    """
    sio = _Socket()
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )

    session.start_output_only()

    kwargs = sounddevice.output_streams[0][0]
    assert kwargs["extra_settings"] is not None
    assert kwargs["extra_settings"].exclusive is False

    session.stop()


def test_exclusive_mode_falls_back_to_shared_when_device_refuses(caplog):
    sio = _Socket()
    sounddevice = _SoundDevice(refuse_exclusive=True)
    session = CallAudioSession(
        sio,
        CallAudioConfig(
            session="winzapp", output_device_name="Speaker", exclusive_output=True
        ),
        sounddevice_module=sounddevice,
    )

    with caplog.at_level("INFO"):
        session.start_output_only()

    assert len(sounddevice.output_streams) == 1
    kwargs = sounddevice.output_streams[0][0]
    assert kwargs["extra_settings"].exclusive is False
    assert any(
        "exclusive mode unavailable, fell back to shared mode for output" in record.message
        for record in caplog.records
    )

    session.stop()


def test_exclusive_mode_disabled_never_attempts_exclusive():
    sio = _Socket()
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        sio,
        CallAudioConfig(
            session="winzapp", output_device_name="Speaker", exclusive_output=False
        ),
        sounddevice_module=sounddevice,
    )

    session.start_output_only()

    assert len(sounddevice.output_streams) == 1
    kwargs = sounddevice.output_streams[0][0]
    assert kwargs["extra_settings"].exclusive is False

    session.stop()


class _MultiHostApiSoundDevice(_SoundDevice):
    """Windows exposes one physical device once per host API.

    PortAudio enumerates them MME first, then DirectSound, then WASAPI, so a
    name lookup that takes the first match always lands on MME -- measured on
    a real Realtek device at 90 ms of input buffering against 3 ms for the
    very same hardware through WASAPI.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.devices = [
            {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0,
             "default_samplerate": 48000, "hostapi": 0},        # 0 MME
            {"name": "Speaker", "max_input_channels": 0, "max_output_channels": 2,
             "default_samplerate": 48000, "hostapi": 0},        # 1 MME
            {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0,
             "default_samplerate": 48000, "hostapi": 2},        # 2 WASAPI
            {"name": "Speaker", "max_input_channels": 0, "max_output_channels": 2,
             "default_samplerate": 48000, "hostapi": 2},        # 3 WASAPI
        ]

    def query_hostapis(self, index=None):
        return {0: {"name": "MME"}, 2: {"name": "Windows WASAPI"}}[int(index)]


def test_the_wasapi_twin_of_the_chosen_device_is_tried_first():
    """REGRESSION: every lookup resolved by name and took the first match, and
    MME comes first, so calls ran on MME -- ~90 ms of device buffering per
    direction against ~3 ms for the same hardware on WASAPI. This is plain
    SHARED-mode WASAPI (PortAudio's default), so nothing is taken away from
    any other application, the screen reader included."""
    sounddevice = _MultiHostApiSoundDevice()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(session="winzapp", input_device_name="Mic", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )

    session.start()

    assert sounddevice.input_streams[0][0]["device"] == 2    # WASAPI Mic
    assert sounddevice.output_streams[0][0]["device"] == 3   # WASAPI Speaker

    session.stop()


def test_a_device_with_no_wasapi_twin_still_opens_exactly_as_before():
    """The twin is a preference, not a requirement: the original index stays
    in the candidate list right behind it, so nothing regresses on hardware
    that WASAPI does not expose."""
    sounddevice = _SoundDevice()  # single host API, no twins
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(session="winzapp", input_device_name="Mic", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )

    session.start()

    assert sounddevice.input_streams[0][0]["device"] == 0
    assert sounddevice.output_streams[0][0]["device"] == 1

    session.stop()


class _CollidingNameSoundDevice(_SoundDevice):
    """Two WASAPI endpoints whose names share the 31-character prefix MME
    truncates at -- the partial match comes FIRST in enumeration order."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.devices = [
            {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0,
             "default_samplerate": 48000, "hostapi": 0},
            {"name": "Headset Earphone (Jabra Evolve", "max_input_channels": 0,
             "max_output_channels": 2, "default_samplerate": 48000, "hostapi": 0},
            {"name": "Headset Earphone (Jabra Evolve 75)", "max_input_channels": 0,
             "max_output_channels": 2, "default_samplerate": 48000, "hostapi": 2},
            {"name": "Headset Earphone (Jabra Evolve", "max_input_channels": 0,
             "max_output_channels": 2, "default_samplerate": 48000, "hostapi": 2},
        ]

    def query_hostapis(self, index=None):
        return {0: {"name": "MME"}, 2: {"name": "Windows WASAPI"}}[int(index)]


def test_an_exact_wasapi_name_beats_a_partial_match_found_earlier():
    """REGRESSION: the twin lookup returned the first device satisfying exact
    OR substring, so a partial match at a lower index beat an exact match
    further down. Two endpoints sharing MME's 31-character truncation would
    open the call on the wrong physical device, while the log reported
    hostapi=wasapi as if all were well -- and a blind user has no visual tell
    that the call is coming out of the monitor instead of the headset."""
    sounddevice = _CollidingNameSoundDevice()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(
            session="winzapp", output_device_name="Headset Earphone (Jabra Evolve"
        ),
        sounddevice_module=sounddevice,
    )

    session.start_output_only()

    # Index 3 is the exact match; index 2 only matches as a substring.
    assert sounddevice.output_streams[0][0]["device"] == 3

    session.stop()


def test_a_refused_exclusive_falls_back_to_shared_wasapi_not_to_mme():
    """REGRESSION (seen live in log.log, 2026-09-21): with exclusive_input on,
    the WASAPI twin refused exclusive access -- common -- and the MME entry
    behind it has no exclusive mode at all, so it opened "successfully" inside
    the exclusive pass. The shared pass, where the same WASAPI device would
    have opened, never ran: the microphone landed on MME at ~90 ms instead of
    shared WASAPI at ~3 ms, while the log honestly reported exclusive=False."""
    sounddevice = _MultiHostApiSoundDevice(refuse_exclusive=True)
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(
            session="winzapp", input_device_name="Mic", output_device_name="Speaker",
            exclusive_input=True, exclusive_output=True,
        ),
        sounddevice_module=sounddevice,
    )

    session.start()

    input_kwargs = sounddevice.input_streams[0][0]
    output_kwargs = sounddevice.output_streams[0][0]
    assert input_kwargs["device"] == 2       # WASAPI Mic, not MME (0)
    assert output_kwargs["device"] == 3      # WASAPI Speaker, not MME (1)
    assert input_kwargs["extra_settings"].exclusive is False
    assert output_kwargs["extra_settings"].exclusive is False

    session.stop()


def test_an_accepted_exclusive_still_opens_exclusively_on_wasapi():
    """The other half: when the device does accept exclusive access, the
    exclusive pass opens it, on the WASAPI twin."""
    sounddevice = _MultiHostApiSoundDevice()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(
            session="winzapp", input_device_name="Mic", output_device_name="Speaker",
            exclusive_input=True,
        ),
        sounddevice_module=sounddevice,
    )

    session.start()

    input_kwargs = sounddevice.input_streams[0][0]
    assert input_kwargs["device"] == 2
    assert input_kwargs["extra_settings"].exclusive is True
    # exclusive_output was left off, so the speaker is shared.
    assert sounddevice.output_streams[0][0]["extra_settings"].exclusive is False

    session.stop()
