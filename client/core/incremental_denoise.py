"""Incremental neural noise reduction while a voice message is being recorded.

The offline path (core.neural_noise_reduction) waits for the finished PCM and
then spends seconds of model time at send. This module runs the exact same
framing as the recording arrives: the recurrent state, the leading WINDOW of
zeros, the hop/overlap-add schedule and the MODEL_DELAY crop all mirror
NeuralDenoiser._enhance_48k, and the rate conversions feed the same
StreamingMonoResampler chunk by chunk (its output count and phase arithmetic
are packet-size independent by contract). The cleaned PCM16 is therefore
bit-exact against the offline path — covered by
tests/test_incremental_denoise.py against the real upstream code.

``spool_path`` streams the cleaned audio to a temporary file as it is produced
(the raw capture is retained by the caller exactly as before — total RAM then
matches the current recording profile, and the send-time processing peak and
wait go away). Without ``spool_path`` the cleaned PCM accumulates in memory.

No user-facing behaviour lives here: engine fallback, announcements and the
original/raw retention strategy stay in the wiring (voice_recording.py).
"""

from __future__ import annotations

import math

import numpy as np

from core import neural_noise_reduction as nn
from core.call_audio_resampling import StreamingMonoResampler

WINDOW = nn.WINDOW
HOP = nn.HOP
MODEL_DELAY = nn.MODEL_DELAY
MODEL_RATE = nn.MODEL_RATE


def _delay_skip(source: int, target: int) -> tuple[int, int]:
    """Same convention as nn._convert: FIR delay and initial samples to skip."""
    delay = math.ceil(32 * max(1, source / target))
    skip = round(delay * target / source)
    return delay, skip


class _ChannelStream:
    """One channel: input resample -> persistent-state hops -> output resample."""

    def __init__(self, denoiser: nn.NeuralDenoiser, rate: int):
        self.d = denoiser
        self.rate = rate
        self.state = denoiser._initial_state.copy()
        self.pending = np.zeros(WINDOW, np.float32)
        self.ola = np.zeros(WINDOW, np.float32)
        self.pos = 0
        self.r_in = StreamingMonoResampler() if rate != MODEL_RATE else None
        self.r_out = StreamingMonoResampler() if rate != MODEL_RATE else None
        self.delay_in, self.skip_in = _delay_skip(rate, MODEL_RATE)
        self.delay_out, self.skip_out = _delay_skip(MODEL_RATE, rate)
        self.fed48 = 0           # 48 kHz samples poured into the hop stream
        self.passed48 = 0        # enhanced 48 kHz samples emitted downstream
        self.out_pending = np.empty(0, np.float32)

    def _hop(self) -> None:
        si, sti, so, sto = self.d._names
        spec = np.fft.rfft(self.pending[:WINDOW] * nn._WINDOW_SHAPE)
        frame = np.stack((spec.real, spec.imag), -1).astype(np.float32)[None, None]
        enhanced, self.state = self.d._session.run([so, sto], {si: frame, sti: self.state})
        bins = enhanced[0, 0, :, 0] + 1j * enhanced[0, 0, :, 1]
        self.ola += (np.fft.irfft(bins, WINDOW) * nn._WINDOW_SHAPE).astype(np.float32)
        left = max(0, WINDOW + MODEL_DELAY - self.pos)
        if left < HOP:
            self._emit48(self.ola[left:HOP].copy())
        self.ola[:HOP] = self.ola[HOP:]
        self.ola[HOP:] = 0
        self.pending = self.pending[HOP:]
        self.pos += HOP

    def feed(self, x: np.ndarray, wanted48: int | None = None) -> None:
        if self.r_in is not None:
            y = self.r_in.process(np.asarray(x, np.float32), self.rate, MODEL_RATE)
            if self.skip_in:
                # Consume only what this chunk covers: a fragment smaller than
                # the pending skip must not drop the rest of the skip.
                k = min(self.skip_in, y.size)
                y = y[k:]
                self.skip_in -= k
        else:
            y = np.asarray(x, np.float32)
        if wanted48 is not None:
            y = y[: max(0, wanted48 - self.fed48)]
        self.fed48 += y.size
        if y.size:
            self.pending = np.concatenate((self.pending, y))
        while self.pending.size >= WINDOW:
            self._hop()

    def _emit48(self, e: np.ndarray) -> None:
        self.passed48 += e.size
        if self.r_out is not None:
            o = self.r_out.process(e, MODEL_RATE, self.rate)
            if self.skip_out:
                k = min(self.skip_out, o.size)
                o = o[k:]
                self.skip_out -= k
        else:
            o = e
        if o.size:
            self.out_pending = np.concatenate((self.out_pending, o))

    def finish(self, frames_in: int) -> None:
        wanted48 = frames_in * MODEL_RATE // self.rate
        # 1. Input resampler tail: the same zeros nn._convert appends.
        if self.r_in is not None:
            self.feed(np.zeros(self.delay_in + 1, np.float32), wanted48)
        # 2. Hop-stream flush with the offline path's exact tail
        #    (MODEL_DELAY + 2*WINDOW zeros); _enhance_48k crops the overrun
        #    with out[first:first+count], the caller crops the same way.
        self.pending = np.concatenate((self.pending, np.zeros(MODEL_DELAY + 2 * WINDOW, np.float32)))
        while self.pending.size >= WINDOW and self.passed48 < wanted48:
            self._hop()
        # 3. Output resampler tail.
        if self.r_out is not None:
            o = self.r_out.process(np.zeros(self.delay_out + 1, np.float32), MODEL_RATE, self.rate)
            if self.skip_out:
                k = min(self.skip_out, o.size)
                o = o[k:]
                self.skip_out -= k
            if o.size:
                self.out_pending = np.concatenate((self.out_pending, o))


