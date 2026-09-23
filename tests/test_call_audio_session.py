import base64
import time
from pathlib import Path

import numpy as np
import pytest

from core.call_audio import (
    CALL_FRAME_SAMPLES,
    CALL_MIC_TARGET_BACKLOG_FRAMES,
    CALL_OUTPUT_FADE_MS,
    CALL_OUTPUT_MAX_BUFFER_MS,
    CALL_OUTPUT_PREBUFFER_MS,
    CALL_OUTPUT_RESUME_MS,
    CALL_OUTPUT_UNDERRUN_LOG_EVERY,
    CALL_SAMPLE_RATE,
    CallAudioConfig,
    CallAudioSession,
    _OutputJitterBuffer,
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
    """Playback is callback driven, so this fake plays only when asked.

    render() is what PortAudio's realtime thread does: hand the callback a
    block of whatever size the device's period happens to be and keep what it
    wrote. The block is prefilled with a sentinel rather than zeros for two
    reasons: PortAudio really does hand the callback an uninitialised buffer
    (writing every sample is now load-bearing, not a formality), and with
    zeros "the callback wrote silence" and "the callback never ran" look
    identical.
    """

    SENTINEL = 7.0

    def __init__(self, callback=None):
        self.started = False
        self.closed = False
        self.callback = callback
        self.writes = []

    def start(self):
        self.started = True

    def render(self, frames=CALL_FRAME_SAMPLES):
        outdata = np.full((frames, 1), self.SENTINEL, dtype=np.float32)
        self.callback(outdata, frames, None, None)
        assert not np.any(outdata == self.SENTINEL), "the callback left samples unwritten"
        self.writes.append(outdata.copy())
        return outdata[:, 0]

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
        stream = _OutputStream(kwargs.get("callback"))
        self.output_streams.append((kwargs, stream))
        return stream

    def _maybe_refuse(self, extra_settings):
        if self.refuse_exclusive and extra_settings is not None and extra_settings.exclusive:
            raise RuntimeError("device refused exclusive access")


class _NoMicrophoneSoundDevice(_SoundDevice):
    """The speaker opens fine; the microphone is held by another application."""

    def InputStream(self, **kwargs):
        raise RuntimeError("microphone is in use")


def _remote_pcm(frames=3, value=0.1):
    """PCM16 bytes for *frames* 20 ms frames of a constant level."""
    samples = np.full(CALL_FRAME_SAMPLES * frames, value, dtype=np.float32)
    return (samples * 32767).astype("<i2").tobytes()


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

    session.enqueue_remote_audio(_remote_pcm(3), 48000)

    output_stream = sounddevice.output_streams[0][1]
    assert _wait_for(lambda: session._output_buffer.buffered_samples >= CALL_FRAME_SAMPLES * 3)
    played = output_stream.render()
    assert played.shape == (CALL_FRAME_SAMPLES,)
    # The fade-in only touches the first few milliseconds; the rest is the
    # remote audio at its own level.
    assert played[-1] == pytest.approx(0.1, abs=0.01)

    session.stop()


def test_call_audio_session_prebuffers_before_playing_remote_audio():
    """The reservoir the popping fix is built on, from the session's side.

    A reservoir here is the component the 2026-09-20 bisection blamed, and
    that bisection never root-caused it (c78fe4c7). The reverted version
    (0f11af9c) was hard capped and dropped oldest as well, so what these
    tests pin is the part that is genuinely different: there is no blocking
    stream.write() and no wall-clock pacing left, only a callback PortAudio
    drives at the device's own rate."""
    sio = _Socket()
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        sio,
        CallAudioConfig(session="winzapp", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )
    session.start_output_only()
    output_kwargs, output_stream = sounddevice.output_streams[0]

    # blocksize=0: PortAudio serves the device's own period instead of a
    # made-up 20 ms one, and pacing comes from the reservoir.
    assert output_kwargs["blocksize"] == 0
    assert output_kwargs["latency"] == "low"
    assert callable(output_kwargs["callback"])

    # One packet is well under the prebuffer target, so nothing plays yet.
    session.enqueue_remote_audio(_remote_pcm(1), 48000)
    assert _wait_for(lambda: session._output_buffer.buffered_samples >= CALL_FRAME_SAMPLES)
    assert np.all(output_stream.render() == 0.0)

    # With the target reached, playback starts and nothing was discarded.
    session.enqueue_remote_audio(_remote_pcm(3), 48000)
    assert _wait_for(lambda: session._output_buffer.buffered_samples >= CALL_FRAME_SAMPLES * 3)
    assert np.any(output_stream.render() != 0.0)

    session.stop()


def test_stopping_the_call_empties_the_playback_reservoir():
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(session="winzapp", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )
    session.start_output_only()
    session.enqueue_remote_audio(_remote_pcm(3), 48000)
    assert _wait_for(lambda: session._output_buffer.buffered_samples > 0)

    session.stop()

    assert session._output_buffer.buffered_samples == 0


def test_answering_with_exclusive_output_restarts_the_reservoir_on_the_new_rate():
    """The ring's audio belongs to the stream that was just closed, and the
    exclusive device may not even run at the same rate."""
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(
            session="winzapp", input_device_name="Mic", output_device_name="Speaker",
            exclusive_output=True,
        ),
        sounddevice_module=sounddevice,
    )
    session.start_output_only(allow_exclusive=False)
    session.enqueue_remote_audio(_remote_pcm(3), 48000)
    assert _wait_for(lambda: session._output_buffer.buffered_samples > 0)

    session._reopen_output_exclusive()

    assert session._output_buffer.buffered_samples == 0
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

    session.enqueue_remote_audio(_remote_pcm(3), 48000)
    assert _wait_for(lambda: bool(sounddevice.output_streams[0][1].render().any()))

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
    _call_audio_session. Under exclusive_output that held the output device and
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


