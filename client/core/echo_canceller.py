"""Acoustic echo cancellation for the outgoing call microphone.

When the call plays through a speaker, the microphone hears the other person
and sends them their own voice back. The far-end signal is known exactly (it is
what the output callback played), so an adaptive filter learns the speaker-to-
microphone path and subtracts its estimate from the captured audio.

Partitioned-block frequency-domain NLMS (overlap-save), mono, 48 kHz, 20 ms
blocks, 16 partitions = 320 ms of echo tail. Pure numpy and independent of
sounddevice and wx, so it is testable with synthetic signals.
"""

from __future__ import annotations

import threading

import numpy as np

AEC_SAMPLE_RATE = 48_000
AEC_BLOCK = 960
AEC_PARTITIONS = 16
# Reference audio older than this many blocks is dropped: a backlog that deep
# means the two device clocks drifted apart, and a stale reference cancels
# nothing (the filter's tail is only PARTITIONS blocks long).
AEC_MAX_REFERENCE_BLOCKS = 6


class EchoCanceller:
    """Streaming echo canceller: push far-end audio, process near-end audio."""

    def __init__(self, *, block: int = AEC_BLOCK, partitions: int = AEC_PARTITIONS,
                 step: float = 0.6):
        self._n = block
        self._p = partitions
        self._step = step
        self._bins = block + 1
        self._weights = np.zeros((partitions, self._bins), dtype=np.complex128)
        self._spectra = np.zeros((partitions, self._bins), dtype=np.complex128)
        self._ref_prev = np.zeros(block, dtype=np.float64)
        self._power = np.full(self._bins, 1e-6)
        self._lock = threading.Lock()
        self._ref_buffer = np.empty(0, dtype=np.float32)
        self._mic_buffer = np.empty(0, dtype=np.float32)

    def reset(self) -> None:
        with self._lock:
            self._weights[:] = 0
            self._spectra[:] = 0
            self._ref_prev[:] = 0
            self._power[:] = 1e-6
            self._ref_buffer = np.empty(0, dtype=np.float32)
            self._mic_buffer = np.empty(0, dtype=np.float32)

    def push_reference(self, samples: np.ndarray) -> None:
        """Queue far-end audio (48 kHz mono float32) as the device played it."""
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        if not data.size:
            return
        with self._lock:
            self._ref_buffer = np.concatenate((self._ref_buffer, data))
            limit = AEC_MAX_REFERENCE_BLOCKS * self._n
            if self._ref_buffer.size > limit:
                self._ref_buffer = self._ref_buffer[-limit:]

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Return echo-reduced microphone audio.

        Input of any length is accepted; output is produced in whole blocks, so
        it can be shorter than the input (the remainder waits for the next
        call). Length is conserved over the stream.
        """
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        with self._lock:
            self._mic_buffer = np.concatenate((self._mic_buffer, data))
            out = []
            while self._mic_buffer.size >= self._n:
                mic = self._mic_buffer[: self._n]
                self._mic_buffer = self._mic_buffer[self._n:]
                if self._ref_buffer.size >= self._n:
                    ref = self._ref_buffer[: self._n]
                    self._ref_buffer = self._ref_buffer[self._n:]
                else:
                    ref = np.zeros(self._n, dtype=np.float32)
                    ref[: self._ref_buffer.size] = self._ref_buffer
                    self._ref_buffer = np.empty(0, dtype=np.float32)
                out.append(self._process_block(mic, ref))
        if not out:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(out)

    def _process_block(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        n = self._n
        mic64 = mic.astype(np.float64)
        ref64 = ref.astype(np.float64)

        window = np.concatenate((self._ref_prev, ref64))
        self._ref_prev = ref64
        self._spectra = np.roll(self._spectra, 1, axis=0)
        self._spectra[0] = np.fft.rfft(window)

        ref_energy = float(np.mean(ref64 * ref64))
        echo = np.fft.irfft(
            np.sum(self._weights * self._spectra, axis=0), 2 * n
        )[n:]
        error = mic64 - echo

        # Nothing was played: there is no echo to learn or remove.
        if ref_energy < 1e-9:
            return mic.astype(np.float32, copy=False)

        mic_energy = float(np.mean(mic64 * mic64))
        err_energy = float(np.mean(error * error))
        echo_energy = float(np.mean(echo * echo))
        # Near-end speech leaves the error large and would drag the filter
        # away from the echo path, so the step shrinks as the error grows
        # relative to what the filter already explains. A floor keeps the
        # first convergence possible, when the filter explains nothing yet.
        confidence = echo_energy / (echo_energy + err_energy + 1e-12)
        step = self._step * max(0.15, min(1.0, confidence * 2.0))
        # Divergence guard: the filter made things worse than no filter.
        if err_energy > 2.0 * mic_energy:
            self._weights *= 0.5
            return mic.astype(np.float32, copy=False)

        power = np.sum(np.abs(self._spectra) ** 2, axis=0)
        self._power = 0.9 * self._power + 0.1 * power
        error_spectrum = np.fft.rfft(np.concatenate((np.zeros(n), error)))
        gradient = np.conj(self._spectra) * error_spectrum / (self._power + 1e-3)
        self._weights += step * gradient
        # Overlap-save constraint: each partition's impulse response is n long.
        time_domain = np.fft.irfft(self._weights, 2 * n, axis=1)
        time_domain[:, n:] = 0.0
        self._weights = np.fft.rfft(time_domain, axis=1)

        return np.clip(error, -1.0, 1.0).astype(np.float32)
