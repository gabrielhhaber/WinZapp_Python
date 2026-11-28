"""The microphone half of dictation: PCM into memory, nothing on disk.

Opening and closing a PortAudio stream can block for seconds while the driver
negotiates, so ``open()`` and ``finish()`` are for a worker thread, never the
wx one. It has an instance of PyAudio of its own, so it never touches the voice
recording's (``_recording_pa``); a recording or a call in progress is refused
before this is reached (``core/dictation.refusal_key``).

The device is the one the person chose in Settings (the name the window hands
in, "" for the system default), with the same fallbacks the voice recording
has: the system default, then every enumerated device. The (rate, channels)
list is ``recording_configs_for()``'s, so a microphone that opens for a voice
message opens here. Mono is asked first: Whisper listens in mono.
"""

from __future__ import annotations

import logging
import threading

from core import dictation
from core.audio_devices import (
    fallback_input_device_indices,
    find_input_device_index,
    recording_configs_for,
)

try:
    import pyaudio
except ImportError:  # no wheel for this Python: the panel says "unavailable"
    pyaudio = None

_FRAMES_PER_BUFFER = 4096


def is_available() -> bool:
    return pyaudio is not None


class MicCapture:
    """One dictation's microphone. ``chunks`` fills from PortAudio's thread."""

    def __init__(self, device_name="", on_full=None, audio_module=None):
        self._device_name = device_name or ""
        self._on_full = on_full
        self._audio = audio_module or pyaudio
        self._pa = None
        self._stream = None
        self._lock = threading.Lock()
        self._bytes = 0
        self._full = False
        self.chunks = []
        self.rate = 0
        self.channels = 0

    # ── Worker thread ────────────────────────────────────────────────────────

    def open(self) -> bool:
        """Open the stream and start capturing; False when no device would."""
        if self._audio is None:
            return False
        try:
            self._pa = self._audio.PyAudio()
            index = (find_input_device_index(self._device_name, self._pa)
                     if self._device_name else None)
            if self._try_open(index):
                return True
            if index is not None and self._try_open(None):
                return True
            for other in fallback_input_device_indices(self._pa, exclude=(index,)):
                if self._try_open(other):
                    logging.info("[dictation] opened an enumerated input device instead")
                    return True
        except Exception as exc:
            # The type only: a driver's text can name the device.
            logging.warning("[dictation] could not open the microphone: %s",
                            type(exc).__name__)
        self.close()
        return False

    def finish(self) -> list:
        """Stop and release the device; the PCM chunks captured."""
        self._release()
        with self._lock:
            chunks, self.chunks = self.chunks, []
        return chunks

    def close(self) -> None:
        """Stop and drop everything captured."""
        self._release()
        with self._lock:
            self.chunks = []

    # ── Internals ────────────────────────────────────────────────────────────

    def _try_open(self, device_index) -> bool:
        for rate, channels in recording_configs_for(device_index, self._pa,
                                                    prefer_stereo=False):
            try:
                stream = self._pa.open(
                    rate=rate, channels=channels, format=self._audio.paInt16,
                    input=True, input_device_index=device_index,
                    frames_per_buffer=_FRAMES_PER_BUFFER,
                    stream_callback=self._callback,
                )
                self.rate, self.channels = rate, channels
                stream.start_stream()
                self._stream = stream
                return True
            except Exception:
                continue
        return False

    def _callback(self, in_data, frame_count, time_info, status):
        # PortAudio's thread: keep it short.
        notify = False
        with self._lock:
            if not self._full:
                self.chunks.append(in_data)
                self._bytes += len(in_data)
                if dictation.capture_seconds(
                        self._bytes, self.rate, self.channels) >= dictation.MAX_SECONDS:
                    self._full = notify = True
        if notify and self._on_full is not None:
            try:
                self._on_full()
            except Exception:
                pass
        return (None, getattr(self._audio, "paContinue", 0))

    def _release(self) -> None:
        stream, self._stream = self._stream, None
        pa, self._pa = self._pa, None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass
