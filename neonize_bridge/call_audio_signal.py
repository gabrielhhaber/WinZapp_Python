"""Content-free s16le mono statistics; levels do not establish audibility.

Each add is one frame. AC is centered within that frame, and DC power comes
from its mean; opposite-signed constant frames therefore never cancel to AC.
Callers own synchronization, reporting cadence, and the measured stage.
"""
import math
import struct


def _dbfs(power):
    if power <= 0:
        return -120.0
    return max(-120.0, round(10 * math.log10(power / (32768 * 32768)), 1))


class SignalWindow:
    """Retain numeric aggregates only, never PCM or individual samples.

    Accept at most 500 ms per add at 16/48 kHz. Frames/samples count only
    accepted inputs. No-data levels are None; accepted digital silence has
    levels at -120 dBFS. Full-scale boundary counts are not clipping proof.
    """
    __slots__ = ("_frames", "_samples", "_invalid_frames", "_zero_frames",
                 "_full_scale_samples", "_sum_squares", "_ac_energy",
                 "_dc_energy", "_peak", "_by_rate")

    def __init__(self):
        self._reset()

    def _reset(self):
        self._frames = self._samples = self._invalid_frames = 0
        self._zero_frames = self._full_scale_samples = 0
        self._sum_squares = self._peak = 0
        self._ac_energy = self._dc_energy = 0.0
        self._by_rate = {16000: 0, 48000: 0}

    def add(self, pcm, sample_rate):
        """Return validity without changing audio or retaining the input."""
        if type(sample_rate) is not int or sample_rate not in self._by_rate:
            self._invalid_frames += 1
            return False
        try:
            view = memoryview(pcm)
            if not view.c_contiguous:
                raise ValueError("noncontiguous PCM")
            view = view.cast("B")
            size = view.nbytes
            if not size or size % 2 or size > sample_rate:
                raise ValueError("invalid PCM frame size")
        except (TypeError, ValueError, BufferError):
            self._invalid_frames += 1
            return False

        total = squares = peak = boundary = 0
        for (value,) in struct.iter_unpack("<h", view):
            total += value
            squares += value * value
            peak = max(peak, abs(value))
            boundary += value == -32768 or value == 32767
        count = size // 2
        # Integer arithmetic avoids cancellation of nearly constant frames.
        variance_numerator = count * squares - total * total
        self._ac_energy += variance_numerator / count
        self._dc_energy += (total * total) / count
        self._sum_squares += squares
        self._peak = max(self._peak, peak)
        self._frames += 1
        self._samples += count
        self._by_rate[sample_rate] += count
        self._zero_frames += peak == 0
        self._full_scale_samples += boundary
        return True

    def summary(self, reset=False):
        """Return a detached JSON-safe aggregate, optionally clear the window."""
        count = self._samples
        result = {
            "frames": self._frames,
            "samples": count,
            "duration_ms": round(sum(n * 1000 / rate for rate, n in self._by_rate.items()), 3),
            "rates_hz": [rate for rate, n in self._by_rate.items() if n],
            "invalid_frames": self._invalid_frames,
            "zero_frames": self._zero_frames,
            "full_scale_samples": self._full_scale_samples,
            "rms_dbfs": _dbfs(self._sum_squares / count) if count else None,
            "ac_rms_dbfs": _dbfs(self._ac_energy / count) if count else None,
            "dc_rms_dbfs": _dbfs(self._dc_energy / count) if count else None,
            "peak_dbfs": _dbfs(self._peak * self._peak) if count else None,
        }
        if reset:
            self._reset()
        return result