class IncrementalDenoiser:
    """feed() interleaved PCM16 chunks as they are captured; finish() at send.

    Returns (spool_path | cleaned_bytes, "neural") from finish(). Raw-capture
    retention, engine fallback and announcements are the wiring's decisions.
    """

    def __init__(self, rate: int, channels: int, denoiser: nn.NeuralDenoiser,
                 spool_path: str | None = None):
        self.rate = rate
        self.channels = channels
        self.d = denoiser
        self._ch = [_ChannelStream(denoiser, rate) for _ in range(channels)]
        self._frames = 0
        self._leftover = b""
        self._file = open(spool_path, "wb") if spool_path else None
        self._retain = bytearray() if spool_path is None else None
        self._drained = 0

    def feed(self, pcm16: bytes) -> None:
        data = self._leftover + pcm16
        frame_bytes = 2 * self.channels
        frames = len(data) // frame_bytes
        self._leftover = data[frames * frame_bytes:]
        if not frames:
            return
        x = np.frombuffer(data[: frames * frame_bytes], dtype="<i2").reshape(frames, self.channels)
        x = x.astype(np.float32) / 32768.0
        for i, ch in enumerate(self._ch):
            ch.feed(x[:, i])
        self._frames += frames
        self._drain()

    def _drain(self) -> None:
        ready = min(c.out_pending.size for c in self._ch)
        if ready <= 0:
            return
        cols = []
        for c in self._ch:
            cols.append(c.out_pending[:ready])
            c.out_pending = c.out_pending[ready:]
        y = np.stack(cols, axis=1)
        if not np.isfinite(y).all():
            # Same guard as the offline path: never clip a blown-up output
            # into a full-scale spike.
            raise ValueError("the model produced non-finite samples")
        block = (np.clip(y, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
        if self._file is not None:
            self._file.write(block)
        else:
            self._retain += block
        self._drained += ready

    def finish(self) -> tuple[bytes | str, str]:
        for c in self._ch:
            c.finish(self._frames)
        remaining = self._frames - self._drained
        if remaining < 0:
            raise AssertionError("incremental denoise emitted past the recording length")
        for c in self._ch:
            c.out_pending = c.out_pending[:remaining]
        self._drain()
        if self._drained != self._frames:
            raise AssertionError(
                f"incremental denoise emitted {self._drained} of {self._frames} frames")
        if self._file is not None:
            if self._leftover:
                self._file.write(self._leftover)
            self._file.close()
            return self._file.name, "neural"
        return bytes(self._retain) + self._leftover, "neural"

    def abort(self) -> None:
        """Cancel without finishing (recording discarded or drain failed)."""
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
