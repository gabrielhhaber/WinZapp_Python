"""Python-owned microphone and speaker transport for WhatsApp voice calls.

Chromium keeps WhatsApp signaling, encryption, and WebRTC. It never opens the
physical microphone: Python captures PCM, forwards it to the page bridge, and
plays the PCM extracted from the remote WebRTC track.
"""

from __future__ import annotations

import base64
import logging
import queue
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
CALL_MIC_QUEUE_LIMIT = 75
CALL_OUTPUT_QUEUE_LIMIT = 40


@dataclass(frozen=True)
class CallAudioConfig:
    session: str
    input_device_name: str = ""
    output_device_name: str = ""


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

    def start(self) -> None:
        if self.running:
            return
        if self._sd is None:
            raise CallAudioUnavailable("sounddevice is not available in this Python runtime")

        self._stop_event.clear()
        self._input_stream, self._input_rate = self._open_input_stream()
        try:
            self._output_stream, self._output_rate = self._open_output_stream()
        except Exception:
            self._close_stream(self._input_stream)
            self._input_stream = None
            raise

        self._input_stream.start()
        self._output_stream.start()
        self._sender_thread = threading.Thread(
            target=self._send_microphone_loop,
            name="WinZappCallMicSender",
            daemon=True,
        )
        self._player_thread = threading.Thread(
            target=self._play_remote_loop,
            name="WinZappCallRemotePlayer",
            daemon=True,
        )
        self._sender_thread.start()
        self._player_thread.start()

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

    def _candidate_devices(self, stored_name: str, *, input_device: bool):
        preferred = self._resolve_device(stored_name, input_device=input_device)
        yielded = set()
        for index in (preferred, None):
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

    def _open_input_stream(self):
        last_error = None
        for device in self._candidate_devices(self._config.input_device_name, input_device=True):
            for rate in self._candidate_rates(device):
                try:
                    stream = self._sd.InputStream(
                        samplerate=rate,
                        blocksize=max(1, int(rate * CALL_FRAME_MS / 1000)),
                        device=device,
                        channels=1,
                        dtype="float32",
                        latency="low",
                        callback=self._on_microphone_frame(rate),
                    )
                    logging.info("[call_audio] input opened device=%r rate=%s", device, rate)
                    return stream, rate
                except Exception as exc:
                    last_error = exc
        raise CallAudioUnavailable(f"No microphone could be opened for the call: {last_error}")

    def _open_output_stream(self):
        last_error = None
        for device in self._candidate_devices(self._config.output_device_name, input_device=False):
            for rate in self._candidate_rates(device):
                try:
                    stream = self._sd.OutputStream(
                        samplerate=rate,
                        blocksize=max(1, int(rate * CALL_FRAME_MS / 1000)),
                        device=device,
                        channels=1,
                        dtype="float32",
                        latency="low",
                    )
                    logging.info("[call_audio] output opened device=%r rate=%s", device, rate)
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

    def _send_microphone_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                pcm = self._mic_queue.get(timeout=0.1)
            except queue.Empty:
                continue
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
