"""Python-owned microphone and speaker transport for WhatsApp voice calls.

Chromium keeps WhatsApp signaling, encryption, and WebRTC. It never opens the
physical microphone: Python captures PCM, forwards it to the page bridge, and
plays the PCM extracted from the remote WebRTC track.
"""

from __future__ import annotations

import base64
import logging
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import numpy as np

from core.audio_devices import repair_device_name
from core.echo_canceller import EchoCanceller

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover - packaging always installs it
    sd = None

CALL_SAMPLE_RATE = 48_000
CALL_FRAME_MS = 20
CALL_FRAME_SAMPLES = CALL_SAMPLE_RATE * CALL_FRAME_MS // 1000
CALL_MIC_QUEUE_LIMIT = 12
CALL_MIC_TARGET_BACKLOG_FRAMES = 2
CALL_OUTPUT_QUEUE_LIMIT = 40
# Playback reservoir, in milliseconds of audio at the output device's rate.
# Remote PCM is tapped in the page by an AudioWorklet delivering ~21.3 ms
# batches (falling back to the old main-thread createScriptProcessor(1024) when
# the page's CSP refuses the worklet module) and then crosses Socket.IO and a
# Python socket thread, so a few milliseconds of arrival jitter is normal.
# Three packets of slack absorb that; the hard cap is what keeps the slack from
# growing into latency (see _OutputJitterBuffer).
# Note the prebuffer is one-way latency the listener pays on every call.
CALL_OUTPUT_PREBUFFER_MS = 60
CALL_OUTPUT_MAX_BUFFER_MS = 200
# Refilling after an underrun aims at an ADAPTIVE target, and that this is not
# a fixed threshold is measured history, not taste. It was a fixed 20 ms (one
# packet), chosen in review because requiring the full 60 ms again would turn a
# network that is jittery but adequate into roughly a 50% duty cycle. The live
# data says the opposite failure is the one that actually happens: on
# 2026-09-23 the page's createScriptProcessor tap was measured delivering
# 47,507 samples/s against the 48,000 it declares -- it drops ~1% of its own
# buffers outright -- and a source that runs under real time drains ANY fixed
# reservoir forever. With a 20 ms resume the buffer re-starved within a packet
# or two: ~2.8 underruns per second, steadily, for a 35-minute call, every one
# of them a fade-out/fade-in chop. So the target starts at the cold-start value
# and GROWS while underruns keep recurring (a slow source buys proportionally
# more time from a deeper reservoir), then decays back while playback stays
# clean, which serves the jittery line the 20 ms was protecting as well. The
# ceiling stays well under CALL_OUTPUT_MAX_BUFFER_MS so the hard cap still
# drops oldest and latency still cannot creep.
CALL_OUTPUT_TARGET_STEP_MS = 20
CALL_OUTPUT_TARGET_MAX_MS = 120
# "Keeps recurring" means another underrun within this much PLAYED audio of
# the previous one. It is also what stops the growth: once the deeper target
# has pushed underruns further apart than this window, they stop counting as
# recurring and the target settles.
CALL_OUTPUT_TARGET_GROWTH_WINDOW_MS = 10_000
CALL_OUTPUT_TARGET_DECAY_MS = 30_000
# A discontinuity clicks even when the samples on either side are silence, so
# starvation and resumption are ramped rather than cut.
CALL_OUTPUT_FADE_MS = 5
CALL_OUTPUT_UNDERRUN_LOG_EVERY = 50


@dataclass(frozen=True)
class CallAudioConfig:
    session: str
    input_device_name: str = ""
    output_device_name: str = ""
    # Exclusive access is per direction, because the two have completely
    # different costs. Holding the MICROPHONE exclusively takes it from other
    # apps that are not using it during a call anyway. Holding the SPEAKER
    # exclusively silences everything else on that device -- including the
    # screen reader, for the whole call, which for WinZapp's users means
    # losing the call window's own controls. Hence two flags, both default
    # off, and only the output one carries a spoken warning in the UI.
    exclusive_input: bool = False
    exclusive_output: bool = False
    # Adaptive echo cancellation on the outgoing microphone, fed by what the
    # output callback actually played. Off by default: it costs CPU and can
    # slightly colour the voice, and a headset user has no echo to remove.
    echo_cancellation: bool = False


class CallAudioUnavailable(RuntimeError):
    """Raised when the local call audio devices cannot be opened."""


