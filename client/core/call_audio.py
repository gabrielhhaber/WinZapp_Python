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
from dataclasses import dataclass
from typing import Optional

import numpy as np

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
        self._stop_event = threading.Event()
        self._sender_thread: Optional[threading.Thread] = None
        self._player_thread: Optional[threading.Thread] = None
        self._mic_frames_sent = 0
        self._mic_bytes_sent = 0
        self._mic_frames_dropped_for_latency = 0
        self._microphone_muted = False

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

    def start_output_only(self) -> None:
        """Open receive audio while ringing without opening the microphone."""
        if self.output_running:
            return
        if self._sd is None:
            raise CallAudioUnavailable("sounddevice is not available in this Python runtime")

        self._stop_event.clear()
        self._output_stream, self._output_rate = self._open_output_stream()
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
            # never sees it. Under exclusive_mode that stranded stream holds
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

    def stop(self) -> None:
        self._stop_event.set()
        self._emit_stop()
        self._close_stream(self._input_stream)
        self._close_stream(self._output_stream)
        self._input_stream = None
        self._output_stream = None
        self._drain_queue(self._mic_queue)
        self._drain_queue(self._output_queue)

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
        return " ".join(str(value or "").replace("(", "").replace(")", "").lower().split())

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

    def _candidate_devices(self, stored_name: str, *, input_device: bool):
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
        if default_device is not None:
            twin = self._wasapi_twin(default_device, input_device=input_device)
            if twin is not None:
                ordered.append(twin)
            ordered.append(default_device)
        # Keep PortAudio's implicit default as a compatibility fallback, but
        # prefer the concrete default index so we can inspect its native rate.
        ordered.append(None)
        for index in ordered:
            key = -1 if index is None else int(index)
            if key not in yielded:
                yielded.add(key)
                yield index
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
    # a real, deliberate feature (settings["call_audio_devices"]["exclusive_mode"]),
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
            for device in self._candidate_devices(self._config.input_device_name, input_device=True):
                for rate in self._candidate_rates(device):
                    try:
                        extra_settings = self._stream_extra_settings(
                            device, input_device=True, exclusive=exclusive
                        )
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
        raise CallAudioUnavailable(f"No microphone could be opened for the call: {last_error}")

    def _open_output_stream(self):
        last_error = None
        exclusive_attempts = [True, False] if self._config.exclusive_output else [False]
        for attempt_index, exclusive in enumerate(exclusive_attempts):
            for device in self._candidate_devices(self._config.output_device_name, input_device=False):
                for rate in self._candidate_rates(device):
                    try:
                        extra_settings = self._stream_extra_settings(
                            device, input_device=False, exclusive=exclusive
                        )
                        stream = self._sd.OutputStream(
                            samplerate=rate,
                            blocksize=max(1, int(rate * CALL_FRAME_MS / 1000)),
                            device=device,
                            channels=1,
                            dtype="float32",
                            latency="low",
                            extra_settings=extra_settings,
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
        raise CallAudioUnavailable(f"No speaker could be opened for the call: {last_error}")

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

    def _play_remote_loop(self) -> None:
        """Write each remote packet to the device as soon as it arrives.

        Diagnostic bisection (2026-09-20): a prebuffer/reservoir rewrite of
        this loop, plus every other change made to this file for video
        calls, was suspected of causing severe choppy/high-latency call
        audio on a real Bluetooth headset. Reverting this whole file to
        main's original version (this exact loop included) while keeping
        every other file from the branch fixed it; reintroducing sample-rate
        priority, "low" latency, exclusive mode and a wall-clock write-pacing
        layer on top of the reservoir version individually did not. That
        isolates the defect to the reservoir/prebuffer mechanism itself, not
        yet root-caused further — so this loop stays exactly as simple as it
        was before any of that, one packet in, one write out, relying on the
        network's own arrival rate to pace playback the same way it always
        did for voice calls before this file changed.
        """
        while not self._stop_event.is_set():
            try:
                pcm, source_rate = self._output_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                samples = _pcm16_float32(pcm)
                samples = _resample_mono(samples, source_rate, self._output_rate)
                if samples.size and self._output_stream is not None:
                    self._output_stream.write(samples.reshape(-1, 1))
            except Exception:
                logging.exception("[call_audio] failed to play remote call audio")
                time.sleep(0.05)

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
            logging.debug("[call_audio] failed to close stream", exc_info=True)

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
