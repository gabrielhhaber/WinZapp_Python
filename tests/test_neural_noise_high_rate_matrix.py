"""High-rate resample and length contract for neural noise reduction.

Additive suite for capture rates above what
tests/test_neural_noise_resample_matrix.py covers: 96000 and 192000 Hz.
Studio and pro-audio microphones commonly report 96000 Hz as their native
rate, and recording_configs_for() offers the device's native rate first, so
this path is reachable on ordinary hardware. The model is replaced by a
stand-in that returns each spectrum four hops late, like the base suite's,
so these tests say nothing about how the real model sounds.

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

# Above the base matrix's 48000 Hz ceiling. 192000 is optional coverage for
# the top of the pro-audio range; both divide the model rate exactly, so
# non-integer ratios at high rates are not exercised here.
HIGH_RATES = [96_000, 192_000]
# Selected, not exhaustive: minimal lengths, around the model HOP and
# WINDOW, non-multiples of HOP, and one prime.
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


@pytest.mark.parametrize("rate", HIGH_RATES)
@pytest.mark.parametrize("channels", [1, 2])
def test_pcm16_length_matrix_two_seconds_odd(rate, channels):
    """Two seconds plus 7 frames (odd) keep their exact byte length at high
    capture rates and both channel layouts."""
    audio = _pcm(_tone(rate * 2 + 7, rate, channels))
    result = nnr.denoise_pcm16(audio, rate, channels, _denoiser())
    assert len(result) == len(audio)


@pytest.mark.parametrize("rate", HIGH_RATES)
@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("frames", SELECTED_LENGTHS[:-1])
def test_pcm16_short_length_matrix(rate, channels, frames):
    """Short high-rate recordings keep their exact byte length, including
    lengths below the resampler delay and below WINDOW."""
    audio = _pcm(_tone(frames, rate, channels))
    result = nnr.denoise_pcm16(audio, rate, channels, _denoiser())
    assert len(result) == len(audio)


@pytest.mark.parametrize("rate", HIGH_RATES)
@pytest.mark.parametrize("channels", [1, 2])
def test_pcm16_long_odd_length(rate, channels):
    """A longer odd-length high-rate recording (5 s + 33 frames) keeps its
    exact byte length."""
    audio = _pcm(_tone(rate * 5 + 33, rate, channels))
    result = nnr.denoise_pcm16(audio, rate, channels, _denoiser())
    assert len(result) == len(audio)


@pytest.mark.parametrize("rate", HIGH_RATES)
@pytest.mark.parametrize("channels", [1, 2])
def test_process_shape_matrix(rate, channels):
    """process() returns exactly the input (frames, channels) shape at high
    capture rates."""
    pcm = _tone(rate + 7, rate, channels)
    out = _denoiser().process(pcm, rate)
    assert out.shape == pcm.shape


@pytest.mark.parametrize("rate", HIGH_RATES)
@pytest.mark.parametrize("frames", SELECTED_LENGTHS)
def test_convert_floor_length_contract(rate, frames):
    """_convert without an explicit length returns floor(N * target / source)
    samples at high rates, matching the resampler's documented output count."""
    samples = _tone(frames, rate)[:, 0]
    down = nnr._convert(samples, rate, nnr.MODEL_RATE)
    assert down.size == frames * nnr.MODEL_RATE // rate
    up = nnr._convert(samples, nnr.MODEL_RATE, rate)
    assert up.size == frames * rate // nnr.MODEL_RATE


@pytest.mark.parametrize("rate", HIGH_RATES)
@pytest.mark.parametrize("frames", SELECTED_LENGTHS)
def test_convert_round_trip_restores_original_length(rate, frames):
    """The call-site contract used by process(): converting down to the model
    rate and back with the original channel length requested returns exactly
    that many samples. No arbitrary length is promised, only this one."""
    samples = _tone(frames, rate)[:, 0]
    at_model_rate = nnr._convert(samples, rate, nnr.MODEL_RATE)
    back = nnr._convert(at_model_rate, nnr.MODEL_RATE, rate, frames)
    assert back.size == frames


@pytest.mark.parametrize("rate", HIGH_RATES)
@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("frames", [1, 2, 3])
def test_no_silent_truncation_at_minimal_lengths(rate, channels, frames):
    """Smallest valid high-rate recordings keep every frame. If the
    resampler ever produced fewer samples than _convert asks for, the public
    output would come out short here: the truncation must be visible, never
    silent."""
    audio = _pcm(_tone(frames, rate, channels))
    result = nnr.denoise_pcm16(audio, rate, channels, _denoiser())
    assert len(result) == len(audio)
    pcm = _tone(frames, rate, channels)
    out = _denoiser().process(pcm, rate)
    assert out.shape == pcm.shape