def _resample_mono(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Return mono float32 audio at ``target_rate`` using linear interpolation."""
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim > 1:
        data = data.mean(axis=1, dtype=np.float32)
    data = data.reshape(-1)
    if not data.size or source_rate == target_rate:
        return data.astype(np.float32, copy=False)
    target_len = max(1, int(round(data.size * target_rate / source_rate)))
    source_x = np.linspace(0.0, 1.0, num=data.size, endpoint=False)
    target_x = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
    return np.interp(target_x, source_x, data).astype(np.float32)


def _pcm16_bytes(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2", copy=False).tobytes()


def _pcm16_float32(pcm: bytes) -> np.ndarray:
    if not pcm:
        return np.empty(0, dtype=np.float32)
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


class _OutputJitterBuffer:
    """Bounded reservoir of float32 mono samples between network and device.

    Module level, and with no sounddevice in sight, so the whole reservoir is
    testable without a device or a wx.App: the player thread pushes, the
    PortAudio callback fills.

    A reservoir in front of the output is the component the 2026-09-20
    bisection isolated as the cause of choppy call audio, and it is back here
    deliberately — so read _play_remote_loop's docstring before trusting it.
    What differs is not the reservoir's own bookkeeping (the reverted one was
    hard capped and dropped oldest too) but what drains it: there is no
    blocking stream.write() and no wall-clock pacing left anywhere, only
    PortAudio asking for one device period at a time.
    """

    def __init__(self, rate: int):
        self._lock = threading.Lock()
        self._chunks: "deque[np.ndarray]" = deque()
        self._size = 0
        # Cumulative for the life of the session and deliberately NOT cleared
        # by reset()/configure(): they exist so the player thread can report
        # how the call went, and an exclusive reopen mid-call must not make
        # the first half of that call disappear from the log. Plain ints read
        # without the lock — an approximate count logged a fraction of a
        # second late beats taking the realtime thread's lock to report it.
        self.underruns = 0
        self.dropped_samples = 0
        # The rate everything below is currently scaled to, so configure() can
        # tell a real device change from a reopen of the same device.
        self._rate = 0
        self.configure(rate)

    def configure(self, rate: int) -> None:
        """Re-scale the reservoir to *rate* and drop whatever it held.

        Under the lock, because the audio callback reads every one of these
        under it: a stream whose close() failed (logged and swallowed) can
        still be draining this reservoir while its replacement is opened, and
        a ramp half-resized against a block sized by the old rate is a shape
        mismatch swallowed into silence. Not on the realtime path, so the cost
        is irrelevant.
        """
        rate = max(1, int(rate or CALL_SAMPLE_RATE))
        with self._lock:
            rate_changed = rate != self._rate
            self._rate = rate
            self._prebuffer_samples = max(1, rate * CALL_OUTPUT_PREBUFFER_MS // 1000)
            self._target_step_samples = max(1, rate * CALL_OUTPUT_TARGET_STEP_MS // 1000)
            self._max_target_samples = max(
                self._prebuffer_samples, rate * CALL_OUTPUT_TARGET_MAX_MS // 1000
            )
            self._growth_window_samples = max(
                1, rate * CALL_OUTPUT_TARGET_GROWTH_WINDOW_MS // 1000
            )
            self._decay_samples = max(1, rate * CALL_OUTPUT_TARGET_DECAY_MS // 1000)
            self._max_samples = max(
                self._prebuffer_samples, rate * CALL_OUTPUT_MAX_BUFFER_MS // 1000
            )
            self._fade_samples = max(1, rate * CALL_OUTPUT_FADE_MS // 1000)
            # Precomputed so a fade costs no allocation in the common case;
            # the callback runs on PortAudio's realtime thread.
            self._fade_in_ramp = np.linspace(0.0, 1.0, self._fade_samples, dtype=np.float32)
            self._fade_out_ramp = self._fade_in_ramp[::-1].copy()
            # Every threshold above is a duration in samples, so what the
            # buffer learned about this source is meaningless at a NEW rate and
            # is dropped with it. At the same rate it is kept, and that is the
            # case that matters: _reopen_output_exclusive() reopens the same
            # device at the same rate when an incoming call is answered, so
            # discarding it here would throw away everything the ringing phase
            # learned and re-chop the first minute of the actual conversation
            # while it climbs back. _seen_underrun has the same lifetime as the
            # target it qualifies: "one underrun is not a pattern" must not be
            # re-armed by a reopen either.
            if rate_changed:
                self._target_samples = self._prebuffer_samples
                self._seen_underrun = False
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._size = 0
            # Playback starts only once the target is there, and the first
            # samples after any silence are faded in. The target is the
            # adaptive one rather than the cold-start constant: reset() also
            # runs on a mid-call device reopen, and the source being slow is a
            # property of the page tap, not of the speaker just reopened.
            self._priming = True
            self._priming_target = self._target_samples
            self._fade_in_pending = True
            # Both counters measure PLAYED audio, which is the only clock a
            # realtime callback may consult: time.monotonic() is cheap but the
            # sample count is exact and is what the thresholds are written in.
            self._samples_since_underrun = 0
            self._clean_samples = 0

    @property
    def buffered_samples(self) -> int:
        """Approximate fill level — for logs and tests, never for a decision.

        Read without the lock, so it can be a callback out of date.
        """
        return self._size

    def push(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if not samples.size:
            return
        with self._lock:
            self._chunks.append(samples)
            self._size += samples.size
            while self._size > self._max_samples and self._chunks:
                overflow = self._size - self._max_samples
                oldest = self._chunks[0]
                if oldest.size <= overflow:
                    self._chunks.popleft()
                    self._size -= oldest.size
                    self.dropped_samples += oldest.size
                else:
                    self._chunks[0] = oldest[overflow:]
                    self._size -= overflow
                    self.dropped_samples += overflow

    def fill(self, out: np.ndarray) -> None:
        """Write one device period into *out* (a writable 1-D float view).

        Never raises, never blocks and never logs: this is called from
        PortAudio's realtime thread, where any of the three is an audible
        glitch at best. A starved buffer is silence plus a counter.
        """
        frames = out.shape[0]
        with self._lock:
            if self._priming:
                if self._size < self._priming_target:
                    out[:] = 0.0
                    return
                self._priming = False

            written = 0
            while written < frames and self._chunks:
                chunk = self._chunks[0]
                take = min(frames - written, chunk.size)
                out[written:written + take] = chunk[:take]
                written += take
                self._size -= take
                if take == chunk.size:
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[take:]

            if self._fade_in_pending and written:
                self._apply_ramp(out, 0, min(self._fade_samples, written), fade_in=True)
                self._fade_in_pending = False

            if written < frames:
                if written:
                    length = min(self._fade_samples, written)
                    self._apply_ramp(out, written - length, length, fade_in=False)
                out[written:] = 0.0
                self.underruns += 1
                # Refill before speaking again, or every callback from here on
                # starves by the same handful of samples and clicks. How much
                # to refill is the adaptive target: an underrun that arrives
                # soon after audio resumed says this reservoir is too shallow
                # for this source, so deepen it a step -- but not on the FIRST
                # underrun at this rate, which proves nothing yet: one is an
                # incident, a second one soon after is a pattern, and every
                # step is one-way latency the listener pays. What the window
                # bounds is the RATE of growth, not its total: two dropouts
                # 5 s apart do buy a step each, which is the intended reading
                # of "recurring". What it does rule out is a healthy call
                # ratcheting to the ceiling over an hour on isolated
                # incidents, and one continuous silence costing more than one
                # step (re-priming means the next underrun cannot arrive until
                # a whole target has been played again).
                if (
                    self._seen_underrun
                    and self._samples_since_underrun < self._growth_window_samples
                ):
                    self._target_samples = min(
                        self._max_target_samples,
                        self._target_samples + self._target_step_samples,
                    )
                self._seen_underrun = True
                self._samples_since_underrun = 0
                self._clean_samples = 0
                self._priming = True
                self._priming_target = self._target_samples
                self._fade_in_pending = True
            else:
                self._samples_since_underrun += written
                self._clean_samples += written
                # Clean for a long stretch: give the latency back a step at a
                # time, down to the cold-start target and never below it.
                if (
                    self._clean_samples >= self._decay_samples
                    and self._target_samples > self._prebuffer_samples
                ):
                    self._target_samples = max(
                        self._prebuffer_samples,
                        self._target_samples - self._target_step_samples,
                    )
                    self._clean_samples = 0

    def _apply_ramp(self, out: np.ndarray, start: int, length: int, *, fade_in: bool) -> None:
        if length == self._fade_samples:
            ramp = self._fade_in_ramp if fade_in else self._fade_out_ramp
        elif fade_in:
            ramp = np.linspace(0.0, 1.0, length, dtype=np.float32)
        else:
            ramp = np.linspace(1.0, 0.0, length, dtype=np.float32)
        out[start:start + length] *= ramp


class CallAudioSession:
    """Own the Python side of one low-latency voice-call audio pipeline."""

    def __init__(self, sio, config: CallAudioConfig, *, sounddevice_module=None):
        self._sio = sio
        self._config = config
        self._sd = sounddevice_module or sd
        self._input_stream = None
        self._output_stream = None
        self._input_rate = CALL_SAMPLE_RATE
        self._output_rate = CALL_SAMPLE_RATE
        self._mic_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=CALL_MIC_QUEUE_LIMIT)
        self._output_queue: "queue.Queue[tuple[bytes, int]]" = queue.Queue(
            maxsize=CALL_OUTPUT_QUEUE_LIMIT
        )
        # The reservoir the output callback drains; the player thread fills it.
        self._output_buffer = _OutputJitterBuffer(self._output_rate)
        self._output_underruns_logged = 0
        self._stop_event = threading.Event()
        # Held around every write to the output stream and around replacing
        # it, so the player never writes to a stream being closed.
        self._output_lock = threading.Lock()
        # True while the output was opened SHARED for the ring although
        # exclusive_output is on; start() reopens it exclusive on answer.
        self._output_exclusive_deferred = False
        self._sender_thread: Optional[threading.Thread] = None
        self._player_thread: Optional[threading.Thread] = None
        self._mic_frames_sent = 0
        self._mic_bytes_sent = 0
        self._mic_frames_dropped_for_latency = 0
        self._microphone_muted = False
        self._echo_canceller: Optional[EchoCanceller] = (
            EchoCanceller() if config.echo_cancellation else None
        )
        # (samples, rate) chunks copied out of the output callback; the sender
        # thread resamples them into the canceller, so the realtime callback
        # only pays for a copy. Bounded: nobody drains it while ringing.
        self._echo_reference_tap: "deque[tuple[np.ndarray, int]]" = deque(maxlen=64)

    @property
    def microphone_muted(self) -> bool:
        return self._microphone_muted

    def set_microphone_muted(self, muted: bool) -> None:
        """Mute only this call's outgoing microphone, keeping the stream alive."""
        self._microphone_muted = bool(muted)
        logging.info("[call_audio] microphone %s", "muted" if self._microphone_muted else "unmuted")

    @property
    def running(self) -> bool:
        return not self._stop_event.is_set() and self._input_stream is not None

    @property
    def output_running(self) -> bool:
        return not self._stop_event.is_set() and self._output_stream is not None

    def start_output_only(self, *, allow_exclusive: bool = True) -> None:
        """Open receive audio while ringing without opening the microphone.

        ``allow_exclusive=False`` is for the ringing monitor. An exclusive
        output opened while the call is still ringing would take the device
        away from the ring tone and from the screen reader announcing who is
        calling -- the call would arrive with no signal at all, which the
        exclusive-output warning never asked the user to accept. The output
        is opened shared instead, and start() makes it exclusive on answer.
        """
        if self.output_running:
            return
        if self._sd is None:
            raise CallAudioUnavailable("sounddevice is not available in this Python runtime")

        self._stop_event.clear()
        self._output_stream, self._output_rate = self._open_output_stream(
            allow_exclusive=allow_exclusive
        )
        self._output_exclusive_deferred = bool(
            self._config.exclusive_output and not allow_exclusive
        )
        # Before start(): the callback begins pulling the moment the stream
        # runs, and it must find an empty, correctly scaled reservoir.
        self._output_buffer.configure(self._output_rate)
        try:
            self._output_stream.start()
        except Exception:
            self._close_stream(self._output_stream)
            self._output_stream = None
            raise
        self._player_thread = threading.Thread(
            target=self._play_remote_loop,
            name="WinZappCallRemotePlayer",
            daemon=True,
        )
        self._player_thread.start()
        self._emit_start()

    def start(self) -> None:
        if self.running:
            return
        if self._sd is None:
            raise CallAudioUnavailable("sounddevice is not available in this Python runtime")

        # An incoming call may already have opened the receive side while it
        # was ringing. Reuse it and only add microphone capture on answer.
        opened_output_here = not self.output_running
        self.start_output_only()
        if self._output_exclusive_deferred:
            self._reopen_output_exclusive()
        try:
            self._input_stream, self._input_rate = self._open_input_stream()
            self._input_stream.start()
        except Exception:
            self._close_stream(self._input_stream)
            self._input_stream = None
            # Tear down the receive side only when THIS call opened it.
            # Without this, an outgoing call whose microphone cannot be
            # opened (taken by another app) left the OutputStream and the
            # player thread alive for the life of the process: nothing else
            # can reach them, because the session never becomes
            # _call_audio_session and _stop_voice_call_audio() therefore
            # never sees it. Under exclusive_output that stranded stream holds
            # the output device, silencing the screen reader until restart,
            # and _restart_active_voice_call_audio()'s three attempts each
            # stranded another one. When the output was already running it
            # belongs to the ringing monitor, whose owner stops it on this
            # same failure -- stopping it here too would be a double close.
            if opened_output_here:
                self.stop()
            raise

        self._sender_thread = threading.Thread(
            target=self._send_microphone_loop,
            name="WinZappCallMicSender",
            daemon=True,
        )
        self._sender_thread.start()

    def _reopen_output_exclusive(self) -> None:
        """Swap the ring's shared output for the exclusive one on answer.

        The shared stream must be closed first: while this process holds the
        device shared, WASAPI refuses it exclusive. _open_output_stream()
        still falls back to shared on its own if exclusive is refused.
        """
        self._output_exclusive_deferred = False
        with self._output_lock:
            old = self._output_stream
            self._output_stream = None
            self._close_stream(old)
            stream, rate = self._open_output_stream()
            # The ring's audio is gone with its stream; the new device may
            # even run at a different rate.
            self._output_buffer.configure(rate)
            try:
                stream.start()
            except Exception:
                self._close_stream(stream)
                raise
            self._output_stream, self._output_rate = stream, rate

    def stop(self, *, notify_bridge: bool = True) -> None:
        """Close the local streams; by default also tell the page bridge.

        ``notify_bridge=False`` is for a device switch inside the same call.
        ``call:audio:stop`` makes the page bridge reset(), which disables it,
        and only ``/call/audio/enable`` turns it back on -- the new session's
        ``call:audio:start`` does not. So a switch that emitted it kept sending
        microphone frames that the page dropped, and the other person heard
        silence for the rest of the call.
        """
        self._stop_event.set()
        if notify_bridge:
            self._emit_stop()
        self._close_stream(self._input_stream)
        self._input_stream = None
        # Under the same lock as the player's writes, so a write in flight
        # does not hit a stream being closed and log a spurious exception.
        with self._output_lock:
            self._close_stream(self._output_stream)
            self._output_stream = None
        self._drain_queue(self._mic_queue)
        self._drain_queue(self._output_queue)
        self._output_buffer.reset()
        self._echo_reference_tap.clear()
        if self._echo_canceller is not None:
            self._echo_canceller.reset()

    def enqueue_remote_audio(self, pcm: bytes, sample_rate: int) -> None:
        if self._stop_event.is_set() or not pcm:
            return
        try:
            sample_rate = int(sample_rate or CALL_SAMPLE_RATE)
        except (TypeError, ValueError):
            sample_rate = CALL_SAMPLE_RATE
        self._put_drop_oldest(self._output_queue, (bytes(pcm), sample_rate))

    def _query_devices(self):
        try:
            return list(self._sd.query_devices())
        except Exception as exc:
            raise CallAudioUnavailable(f"Could not enumerate audio devices: {exc}") from exc

    @staticmethod
    def _normalized_name(value: str) -> str:
        # repair_device_name: a microphone chosen from a PyAudio-built list was
        # saved as mojibake ("estÃ©reo") and never matched sounddevice's
        # correct name here, so the call used the default microphone instead.
        value = repair_device_name(value)
        return " ".join(value.replace("(", "").replace(")", "").lower().split())

    def _resolve_device(self, stored_name: str, *, input_device: bool) -> Optional[int]:
        if not stored_name:
            return None
        wanted = self._normalized_name(stored_name)
        devices = self._query_devices()
        candidates = []
        for index, info in enumerate(devices):
            channels_key = "max_input_channels" if input_device else "max_output_channels"
            if int(info.get(channels_key, 0) or 0) <= 0:
                continue
            actual = self._normalized_name(info.get("name", ""))
            if actual == wanted:
                return index
            if wanted and (wanted in actual or actual in wanted):
                candidates.append(index)
        return candidates[0] if candidates else None

    def _default_device_index(self, *, input_device: bool) -> Optional[int]:
        """Return PortAudio's concrete default device instead of opaque None."""
        try:
            defaults = getattr(getattr(self._sd, "default", None), "device", None)
            index = defaults[0 if input_device else 1]
            index = int(index)
            return index if index >= 0 else None
        except (AttributeError, IndexError, TypeError, ValueError):
            return None

    def _hostapi_name(self, device_index) -> str:
        """Lower-cased PortAudio host API name for *device_index*, or ""."""
        try:
            info = self._sd.query_devices(int(device_index))
            hostapi = self._sd.query_hostapis(int(info.get("hostapi", -1)))
            return str(hostapi.get("name", "")).lower()
        except Exception:
            return ""

    def _wasapi_twin(self, device_index, *, input_device: bool):
        """The WASAPI entry for the same physical device as *device_index*.

        Windows exposes one device once per host API, and PortAudio enumerates
        them MME first, then DirectSound, then WASAPI, then WDM-KS. Every
        lookup here resolves by NAME and takes the first match, so calls ran on
        MME -- measured on a real Realtek device, 90 ms of input buffering and
        120 ms on DirectSound output, against 3 ms for the very same hardware
        through WASAPI. That is device latency alone, before any network, and
        it is the difference between a conversation and a walkie-talkie.

        This is plain SHARED-mode WASAPI (PortAudio's default; exclusive is
        opt-in through WasapiSettings), so nothing is taken away from any other
        application -- the screen reader included. The exclusive_* flags stay a
        separate, per-direction choice.

        Returns None when there is no WASAPI twin, in which case the caller
        keeps the original index and the existing fallback cascade applies.
        """
        if device_index is None:
            return None
        channels_key = "max_input_channels" if input_device else "max_output_channels"
        try:
            wanted = self._normalized_name(
                self._sd.query_devices(int(device_index)).get("name", "")
            )
        except Exception:
            return None
        if not wanted:
            return None
        # Two passes, exact before partial. The substring tolerance is needed
        # -- MME truncates device names at 31 characters, so the stored name
        # can legitimately be a prefix of the WASAPI one -- but taking the
        # first hit of EITHER kind let a partial match at a lower index beat
        # an exact match further down. Two endpoints sharing a 31-character
        # prefix ("Headset Earphone (Jabra Evolve 65/75)") would then open the
        # call on the wrong physical device, while the log cheerfully reported
        # hostapi=wasapi. For a blind user, hearing the call come out of the
        # monitor's HDMI output instead of the headset has no visual tell.
        partial = None
        for index, info in enumerate(self._query_devices()):
            if int(info.get(channels_key, 0) or 0) <= 0:
                continue
            try:
                hostapi = self._sd.query_hostapis(int(info.get("hostapi", -1)))
                if "wasapi" not in str(hostapi.get("name", "")).lower():
                    continue
            except Exception:
                continue
            actual = self._normalized_name(info.get("name", ""))
            if actual == wanted:
                return index
            if partial is None and (wanted in actual or actual in wanted):
                partial = index
        return partial

    def _candidate_devices(
        self, stored_name: str, *, input_device: bool, include_fallbacks: bool = True
    ):
        preferred = self._resolve_device(stored_name, input_device=input_device)
        default_device = self._default_device_index(input_device=input_device)
        yielded = set()
        ordered = []
        # The WASAPI twin of whatever the user picked comes first, then the
        # pick itself, so a device that refuses WASAPI still opens exactly the
        # way it does today. Same for the system default below.
        if preferred is not None:
            twin = self._wasapi_twin(preferred, input_device=input_device)
            if twin is not None:
                ordered.append(twin)
            ordered.append(preferred)
        # The exclusive pass (include_fallbacks=False) never leaves a device
        # the user chose: offered the system default after the chosen one, a
        # headset refusing exclusive access put the call EXCLUSIVE on the
        # default speakers -- where the screen reader lives -- with the headset
        # silent. The shared pass that follows still reaches the default.
        # Keyed on the user having CHOSEN a device, not on it resolving: a
        # chosen headset that is unplugged resolves to nothing, and the
        # exclusive pass must not take the default speakers in its place.
        chosen = bool(str(stored_name or "").strip())
        if chosen and not include_fallbacks:
            default_device = None
        if default_device is not None:
            twin = self._wasapi_twin(default_device, input_device=input_device)
            if twin is not None:
                ordered.append(twin)
            ordered.append(default_device)
        # Keep PortAudio's implicit default as a compatibility fallback, but
        # prefer the concrete default index so we can inspect its native rate.
        if not chosen or include_fallbacks:
            ordered.append(None)
        for index in ordered:
            key = -1 if index is None else int(index)
            if key not in yielded:
                yielded.add(key)
                yield index
        # The sweep over EVERY device is a last resort for "open something
        # rather than nothing", and it belongs to the shared pass only. Offered
        # to the exclusive pass, it opened whatever unrelated device first
        # accepted exclusive access once the intended one refused -- measured
        # live on 2026-09-21: exclusive_input on opened "Line 1 (Virtual Audio
        # Cable)" instead of the microphone, and the peer heard silence for the
        # whole call.
        if not include_fallbacks:
            return
        channels_key = "max_input_channels" if input_device else "max_output_channels"
        for index, info in enumerate(self._query_devices()):
            if int(info.get(channels_key, 0) or 0) <= 0 or index in yielded:
                continue
            yielded.add(index)
            yield index

    def _candidate_rates(self, device_index: Optional[int]):
        # Try the call transport's own rate (48 kHz) first. Most non-HFP
        # devices open at 48 kHz directly, which needs no resampling in
        # either direction for the life of the call. A previous revision put
        # the device's native rate first instead — reasoned the same way
        # core/audio_devices.py's recording_configs_for() does for voice
        # messages, where it's the right call — but for calls specifically,
        # unlike a one-shot recording, that meant a device whose native rate
        # merely *differs* from 48 kHz (44100, common on plenty of ordinary
        # hardware, not just Bluetooth) got resampled on every single 20 ms
        # frame for the whole call. Delivery stayed smooth (no underflow, no
        # rebuffering — the queue/output-write plumbing was never the issue),
        # but the resampled audio itself was audibly choppy. Native rate
        # stays second, ahead of the fixed tail, so a genuine HFP-only
        # Bluetooth microphone (8000/16000 Hz, the reason native is tried at
        # all) is still reached before giving up.
        rates = [CALL_SAMPLE_RATE]
        try:
            info = self._sd.query_devices(device_index)
            native = int(round(float(info.get("default_samplerate") or 0)))
            if native > 0 and native not in rates:
                rates.append(native)
        except Exception:
            pass
        for rate in (44_100, 32_000, 16_000):
            if rate not in rates:
                rates.append(rate)
        return rates

    # A previous revision forced every WASAPI stream into shared mode via
    # sd.WasapiSettings(exclusive=False, auto_convert=True) here, meant to
    # stop a call from taking the device away from other applications. That
    # is what PortAudio already does by default on WASAPI — exclusive mode is
    # opt-in, never the fallback — so the extra settings changed nothing about
    # exclusivity and only routed every call through WASAPI's own format
    # converter (auto_convert), which measurably added the choppy, high-
    # latency playback reported after this landed. Now that exclusive mode is
    # a real, deliberate feature (settings["call_audio_devices"]["exclusive_input"]
    # and ["exclusive_output"]),
    # auto_convert is kept in BOTH modes as a resilience fallback — it only
    # engages if the exact requested rate/format cannot be opened as-is, so it
    # does not reintroduce the earlier regression, which came from forcing
    # shared mode with no exclusive option, not from auto_convert itself.
    def _stream_extra_settings(self, device_index, *, input_device, exclusive):
        if sys.platform != "win32":
            return None
        actual_index = device_index
        if actual_index is None:
            actual_index = self._default_device_index(input_device=input_device)
        if actual_index is None:
            return None
        try:
            info = self._sd.query_devices(actual_index)
            hostapi = self._sd.query_hostapis(int(info.get("hostapi", -1)))
            if "wasapi" not in str(hostapi.get("name", "")).lower():
                return None
            settings_type = getattr(self._sd, "WasapiSettings", None)
            if settings_type is None:
                return None
            return settings_type(exclusive=exclusive, auto_convert=True)
        except Exception:
            logging.debug(
                "[call_audio] could not apply WASAPI settings (exclusive=%s)", exclusive, exc_info=True,
            )
            return None

    def _open_input_stream(self):
        last_error = None
        # Exclusive mode is attempted first (unless the user opted out), across
        # the whole device/rate matrix; only once that entire space is
        # exhausted does the same matrix get retried in shared mode. A single
        # device refusing exclusive access must not fall back to a worse
        # device — it should fall back to the same device in shared mode.
        exclusive_attempts = [True, False] if self._config.exclusive_input else [False]
        for attempt_index, exclusive in enumerate(exclusive_attempts):
            for device in self._candidate_devices(
                self._config.input_device_name, input_device=True,
                include_fallbacks=not exclusive,
            ):
                for rate in self._candidate_rates(device):
                    try:
                        extra_settings = self._stream_extra_settings(
                            device, input_device=True, exclusive=exclusive
                        )
                        # The exclusive pass may only open a device exclusive
                        # mode actually APPLIES to. A non-WASAPI candidate gets
                        # extra_settings=None and would open "successfully"
                        # right here -- so with the WASAPI twin refusing
                        # exclusive access (common), the MME entry behind it
                        # won the exclusive pass and the shared pass, where the
                        # same WASAPI device would have opened, never ran.
                        # Measured live: exclusive_input on put the microphone
                        # on MME at ~90 ms instead of shared WASAPI at ~3 ms.
                        if exclusive and extra_settings is None:
                            continue
                        stream = self._sd.InputStream(
                            samplerate=rate,
                            blocksize=max(1, int(rate * CALL_FRAME_MS / 1000)),
                            device=device,
                            channels=1,
                            dtype="float32",
                            latency="low",
                            extra_settings=extra_settings,
                            callback=self._on_microphone_frame(rate),
                        )
                        if attempt_index > 0:
                            logging.info(
                                "[call_audio] exclusive mode unavailable, fell back to shared mode for input"
                            )
                        # Report what was actually APPLIED, not what was asked
                        # for. _stream_extra_settings() returns None for any
                        # non-WASAPI device, so this used to log exclusive=True
                        # for streams running in ordinary shared mode -- which
                        # is precisely what hid the fact that the whole
                        # exclusive path was unreachable on a default install.
                        logging.info(
                            "[call_audio] input opened device=%r hostapi=%s rate=%s latency=%r exclusive=%s",
                            device,
                            self._hostapi_name(device) if device is not None else "default",
                            rate,
                            getattr(stream, "latency", "low"),
                            bool(exclusive and extra_settings is not None),
                        )
                        return stream, rate
                    except Exception as exc:
                        last_error = exc
                        self._log_candidate_failure(
                            "input", device, rate, exclusive, exc
                        )
        raise CallAudioUnavailable(f"No microphone could be opened for the call: {last_error}")

    def _open_output_stream(self, *, allow_exclusive: bool = True):
        last_error = None
        exclusive_attempts = (
            [True, False] if (self._config.exclusive_output and allow_exclusive) else [False]
        )
        for attempt_index, exclusive in enumerate(exclusive_attempts):
            for device in self._candidate_devices(
                self._config.output_device_name, input_device=False,
                include_fallbacks=not exclusive,
            ):
                for rate in self._candidate_rates(device):
                    try:
                        extra_settings = self._stream_extra_settings(
                            device, input_device=False, exclusive=exclusive
                        )
                        # The exclusive pass may only open a device exclusive
                        # mode actually APPLIES to. A non-WASAPI candidate gets
                        # extra_settings=None and would open "successfully"
                        # right here -- so with the WASAPI twin refusing
                        # exclusive access (common), the MME entry behind it
                        # won the exclusive pass and the shared pass, where the
                        # same WASAPI device would have opened, never ran.
                        # Measured live: exclusive_input on put the microphone
                        # on MME at ~90 ms instead of shared WASAPI at ~3 ms.
                        if exclusive and extra_settings is None:
                            continue
                        stream = self._sd.OutputStream(
                            samplerate=rate,
                            # blocksize=0 lets PortAudio use the device's own
                            # period, which on WASAPI is the only size it can
                            # serve without an extra conversion buffer. A
                            # fixed 20 ms block here made every device pretend
                            # to a period it did not have. Pacing comes from
                            # the reservoir the callback drains, not from the
                            # block size.
                            blocksize=0,
                            device=device,
                            channels=1,
                            dtype="float32",
                            latency="low",
                            extra_settings=extra_settings,
                            callback=self._on_output_frames,
                        )
                        if attempt_index > 0:
                            logging.info(
                                "[call_audio] exclusive mode unavailable, fell back to shared mode for output"
                            )
                        # Actually applied, not merely requested — see the
                        # matching note in _open_input_stream().
                        logging.info(
                            "[call_audio] output opened device=%r hostapi=%s rate=%s latency=%r exclusive=%s",
                            device,
                            self._hostapi_name(device) if device is not None else "default",
                            rate,
                            getattr(stream, "latency", "low"),
                            bool(exclusive and extra_settings is not None),
                        )
                        return stream, rate
                    except Exception as exc:
                        last_error = exc
                        self._log_candidate_failure(
                            "output", device, rate, exclusive, exc
                        )
        raise CallAudioUnavailable(f"No speaker could be opened for the call: {last_error}")

    def _log_candidate_failure(self, direction, device, rate, exclusive, exc) -> None:
        """Say WHY a candidate lost, instead of swallowing it into last_error.

        Measured 2026-09-22: on one machine the same headset opened on WASAPI
        (device 14, 40 ms) for one call and on MME (device 6, 100 ms) for the
        next, with no setting changed in between. _wasapi_twin() picks the
        WASAPI entry first, so the WASAPI open must have failed — and nothing
        anywhere said so, because only a total failure of the whole matrix was
        ever reported. This only fires on a failure, so it cannot spam, and it
        names device INDEXES and host APIs, never the device name (PII rule:
        a device name can carry the owner's own name).
        """
        logging.info(
            "[call_audio] %s candidate failed device=%r hostapi=%s rate=%s exclusive=%s error=%s",
            direction,
            device,
            self._hostapi_name(device) if device is not None else "default",
            rate,
            exclusive,
            exc,
        )

    def _on_microphone_frame(self, source_rate: int):
        def _callback(indata, _frames, _time_info, status):
            if status:
                logging.debug("[call_audio] microphone status: %s", status)
            if self._stop_event.is_set() or indata is None:
                return
            samples = _resample_mono(np.asarray(indata), source_rate, CALL_SAMPLE_RATE)
            if samples.size:
                self._put_drop_oldest(self._mic_queue, _pcm16_bytes(samples))

        return _callback

    def _dequeue_fresh_microphone_frame(self) -> tuple[bytes, int]:
        """Return current microphone audio instead of replaying stale backlog."""
        pcm = self._mic_queue.get(timeout=0.1)
        dropped = 0
        while self._mic_queue.qsize() > CALL_MIC_TARGET_BACKLOG_FRAMES:
            try:
                pcm = self._mic_queue.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        return pcm, dropped

    def _send_microphone_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                pcm, dropped = self._dequeue_fresh_microphone_frame()
            except queue.Empty:
                continue

            if dropped:
                previous_dropped = self._mic_frames_dropped_for_latency
                self._mic_frames_dropped_for_latency += dropped
                if previous_dropped == 0 or (
                    previous_dropped // 50
                    != self._mic_frames_dropped_for_latency // 50
                ):
                    logging.info(
                        "[call_audio] skipped stale microphone audio frames=%s total=%s backlog=%s",
                        dropped,
                        self._mic_frames_dropped_for_latency,
                        self._mic_queue.qsize(),
                    )

            try:
                if self._microphone_muted:
                    pcm = b"\x00" * len(pcm)
                self._sio.emit(
                    "call:audio:mic",
                    {
                        "session": self._config.session,
                        "sampleRate": CALL_SAMPLE_RATE,
                        "encoding": "base64",
                        "pcm": base64.b64encode(pcm).decode("ascii"),
                    },
                )
                self._mic_frames_sent += 1
                self._mic_bytes_sent += len(pcm)
                if self._mic_frames_sent == 1 or self._mic_frames_sent % 50 == 0:
                    logging.info(
                        "[call_audio] microphone sent session=%s frames=%s bytes=%s",
                        self._config.session, self._mic_frames_sent, self._mic_bytes_sent,
                    )
            except Exception:
                logging.exception("[call_audio] failed to send microphone audio")
                time.sleep(0.05)

    def _cancel_echo(self, pcm: bytes) -> bytes:
        """Run one microphone chunk through the canceller.

        May return fewer bytes than it was given (whole blocks only) or none.
        """
        canceller = self._echo_canceller
        while True:
            try:
                chunk, rate = self._echo_reference_tap.popleft()
            except IndexError:
                break
            canceller.push_reference(_resample_mono(chunk, rate, CALL_SAMPLE_RATE))
        return _pcm16_bytes(canceller.process(_pcm16_float32(pcm)))

    def _on_output_frames(self, outdata, _frames, _time_info, _status) -> None:
        """PortAudio's realtime thread asking for one device period.

        Everything expensive or fallible happens in the player thread; this
        only copies out of the reservoir. It must not raise (PortAudio aborts
        the stream), must not block, and must not log — all three are audible.
        """
        try:
            view = outdata[:, 0] if getattr(outdata, "ndim", 1) > 1 else outdata
            self._output_buffer.fill(view)
            if self._echo_canceller is not None:
                self._echo_reference_tap.append((view.copy(), self._output_rate))
        except Exception:
            # Silence is the only safe answer left; the underrun counter and
            # the player thread's summary are what make it visible.
            try:
                outdata[:] = 0
            except Exception:
                pass

    def _play_remote_loop(self) -> None:
        """Resample each remote packet into the playback reservoir.

        Playback itself is the OutputStream's own callback
        (_on_output_frames), draining _OutputJitterBuffer. Before this, the
        loop wrote each arriving packet straight into a blocking stream whose
        device buffer held barely two 20 ms periods: a few milliseconds of
        arrival jitter — routine, since the page taps the remote track with a
        deprecated main-thread createScriptProcessor and the packets cross
        Socket.IO — underflowed the device, and every underflow is a click.
        Hardware with a deep driver buffer hid it; a low-latency USB headset
        (CORSAIR HS80, reported 2026-09-22) popped continuously.

        **A reservoir in front of the output is the exact component the
        2026-09-20 bisection blamed, and it is back on purpose. Watch for it.**
        Be accurate about what that bisection established: swapping this file
        for main's version fixed severely choppy audio where three targeted
        fixes had not, which isolated the defect to the reservoir rewrite as a
        whole, and c78fe4c7 says in as many words that the root cause was
        never pinned down further. So this is a deliberate re-test of the
        suspect component with the part it could never be separated from taken
        away. The reverted version (0f11af9c) was hard capped at
        CALL_OUTPUT_MAX_BUFFER_MS and dropped its oldest samples too — those
        are not what is new here. What is new is that nothing blocks and
        nothing paces: it drained its reservoir through blocking
        stream.write() calls, and when the device's own buffer turned out to
        be five frames deep those writes did not block, so frames landed in
        bursts (3d507ce1 then paced them by wall clock, and that did not fix
        it either). Here there is no write call at all — PortAudio asks for
        one device period whenever it needs one, and that request rate is the
        only clock in the path. If choppiness returns, this docstring is the
        first place to look, and the next step is data (underrun counts,
        device period, reported latency), not another guess.
        """
        while not self._stop_event.is_set():
            try:
                pcm, source_rate = self._output_queue.get(timeout=0.1)
            except queue.Empty:
                self._log_output_underruns()
                continue
            try:
                samples = _pcm16_float32(pcm)
                # The lock still guards the stream/rate pair against a swap to
                # the exclusive stream mid-packet; the reservoir has its own.
                with self._output_lock:
                    if self._output_stream is None:
                        continue
                    target_rate = self._output_rate
                samples = _resample_mono(samples, source_rate, target_rate)
                if samples.size:
                    self._output_buffer.push(samples)
            except Exception:
                logging.exception("[call_audio] failed to play remote call audio")
                time.sleep(0.05)
            self._log_output_underruns()

    def _log_output_underruns(self) -> None:
        """Report starvation from OUTSIDE the callback, rate limited."""
        underruns = self._output_buffer.underruns
        if not underruns:
            return
        if self._output_underruns_logged and (
            underruns - self._output_underruns_logged < CALL_OUTPUT_UNDERRUN_LOG_EVERY
        ):
            return
        self._output_underruns_logged = underruns
        logging.info(
            "[call_audio] remote audio starved underruns=%s dropped_samples=%s buffered=%s",
            underruns,
            self._output_buffer.dropped_samples,
            self._output_buffer.buffered_samples,
        )

    def _emit_start(self) -> None:
        try:
            self._sio.emit("call:audio:start", {"session": self._config.session})
        except Exception:
            logging.debug("[call_audio] could not emit call:audio:start", exc_info=True)

    def _emit_stop(self) -> None:
        try:
            self._sio.emit("call:audio:stop", {"session": self._config.session})
        except Exception:
            logging.debug("[call_audio] could not emit call:audio:stop", exc_info=True)

    @staticmethod
    def _close_stream(stream) -> None:
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            logging.debug("[call_audio] failed to stop stream", exc_info=True)
        try:
            stream.close()
        except Exception:
            # WARNING, not DEBUG: a stream that failed to close is still
            # running its callback against the reservoir its replacement is
            # about to reconfigure, and it still holds the device -- under
            # exclusive_output, that is the screen reader silenced for the
            # rest of the session with nothing in the log to say why.
            logging.warning("[call_audio] failed to close stream", exc_info=True)

    @staticmethod
    def _drain_queue(items: queue.Queue) -> None:
        while True:
            try:
                items.get_nowait()
            except queue.Empty:
                return

    @staticmethod
    def _put_drop_oldest(items: queue.Queue, item) -> None:
        try:
            items.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            items.get_nowait()
        except queue.Empty:
            pass
        try:
            items.put_nowait(item)
        except queue.Full:
            pass
