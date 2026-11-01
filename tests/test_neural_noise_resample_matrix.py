"""Resample and length contract matrix for neural noise reduction.

Additive suite for the rates and lengths tests/test_neural_noise_reduction.py
does not cover: 8000, 22050 and 32000 Hz, short, odd and prime lengths, and
the _convert length contract behind denoise_pcm16. The model is replaced by a
stand-in that returns each spectrum four hops late, like the base suite's, so
these tests say nothing about how the real model sounds.

Every assertion here states the length contract, not the current behaviour:
a recording of N frames must come back with exactly N frames, at every rate
the resampler accepts (8000-384000 Hz, see core/call_audio_resampling.py).
A violation is a plumbing bug in framing/resampling/cropping, not a model
limitation, and must be reported with its inputs rather than tolerated. No
model file, no network, no audio hardware, no wx.App.
"""

import numpy as np
import pytest

from core import neural_noise_reduction as nnr

BINS = nnr.WINDOW // 2 + 1
LATE_FRAMES = 4

# Every rate the existing suite covers plus the three it does not.
ALL_RATES = [8_000, 16_000, 22_050, 32_000, 44_100, 48_000]
# The rates with no length coverage in the base suite.
NEW_RATES = [8_000, 22_050, 32_000]
# Selected, not exhaustive: below the resampler delay (32 samples at 8 kHz),
# around HOP and WINDOW, non-multiples of HOP, a mid size, and one prime to
# force non-integer resample ratios in every direction.
SELECTED_LENGTHS = [1, 2, 100, 479, 480, 481, 959, 960, 961, 1000, 1001, 5000, 9973]


class _LateSession:
    """Returns every spectrum LATE_FRAMES frames after it came in."""

    def get_inputs(self):
        from types import SimpleNamespace
        return [SimpleNamespace(name="spec"), SimpleNamespace(name="state_in")]

    def get_outputs(self):
        from types import SimpleNamespace
        return [SimpleNamespace(name="spec_e"), SimpleNamespace(name="state_out")]

    def run(self, _outputs, feeds):
        state = feeds["state_in"].reshape(LATE_FRAMES, BINS, 2)
        spec = feeds["spec"].reshape(1, BINS, 2)
        late = state[:1].reshape(1, 1, BINS, 2).copy()
        return [late, np.concatenate((state[1:], spec)).reshape(-1)]


def _denoiser():
    state = np.zeros(LATE_FRAMES * BINS * 2, dtype=np.float32)
    return nnr.NeuralDenoiser(session=_LateSession(), initial_state=state)


def _tone(frames, rate, channels=1, level=0.3):
    t = np.arange(frames) / rate
    wave = level * (np.sin(2 * np.pi * 440 * t) + 0.5 * np.sin(2 * np.pi * 1250 * t)) / 1.5
    return np.stack([wave] * channels, axis=1).astype(np.float32)


def _pcm(samples):
    return (samples * 32767).round().astype("<i2").tobytes()


@pytest.mark.parametrize("rate", ALL_RATES)
@pytest.mark.parametrize("channels", [1, 2])
def test_pcm16_length_matrix_two_seconds_odd(rate, channels):
    """Two seconds plus 7 frames (odd) keep their exact byte length at every
    supported rate and both channel layouts."""
    audio = _pcm(_tone(rate * 2 + 7, rate, channels))
    result = nnr.denoise_pcm16(audio, rate, channels, _denoiser())
    assert len(result) == len(audio)


@pytest.mark.parametrize("rate", NEW_RATES)
@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("frames", SELECTED_LENGTHS[:-1])
def test_pcm16_short_length_matrix(rate, channels, frames):
    """Short recordings at the previously uncovered rates keep their exact
    byte length, including lengths below the resampler delay and below WINDOW."""
    audio = _pcm(_tone(frames, rate, channels))
    result = nnr.denoise_pcm16(audio, rate, channels, _denoiser())
    assert len(result) == len(audio)


@pytest.mark.parametrize("rate", NEW_RATES)
@pytest.mark.parametrize("channels", [1, 2])
def test_pcm16_long_odd_length(rate, channels):
    """A longer odd-length recording (5 s + 33 frames) keeps its exact byte
    length at the previously uncovered rates."""
    audio = _pcm(_tone(rate * 5 + 33, rate, channels))
    result = nnr.denoise_pcm16(audio, rate, channels, _denoiser())
    assert len(result) == len(audio)


@pytest.mark.parametrize("rate", ALL_RATES)
@pytest.mark.parametrize("channels", [1, 2])
def test_process_shape_matrix(rate, channels):
    """process() returns exactly the input (frames, channels) shape at every
    supported rate."""
    pcm = _tone(rate + 7, rate, channels)
    out = _denoiser().process(pcm, rate)
    assert out.shape == pcm.shape


@pytest.mark.parametrize("rate", ALL_RATES)
@pytest.mark.parametrize("frames", SELECTED_LENGTHS)
def test_convert_floor_length_contract(rate, frames):
    """_convert without an explicit length returns floor(N * target / source)
    samples, matching the resampler's documented output count."""
    samples = _tone(frames, rate)[:, 0]
    up = nnr._convert(samples, rate, nnr.MODEL_RATE)
    assert up.size == frames * nnr.MODEL_RATE // rate
    down = nnr._convert(samples, nnr.MODEL_RATE, rate)
    assert down.size == frames * rate // nnr.MODEL_RATE


@pytest.mark.parametrize("rate", ALL_RATES)
@pytest.mark.parametrize("frames", SELECTED_LENGTHS)
def test_convert_round_trip_restores_original_length(rate, frames):
    """The call-site contract used by process(): converting up to the model
    rate and back with the original channel length requested returns exactly
    that many samples. No arbitrary length is promised, only this one."""
    samples = _tone(frames, rate)[:, 0]
    at_model_rate = nnr._convert(samples, rate, nnr.MODEL_RATE)
    back = nnr._convert(at_model_rate, nnr.MODEL_RATE, rate, frames)
    assert back.size == frames


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("frames", [1, 2, 3])
def test_no_silent_truncation_at_minimal_lengths(channels, frames):
    """Smallest valid recordings at 8000 Hz keep every frame. If the resampler
    ever produced fewer samples than _convert asks for, the public output
    would come out short here: the truncation must be visible, never silent."""
    audio = _pcm(_tone(frames, 8_000, channels))
    result = nnr.denoise_pcm16(audio, 8_000, channels, _denoiser())
    assert len(result) == len(audio)
    pcm = _tone(frames, 8_000, channels)
    out = _denoiser().process(pcm, 8_000)
    assert out.shape == pcm.shape
