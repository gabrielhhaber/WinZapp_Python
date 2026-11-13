"""Mono call PCM across shared WASAPI mono/stereo device streams.

Channel capacity is only an opening candidate, not the Windows mix format.
Downmix runs on the sender, never PortAudio's borrowed-buffer callback.
"""
import numpy as np


def shared_channel_counts(info, *, input_device):
    key = "max_input_channels" if input_device else "max_output_channels"
    try:
        return (2, 1) if int(info.get(key, 0)) >= 2 else (1,)
    except (TypeError, ValueError, OverflowError, AttributeError):
        return (1,)


class MicrophoneDownmix:
    """Average stereo, selecting a persistently empty side's live neighbour.

    Decisions use non-overlapping 200 ms variance windows (DC is not speech),
    independent of callback boundaries. A 20 ms gain ramp avoids a step when
    the decision changes. Silent windows retain the last decision; this is
    device adaptation, not per-frame automatic gain control. New activity on
    the excluded side immediately restores the mix through the same ramp;
    short speech must not wait for the next 200 ms classification window.
    Opposite-phase stereo is averaged too and can cancel, as a conventional
    mono mix does.
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
        self._previous = np.zeros(2, dtype=np.float64)

    def process(self, samples, rate):
        data = np.asarray(samples, dtype=np.float32)
        if data.ndim not in (1, 2) or (data.ndim == 2 and data.shape[1] not in (1, 2)):
            raise ValueError("Call capture must be mono or stereo")
        if not np.isfinite(data).all():
            raise ValueError("Call capture must contain finite samples")
        if data.ndim == 1 or data.shape[1] == 1:
            return data.reshape(-1)
        rate = int(rate)
        if rate <= 0:
            raise ValueError("Invalid capture rate")
        if rate != self._rate:
            self.reset()
            self._rate = rate
        window = max(1, rate // 5)
        output = np.empty(len(data), dtype=np.float32)
        start = 0
        while start < len(data):
            end = min(len(data), start + window - self._count)
            if self._ramp_left:
                end = min(end, start + self._ramp_left)
            block = data[start:end].astype(np.float64)
            excluded = np.flatnonzero(self._target == 0)
            if excluded.size:
                side = int(excluded[0])
                # Differences ignore a steady DC offset. Detect the first
                # new sample on the discarded side, even across callbacks;
                # never mute a short word until the variance window ends.
                changes = np.diff(block, axis=0, prepend=self._previous[None, :]) ** 2
                activity = np.flatnonzero(
                    (changes[:, side] > 1e-10)
                    & (changes[:, side] * 1000 > changes[:, 1 - side])
                )
                if activity.size:
                    first = int(activity[0])
                    if first:
                        end = start + first
                        block = block[:first]
                    else:
                        self._target = np.array([0.5, 0.5])
                        self._ramp_left = max(1, rate // 50)
                        end = min(end, start + self._ramp_left)
                        block = block[:end - start]
            count = len(block)
            if self._ramp_left:
                fraction = np.arange(1, count + 1)[:, None] / self._ramp_left
                weights = self._weights + fraction * (self._target - self._weights)
                output[start:end] = (block * weights).sum(axis=1)
                self._weights = weights[-1]
                self._ramp_left -= count
            else:
                output[start:end] = block @ self._weights
            self._sum += block.sum(axis=0)
            self._previous = block[-1]
            self._squares += (block * block).sum(axis=0)
            self._count += count
            if self._count == window:
                power = np.maximum(0, self._squares / window - (self._sum / window) ** 2)
                if max(power) > 1e-10:
                    target = np.array([0.5, 0.5])
                    if min(power) * 1000 <= max(power):
                        target = np.array([1.0, 0.0]) if power[0] > power[1] else np.array([0.0, 1.0])
                    if not np.array_equal(target, self._target):
                        self._target = target
                        self._ramp_left = max(1, rate // 50)
                self._count = 0
                self._sum[:] = 0
                self._squares[:] = 0
            start = end
        return output