class _VirtualCableSoundDevice(_SoundDevice):
    """The real layout that broke a live call on 2026-09-21: the microphone's
    WASAPI entry refuses exclusive access, while an UNRELATED WASAPI input --
    a virtual audio cable, which carries no microphone signal -- accepts it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.devices = [
            {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0,
             "default_samplerate": 48000, "hostapi": 0},                     # 0 MME
            {"name": "Speaker", "max_input_channels": 0, "max_output_channels": 2,
             "default_samplerate": 48000, "hostapi": 0},                     # 1 MME
            {"name": "Line 1 (Virtual Audio Cable)", "max_input_channels": 1,
             "max_output_channels": 0, "default_samplerate": 48000, "hostapi": 2},  # 2
            {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0,
             "default_samplerate": 48000, "hostapi": 2},                     # 3 WASAPI mic
            {"name": "Speaker", "max_input_channels": 0, "max_output_channels": 2,
             "default_samplerate": 48000, "hostapi": 2},                     # 4
        ]

    def query_hostapis(self, index=None):
        return {0: {"name": "MME"}, 2: {"name": "Windows WASAPI"}}[int(index)]

    def InputStream(self, **kwargs):
        settings = kwargs.get("extra_settings")
        if kwargs.get("device") == 3 and settings is not None and settings.exclusive:
            raise RuntimeError("the microphone refuses exclusive access")
        return super().InputStream(**kwargs)


def test_a_refused_exclusive_never_wanders_to_an_unrelated_device():
    """REGRESSION (live call, 2026-09-21): with exclusive_input on and the
    microphone refusing exclusive access, the exclusive pass went on through
    the "try every device" safety sweep and opened "Line 1 (Virtual Audio
    Cable)" exclusively -- the peer heard silence for the whole call while
    frames kept flowing. A refused exclusive must fall back to the SAME device
    in shared mode; the sweep belongs to the shared pass only."""
    sounddevice = _VirtualCableSoundDevice()
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
    assert input_kwargs["device"] == 3                    # the microphone...
    assert input_kwargs["extra_settings"].exclusive is False   # ...shared
    assert all(kwargs["device"] != 2 for kwargs, _ in sounddevice.input_streams)

    session.stop()


def test_the_every_device_sweep_still_rescues_the_shared_pass():
    """The sweep is not removed, only kept out of the exclusive pass: when
    the named device cannot be found at all, shared mode still opens
    something rather than failing the call."""
    sounddevice = _VirtualCableSoundDevice()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(session="winzapp", input_device_name="Unplugged headset"),
        sounddevice_module=sounddevice,
    )
    candidates = list(session._candidate_devices(
        "Unplugged headset", input_device=True, include_fallbacks=True,
    ))
    assert 2 in candidates and 3 in candidates

    exclusive_only = list(session._candidate_devices(
        "Unplugged headset", input_device=True, include_fallbacks=False,
    ))
    assert 2 not in exclusive_only
    # A chosen device that does not resolve (unplugged) must not hand the
    # exclusive pass the system default either -- nor PortAudio's None, which
    # is that same default: the shared pass takes over instead.
    assert exclusive_only == []


def test_with_no_device_chosen_the_exclusive_pass_uses_the_default():
    session = CallAudioSession(
        _Socket(), CallAudioConfig(session="winzapp"),
        sounddevice_module=_VirtualCableSoundDevice(),
    )
    exclusive_only = list(session._candidate_devices(
        "", input_device=True, include_fallbacks=False,
    ))
    assert 0 in exclusive_only and None in exclusive_only


class _CableIsDefaultDefaults:
    device = (2, 1)


def test_a_refused_exclusive_never_moves_to_the_system_default_either():
    """Review finding: the exclusive pass still offered the SYSTEM DEFAULT
    after the chosen device. With the virtual cable as the default input and
    the microphone refusing exclusive access, the call opened the cable
    exclusively -- and on the output side, a headset refusing exclusive put
    the call exclusive on the default speakers, where the screen reader is.
    The chosen device falls back to itself, shared."""
    sounddevice = _VirtualCableSoundDevice()
    sounddevice.default = _CableIsDefaultDefaults()
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
    assert input_kwargs["device"] == 3
    assert input_kwargs["extra_settings"].exclusive is False
    assert all(kwargs["device"] != 2 for kwargs, _ in sounddevice.input_streams)

    session.stop()


def test_ringing_opens_the_speaker_shared_even_with_exclusive_output():
    """Review finding: an exclusive output opened while the call still rings
    takes the device from the ring tone and from the screen reader saying
    who is calling -- the call arrives with no signal at all."""
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(
            session="winzapp", input_device_name="Mic", output_device_name="Speaker",
            exclusive_output=True,
        ),
        sounddevice_module=sounddevice,
    )

    session.start_output_only(allow_exclusive=False)

    ring_kwargs, ring_stream = sounddevice.output_streams[-1]
    assert ring_kwargs["extra_settings"].exclusive is False

    # answered: the ring's shared stream is replaced by the exclusive one
    session.start()

    assert ring_stream.closed is True
    answer_kwargs, answer_stream = sounddevice.output_streams[-1]
    assert answer_stream is not ring_stream
    assert answer_kwargs["extra_settings"].exclusive is True
    assert answer_stream.started is True

    # remote audio now goes to the exclusive stream only: the ring's stream
    # is closed, so its callback can no longer be served at all.
    session.enqueue_remote_audio(_remote_pcm(3), 48000)
    assert _wait_for(lambda: bool(answer_stream.render().any()))
    assert ring_stream.writes == []

    session.stop()


def test_ringing_without_exclusive_output_keeps_its_stream_on_answer():
    sounddevice = _SoundDevice()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(session="winzapp", input_device_name="Mic", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )

    session.start_output_only(allow_exclusive=False)
    session.start()

    assert len(sounddevice.output_streams) == 1
    assert sounddevice.output_streams[0][1].closed is False

    session.stop()


def test_the_ringing_monitor_asks_for_a_shared_output():
    src = (Path(__file__).parents[1] / "client" / "main.py").read_text(encoding="utf-8")
    start = src.index("    def _start_incoming_call_audio_monitor(self")
    body = src[start:src.index("\n    def ", start + 10)]
    assert "audio.start_output_only(allow_exclusive=False)" in body


def _fill(buffer, frames=CALL_FRAME_SAMPLES):
    # Prefilled with the sentinel, not zeros: PortAudio hands the callback an
    # UNINITIALISED buffer, so a fill() that stopped zero-filling the tail of a
    # partially starved block would emit whatever the driver left there -- a
    # burst of noise into a headset, not a click. Zeros here hide exactly that.
    out = np.full(frames, _OutputStream.SENTINEL, dtype=np.float32)
    buffer.fill(out)
    return out


def test_jitter_buffer_stays_silent_until_the_prebuffer_target_is_reached():
    """The whole point of the reservoir: the device's callback must find
    enough audio to survive the next burst of arrival jitter, or every
    starved period clicks."""
    buffer = _OutputJitterBuffer(CALL_SAMPLE_RATE)
    target = CALL_SAMPLE_RATE * CALL_OUTPUT_PREBUFFER_MS // 1000

    buffer.push(np.full(target - 1, 0.5, dtype=np.float32))
    assert np.all(_fill(buffer) == 0.0)
    assert buffer.underruns == 0  # waiting to prime is not starvation

    buffer.push(np.full(1, 0.5, dtype=np.float32))
    played = _fill(buffer)
    assert played[-1] == pytest.approx(0.5)


def test_jitter_buffer_hands_out_samples_in_order_once_playing():
    buffer = _OutputJitterBuffer(CALL_SAMPLE_RATE)
    total = CALL_SAMPLE_RATE * CALL_OUTPUT_PREBUFFER_MS // 1000
    buffer.push(np.arange(total, dtype=np.float32))

    first = _fill(buffer)
    second = _fill(buffer)

    # The fade-in touches only the first few milliseconds of the first block.
    fade = CALL_SAMPLE_RATE * 5 // 1000
    assert np.array_equal(first[fade:], np.arange(fade, CALL_FRAME_SAMPLES, dtype=np.float32))
    assert np.array_equal(
        second, np.arange(CALL_FRAME_SAMPLES, CALL_FRAME_SAMPLES * 2, dtype=np.float32)
    )
    assert buffer.underruns == 0
    assert buffer.buffered_samples == total - CALL_FRAME_SAMPLES * 2


def test_jitter_buffer_starves_into_silence_without_raising():
    buffer = _OutputJitterBuffer(CALL_SAMPLE_RATE)
    target = CALL_SAMPLE_RATE * CALL_OUTPUT_PREBUFFER_MS // 1000
    buffer.push(np.full(target + CALL_FRAME_SAMPLES // 2, 0.5, dtype=np.float32))
    for _ in range(3):
        _fill(buffer)

    starved = _fill(buffer)  # the reservoir now runs out mid-block

    assert starved[-1] == 0.0
    assert buffer.underruns == 1
    # Faded to zero rather than cut: the discontinuity is the click.
    voiced = np.flatnonzero(starved)
    if voiced.size:
        assert starved[voiced[-1]] < 0.5


def test_jitter_buffer_fades_back_in_when_audio_resumes():
    buffer = _OutputJitterBuffer(CALL_SAMPLE_RATE)
    target = CALL_SAMPLE_RATE * CALL_OUTPUT_PREBUFFER_MS // 1000
    buffer.push(np.full(target, 0.5, dtype=np.float32))
    for _ in range(4):
        _fill(buffer)
    assert buffer.underruns == 1

    buffer.push(np.full(target, 0.5, dtype=np.float32))
    resumed = _fill(buffer)

    assert resumed[0] == pytest.approx(0.0, abs=1e-6)
    assert resumed[-1] == pytest.approx(0.5)


def test_jitter_buffer_drops_the_oldest_audio_at_the_hard_cap():
    """A network running ahead costs a skip, never accumulated latency --
    and with the blocking write gone this cap is the ONLY bound left in the
    path, since the player thread empties _output_queue as fast as it fills."""
    buffer = _OutputJitterBuffer(CALL_SAMPLE_RATE)
    cap = CALL_SAMPLE_RATE * CALL_OUTPUT_MAX_BUFFER_MS // 1000

    buffer.push(np.full(cap, -1.0, dtype=np.float32))  # the stale audio
    buffer.push(np.full(CALL_FRAME_SAMPLES, 0.25, dtype=np.float32))  # the newest

    assert buffer.buffered_samples == cap
    assert buffer.dropped_samples == CALL_FRAME_SAMPLES
    # The newest packet survived: its samples are still at the end.
    tail = np.zeros(cap, dtype=np.float32)
    buffer.fill(tail)
    assert tail[-1] == pytest.approx(0.25)


def test_jitter_buffer_reset_reprimes():
    buffer = _OutputJitterBuffer(CALL_SAMPLE_RATE)
    buffer.push(np.full(CALL_SAMPLE_RATE, 0.5, dtype=np.float32))

    buffer.reset()

    assert buffer.buffered_samples == 0
    assert np.all(_fill(buffer) == 0.0)


def test_a_failed_device_candidate_says_why_instead_of_being_swallowed(caplog):
    """Measured 2026-09-22: the same headset opened on WASAPI for one call and
    on MME for the next, and nothing in the log said why the WASAPI twin lost
    — every candidate exception went silently into last_error."""

    class _RefusesFirstSpeaker(_SoundDevice):
        def OutputStream(self, **kwargs):
            if kwargs["device"] == 1 and kwargs["samplerate"] == 48000:
                raise RuntimeError("device in use")
            return super().OutputStream(**kwargs)

    sounddevice = _RefusesFirstSpeaker()
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(session="winzapp", output_device_name="Speaker"),
        sounddevice_module=sounddevice,
    )

    with caplog.at_level("INFO"):
        session.start_output_only()

    failures = [r.getMessage() for r in caplog.records if "candidate failed" in r.getMessage()]
    assert failures, "a refused candidate must name itself"
    assert "device=1" in failures[0]
    assert "rate=48000" in failures[0]
    assert "device in use" in failures[0]
    # Device NAMES stay out of the log (PII); the index and host API are enough.
    assert "Speaker" not in failures[0]

    session.stop()


def test_jitter_buffer_resumes_after_one_packet_not_the_cold_start_target():
    """Re-priming the full 60 ms after every underrun would chop a jittery
    but adequate line into roughly a 50% duty cycle — for a blind user on a
    call, speech cut in half is worse than the clicks this buffer removes."""
    buffer = _OutputJitterBuffer(CALL_SAMPLE_RATE)
    resume = CALL_SAMPLE_RATE * CALL_OUTPUT_RESUME_MS // 1000
    cold_start = CALL_SAMPLE_RATE * CALL_OUTPUT_PREBUFFER_MS // 1000
    assert resume < cold_start

    buffer.push(np.full(cold_start, 0.5, dtype=np.float32))
    for _ in range(4):
        _fill(buffer)
    assert buffer.underruns == 1

    # One packet — far short of the cold-start target — is enough to speak
    # again, and it is played rather than held.
    buffer.push(np.full(resume, 0.5, dtype=np.float32))
    assert np.any(_fill(buffer) != 0.0)


def test_jitter_buffer_thresholds_scale_with_the_device_rate():
    """Every threshold is a duration, so it has to be recomputed per device.

    _candidate_rates() deliberately reaches 16000 for an HFP-only Bluetooth
    headset. A resume threshold left at 48 kHz samples would hold 60 ms of
    audio at 16 kHz before speaking again instead of 20 ms — the exact 50%
    duty-cycle chop the split threshold exists to prevent, on the hardware
    most likely to be jittery in the first place.
    """
    rate = 16_000
    buffer = _OutputJitterBuffer(rate)

    assert buffer._prebuffer_samples == rate * CALL_OUTPUT_PREBUFFER_MS // 1000
    assert buffer._resume_samples == rate * CALL_OUTPUT_RESUME_MS // 1000
    assert buffer._max_samples == rate * CALL_OUTPUT_MAX_BUFFER_MS // 1000
    assert buffer._fade_samples == rate * CALL_OUTPUT_FADE_MS // 1000

    # And the same after a mid-call device switch reconfigures it.
    buffer.configure(CALL_SAMPLE_RATE)
    assert buffer._prebuffer_samples == CALL_SAMPLE_RATE * CALL_OUTPUT_PREBUFFER_MS // 1000
    assert buffer._resume_samples == CALL_SAMPLE_RATE * CALL_OUTPUT_RESUME_MS // 1000


def test_jitter_buffer_serves_whatever_period_the_device_asks_for():
    """blocksize=0 means the block size is the DEVICE's period, which is
    neither 20 ms nor constant — including a block larger than the whole
    reservoir."""
    buffer = _OutputJitterBuffer(CALL_SAMPLE_RATE)
    cap = CALL_SAMPLE_RATE * CALL_OUTPUT_MAX_BUFFER_MS // 1000
    buffer.push(np.full(cap, 0.5, dtype=np.float32))

    odd = _fill(buffer, frames=113)
    assert odd.shape == (113,)
    assert odd[-1] == pytest.approx(0.5)

    # A period longer than everything buffered: the tail is silence, the
    # underrun is counted, and nothing raises.
    oversized = _fill(buffer, frames=cap * 2)
    assert oversized[-1] == 0.0
    assert buffer.underruns == 1


def test_jitter_buffer_fades_a_block_shorter_than_the_fade_itself():
    """The device period can be shorter than the 5 ms fade, so _apply_ramp's
    partial-length path is real code, not a defensive branch."""
    buffer = _OutputJitterBuffer(CALL_SAMPLE_RATE)
    fade = CALL_SAMPLE_RATE * 5 // 1000
    buffer.push(np.full(CALL_SAMPLE_RATE * CALL_OUTPUT_PREBUFFER_MS // 1000, 0.5, dtype=np.float32))

    tiny = _fill(buffer, frames=fade // 2)

    assert tiny.shape == (fade // 2,)
    assert tiny[0] == pytest.approx(0.0, abs=1e-6)  # ramped from silence
    assert np.all(tiny <= 0.5)
    assert buffer.underruns == 0


def test_starvation_is_reported_from_outside_the_callback(caplog):
    """The callback may not log (it runs on PortAudio's realtime thread), so
    the player thread reports instead — at once for the first underrun, then
    throttled, because a bad line produces one per device period."""
    session = CallAudioSession(
        _Socket(),
        CallAudioConfig(session="winzapp"),
        sounddevice_module=_SoundDevice(),
    )

    def _starved_lines():
        return [r.getMessage() for r in caplog.records if "remote audio starved" in r.getMessage()]

    with caplog.at_level("INFO"):
        session._log_output_underruns()
        assert _starved_lines() == [], "no underruns, nothing to say"

        session._output_buffer.underruns = 1
        session._log_output_underruns()
        assert len(_starved_lines()) == 1
        assert "underruns=1" in _starved_lines()[0]

        # Everything up to the throttle stays quiet.
        session._output_buffer.underruns = CALL_OUTPUT_UNDERRUN_LOG_EVERY
        session._log_output_underruns()
        assert len(_starved_lines()) == 1

        session._output_buffer.underruns = 1 + CALL_OUTPUT_UNDERRUN_LOG_EVERY
        session._log_output_underruns()
        assert len(_starved_lines()) == 2
        assert f"underruns={1 + CALL_OUTPUT_UNDERRUN_LOG_EVERY}" in _starved_lines()[1]
