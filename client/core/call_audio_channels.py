"""Mono call PCM across shared WASAPI mono/stereo device streams.

Channel capacity is only an opening candidate, not the Windows mix format.
Downmix runs on the sender, never PortAudio's borrowed-buffer callback.
"""
import logging

import numpy as np

# A 200 ms window decides a side only when one channel carries real signal
# (about -60 dBFS); quieter windows are noise or a noise gate's silence and
# keep the last decision, so hiss on a dead side is never picked over a gated
# microphone that is merely quiet.
_DECISION_POWER = 1e-6
# Activity on the excluded side is judged per 5 ms block against that side's
# own noise measured when it was excluded, never per sample: per-sample
# differences near a waveform peak are ~0 and let any hiss win the ratio.
_ACTIVITY_FLOOR = 1e-10
_ACTIVITY_NOISE_MARGIN = 10.0
_CROSSTALK_RATIO = 1000.0
# Hysteresis: a side excluded at 30 dB below the other comes back only once it
# is within 20 dB, so crosstalk sitting right at the limit does not flap.
_RESTORE_RATIO = 100.0


def shared_channel_counts(info, *, input_device):
    key = "max_input_channels" if input_device else "max_output_channels"
    try:
        return (2, 1) if int(info.get(key, 0)) >= 2 else (1,)
    except (TypeError, ValueError, OverflowError, AttributeError):
        return (1,)


def log_downmix_side(downmix, previous):
    """Log when the microphone mix starts or stops using one side alone.

    Runs on the sender thread, never the capture callback. Returns the side
    to compare against next time, so "my voice is quiet / one-sided" reports
    show in log.log whether a stereo microphone was reduced to one channel.
    """
    side = downmix.selected_side
    if side != previous:
        if side is None:
            logging.info("[call_audio] microphone downmix: mixing both channels")
        else:
            logging.info("[call_audio] microphone downmix: using only the %s channel",
                         "left" if side == 0 else "right")
    return side


def _power(count, total, squares):
    """Mean-removed power per channel (a steady DC offset is not speech)."""
    return np.maximum(0, squares / count - (total / count) ** 2)


class MicrophoneDownmix:
    """Average stereo, selecting a persistently empty side's live neighbour.

    Decisions use non-overlapping 200 ms variance windows, independent of
    callback boundaries. A 20 ms gain ramp avoids a step when the decision
    changes. Windows without real signal retain the last decision; this is
    device adaptation, not per-frame automatic gain control. New activity on
    the excluded side (above its own measured noise) restores the mix through
    the same ramp within one 5 ms block; short speech must not wait for the
    next 200 ms classification window. Opposite-phase stereo is averaged too
    and can cancel, as a conventional mono mix does.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._rate = None
        self._weights = np.array([0.5, 0.5], dtype=np.float64)
        self._target = self._weights.copy()
        self._ramp_left = 0
        self._count = 0
        self._sum = np.zeros(2, dtype=np.float64)
        self._squares = np.zeros(2, dtype=np.float64)
        self._chunk_count = 0
        self._chunk_sum = np.zeros(2, dtype=np.float64)
        self._chunk_squares = np.zeros(2, dtype=np.float64)
        self._excluded_noise = 0.0

    @property
    def selected_side(self):
        """0 or 1 while one side is used alone, None while both are mixed."""
        excluded = np.flatnonzero(self._target == 0)
        return 1 - int(excluded[0]) if excluded.size else None

    def process(self, samples, rate):
        data = np.asarray(samples, dtype=np.float32)
        if data.ndim not in (1, 2) or (data.ndim == 2 and data.shape[1] not in (1, 2)):
            raise ValueError("Call capture must be mono or stereo")
        if not np.isfinite(data).all():
            # A driver glitch must not drop whole 20 ms blocks (and log a
            # traceback for each) nor reach the echo canceller: silence it.
            data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
        if data.ndim == 1 or data.shape[1] == 1:
            return data.reshape(-1)
        rate = int(rate)
        if rate <= 0:
            raise ValueError("Invalid capture rate")
        if rate != self._rate:
            self.reset()
            self._rate = rate
        window = max(1, rate // 5)
        step = max(1, rate // 200)
        output = np.empty(len(data), dtype=np.float32)
        start = 0
        while start < len(data):
            end = min(len(data), start + window - self._count, start + step - self._chunk_count)
            if self._ramp_left:
                end = min(end, start + self._ramp_left)
            block = data[start:end].astype(np.float64)
            count = len(block)
            if self._ramp_left:
                fraction = np.arange(1, count + 1)[:, None] / self._ramp_left
                weights = self._weights + fraction * (self._target - self._weights)
                output[start:end] = (block * weights).sum(axis=1)
                self._weights = weights[-1]
                self._ramp_left -= count
            else:
                output[start:end] = block @ self._weights
            total = block.sum(axis=0)
            squares = (block * block).sum(axis=0)
            self._sum += total
            self._squares += squares
            self._count += count
            self._chunk_sum += total
            self._chunk_squares += squares
            self._chunk_count += count
            if self._chunk_count == step or self._count == window:
                self._check_excluded_side(rate)
            if self._count == window:
                self._classify_window(rate)
            start = end
        return output

    def _set_target(self, target, rate):
        if not np.array_equal(target, self._target):
            self._target = target
            self._ramp_left = max(1, rate // 50)

    def _check_excluded_side(self, rate):
        count = self._chunk_count
        power = _power(count, self._chunk_sum, self._chunk_squares)
        self._chunk_count = 0
        self._chunk_sum[:] = 0
        self._chunk_squares[:] = 0
        excluded = np.flatnonzero(self._target == 0)
        if not excluded.size:
            return
        side = int(excluded[0])
        threshold = max(_ACTIVITY_FLOOR, self._excluded_noise * _ACTIVITY_NOISE_MARGIN)
        if power[side] > threshold and power[side] * _RESTORE_RATIO > power[1 - side]:
            self._set_target(np.array([0.5, 0.5]), rate)

    def _classify_window(self, rate):
        power = _power(self._count, self._sum, self._squares)
        self._count = 0
        self._sum[:] = 0
        self._squares[:] = 0
        if max(power) <= _DECISION_POWER:
            return
        live = 0 if power[0] > power[1] else 1
        ratio = _RESTORE_RATIO if self.selected_side == live else _CROSSTALK_RATIO
        if power[1 - live] * ratio > power[live]:
            self._set_target(np.array([0.5, 0.5]), rate)
            return
        if (self._target[live] == 0
                and power[live] <= self._excluded_noise * _ACTIVITY_NOISE_MARGIN):
            # The excluded side carries only the noise it was excluded with
            # (mains hum, say) while a gated microphone pauses: that pause is
            # not a reason to hand the call to the noise.
            return
        self._excluded_noise = float(power[1 - live])
        self._set_target(np.array([1.0, 0.0]) if live == 0 else np.array([0.0, 1.0]), rate)
