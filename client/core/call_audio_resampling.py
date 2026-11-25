"""Stateful, band-limited mono conversion for the two ends of a call.

The causal windowed-sinc FIR keeps fractional position and history across
packets. Its delay is ``ceil(32 * max(1, source / target)) / source`` seconds
(at most 4 ms when one end is the 48 kHz call rate). No tail is flushed on a
device change: old speech belongs to the old stream. Run on audio workers,
never the PortAudio callback. The same-rate path is untouched float32 PCM.
"""

from __future__ import annotations

import math

import numpy as np


class StreamingMonoResampler:
    """Causal Blackman-windowed sinc with an exact rational sample clock.

    Output count is floor(total_input * target / source), independent of
    packet sizes. Initial zero history accounts for the fixed filter delay;
    audio is delayed, never extended by rounding every packet independently.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._rates = None
        self._history = np.empty(0, dtype=np.float32)
        self._total_in = self._total_out = 0
        self._phases = None

    def _configure(self, source: int, target: int) -> None:
        # Bound memory/work for malformed remote rate metadata as well as
        # accepting ordinary 8/16/32/44.1/48/96/192/384 kHz endpoints.
        if not (8_000 <= source <= 384_000 and 8_000 <= target <= 384_000):
            raise ValueError("Call sample rates must be between 8000 and 384000 Hz")
        self.reset()
        self._rates = (source, target)
        if source == target:
            return
        divisor = math.gcd(source, target)
        self._up, self._down = target // divisor, source // divisor
        self._radius = math.ceil(32 * max(1.0, source / target))
        self._offsets = np.arange(-self._radius + 1, self._radius + 1)
        self._cutoff = 0.94 * min(1.0, target / source)
        # The first output after a packet can still refer to an input step
        # before that packet. Keep that whole decimation step as well as FIR
        # support (e.g. 48 -> 8 kHz with non-multiple-of-six packet lengths).
        self._history_size = 2 * self._radius + math.ceil(source / target) + 2
        self._history = np.zeros(self._history_size, dtype=np.float32)
        # Common rational rates have <=160 phases. Unusual default rates
        # must not allocate a table with hundreds of thousands of rows.
        if self._up <= 512:
            self._phases = self._weights(np.arange(self._up))

    def _weights(self, phases: np.ndarray) -> np.ndarray:
        distance = self._offsets[None, :] - phases[:, None] / self._up
        angle = np.pi * distance / self._radius
        window = 0.42 + 0.5 * np.cos(angle) + 0.08 * np.cos(2 * angle)
        window[np.abs(distance) > self._radius] = 0
        weights = np.sinc(distance * self._cutoff) * window * self._cutoff
        # Preserve DC/voice level at every fractional phase.
        return weights / weights.sum(axis=1, keepdims=True)

    def skip(self, samples: int, source_rate: int, target_rate: int) -> int:
        """Advance across discarded capture, clearing speech but not phase."""
        rates = (int(source_rate), int(target_rate))
        if self._rates != rates:
            self._configure(*rates)
        if rates[0] == rates[1]:
            return samples
        self._total_in += samples
        wanted = self._total_in * self._up // self._down
        skipped = wanted - self._total_out
        self._total_out = wanted
        self._history.fill(0)
        return int(skipped)

    def process(self, samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
        data = np.asarray(samples, dtype=np.float32)
        if data.ndim > 1:
            data = data.mean(axis=1, dtype=np.float32)
        data = data.reshape(-1)
        rates = (int(source_rate), int(target_rate))
        if self._rates != rates:
            self._configure(*rates)
        if not data.size or rates[0] == rates[1]:
            return data

        origin = self._total_in - self._history.size
        available = np.concatenate((self._history, data))
        self._total_in += data.size
        wanted = self._total_in * self._up // self._down
        output = np.empty(wanted - self._total_out, dtype=np.float32)
        # Bounded temporaries even when a remote packet contains much more
        # than the normal 1024 samples. Integer arithmetic avoids clock drift.
        for start in range(self._total_out, wanted, 512):
            end = min(start + 512, wanted)
            clock = np.arange(start, end, dtype=np.int64) * self._down
            centers = clock // self._up - self._radius
            phases = clock % self._up
            weights = self._phases[phases] if self._phases is not None else self._weights(phases)
            positions = centers[:, None] + self._offsets - origin
            output[start - self._total_out:end - self._total_out] = (
                available[positions] * weights
            ).sum(axis=1)
        self._total_out = wanted
        self._history = available[-self._history_size:].copy()
        return output


class MicrophonePCMEncoder:
    """Keep converted microphone blocks at the call's 20 ms frame size.

    Captured float32 stays float32 until the caller encodes transport PCM16.
    The direct call-rate path preserves its original callback boundaries.
    """

    def __init__(self, rate: int, frame_samples: int):
        self._rate, self._frame_samples = rate, frame_samples
        self._resampler = StreamingMonoResampler()
        self._source_rate = None
        self._pending = np.empty(0, dtype=np.float32)

    def reset(self) -> int:
        discarded = self._pending.size
        self._pending = np.empty(0, dtype=np.float32)
        self._source_rate = None
        self._resampler.reset()
        return int(discarded)

    def skip(self, samples: int, source_rate: int) -> int:
        if self._source_rate != source_rate:
            self.reset()
            self._source_rate = source_rate
        discarded = self._pending.size + self._resampler.skip(samples, source_rate, self._rate)
        self._pending = np.empty(0, dtype=np.float32)
        return int(discarded)

    def process(self, samples: np.ndarray, source_rate: int) -> list[np.ndarray]:
        if self._source_rate != source_rate:
            self.reset()
            self._source_rate = source_rate
        data = self._resampler.process(samples, source_rate, self._rate)
        if source_rate == self._rate:
            return [data] if data.size else []
        pending = np.concatenate((self._pending, data))
        end = pending.size // self._frame_samples * self._frame_samples
        frames = [pending[i:i + self._frame_samples] for i in range(0, end, self._frame_samples)]
        self._pending = pending[end:].copy()
        return frames


class PerSenderResampler:
    """One FIR history per timed sender of a group call.

    Group senders arrive interleaved; one shared history would either mix
    their tails or, reset on every change of sender, click at each packet.
    A sender whose rate or codec clock changes starts a fresh history. The
    least recently heard sender is dropped past ``limit``.
    """

    def __init__(self, limit: int = 32):
        self._limit = limit
        self._senders: dict = {}

    def reset(self) -> None:
        self._senders.clear()

    def process(self, sender, source, samples, source_rate: int, target_rate: int) -> np.ndarray:
        entry = self._senders.pop(sender, None)
        if entry is None or entry[0] != source:
            entry = (source, StreamingMonoResampler())
            while len(self._senders) >= self._limit:
                self._senders.pop(next(iter(self._senders)))
        self._senders[sender] = entry  # most recently heard last
        return entry[1].process(samples, source_rate, target_rate)
