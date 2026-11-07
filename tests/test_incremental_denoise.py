"""Incremental (during-recording) noise reduction equals the offline path.

The model is the same stand-in as tests/test_neural_noise_reduction.py — a
session that answers four hops late — so the default suite covers framing,
delay removal, resampling, chunk boundaries and channel handling with no model
file, audio hardware, network or wx.App.

Set DPDFNET_MODEL to the pinned dpdfnet2_48khz_hr.onnx (hash-checked) to also
run bit-exact PCM16 equivalence against the real model.
"""

import hashlib
import os
from types import SimpleNamespace

import numpy as np
import pytest

from core import neural_noise_reduction as nnr
from core.incremental_denoise import IncrementalDenoiser

BINS = nnr.WINDOW // 2 + 1
LATE_FRAMES = 4

MODEL_SHA256 = "7f0575a5cec0ba4ffd8f8bd657e06d007e4ccdd955d76faab922b9d3291dc14b"
MODEL_PATH = os.environ.get("DPDFNET_MODEL")


class _LateSession:
    """Returns every spectrum LATE_FRAMES frames after it came in."""

    def get_inputs(self):
        return [SimpleNamespace(name="spec"), SimpleNamespace(name="state_in")]

    def get_outputs(self):
        return [SimpleNamespace(name="spec_e"), SimpleNamespace(name="state_out")]

    def run(self, _outputs, feeds):
        state = feeds["state_in"].reshape(LATE_FRAMES, BINS, 2)
        spec = feeds["spec"].reshape(1, BINS, 2)
        late = state[:1].reshape(1, 1, BINS, 2).copy()
        return [late, np.concatenate((state[1:], spec)).reshape(-1)]


def _stand_in():
    state = np.zeros(LATE_FRAMES * BINS * 2, dtype=np.float32)
    return nnr.NeuralDenoiser(session=_LateSession(), initial_state=state)


def _signal(frames, rate, channels, seed):
    rng = np.random.default_rng(seed)
    t = np.arange(frames) / rate
    x = .15 * np.sin(2 * np.pi * (130 * t + 8 * t * t)) + .07 * np.sin(2 * np.pi * 730 * t)
    x = x + rng.normal(0, .025, frames)
    x = x * (.3 + .7 * (np.sin(2 * np.pi * 1.7 * t) > 0))  # silence gaps
    if channels == 1:
        a = x[:, None]
    else:
        a = np.stack([x, .7 * x + .025 * np.sin(2 * np.pi * 233 * t)], -1)
    return (np.clip(a, -1, 1) * 32767).round().astype("<i2").tobytes()


def _chunks(data, style, rate, channels):
    fb = 2 * channels
    if style == "oneshot":
        return [data]
    if style == "pyaudio4096":
        n = 4096 * fb
        return [data[i:i + n] for i in range(0, len(data), n)]
    if style == "100ms":
        n = int(rate * 0.1) * fb
        return [data[i:i + n] for i in range(0, len(data), n)]
    if style == "irregular":
        rng = np.random.default_rng(99)
        out, i = [], 0
        while i < len(data):
            n = int(rng.integers(500, 5000)) * fb
            out.append(data[i:i + n])
            i += n
        return out
    if style == "tiny-head":
        # First fragment smaller than the resampler's pending skip: the skip
        # must be consumed across fragments, not dropped with the first one.
        head, rest = data[:fb], data[fb:]
        n = 4096 * fb
        return [head] + [rest[i:i + n] for i in range(0, len(rest), n)]
    if style == "creeping":
        # Fragments of 1, 2, 3, ... frames, cycling: many sub-skip fragments.
        out, i, step = [], 0, 1
        while i < len(data):
            n = step * fb
            out.append(data[i:i + n])
            i += n
            step = step % 64 + 1
        return out
    raise ValueError(style)


def _incremental(data, rate, channels, style, denoiser, spool_path=None):
    inc = IncrementalDenoiser(rate, channels, denoiser, spool_path=spool_path)
    for block in _chunks(data, style, rate, channels):
        inc.feed(block)
    out, engine = inc.finish()
    assert engine == "neural"
    if spool_path is not None:
        with open(spool_path, "rb") as f:
            return f.read()
    return out


CASES = [
    (48000, 48000, 1, "pyaudio4096"),
    (48000, 5 * 48000, 1, "100ms"),
    (48000, 15 * 48000, 2, "irregular"),
    (48000, 14400, 1, "oneshot"),        # 0.3 s, shorter than the model delay
    (48000, 2 * 48000, 2, "oneshot"),
    (48000, 9973, 1, "pyaudio4096"),     # odd length
    (48000, 281041, 1, "100ms"),         # odd length
    (44100, 3 * 44100, 1, "pyaudio4096"),
    (44100, 5 * 44100, 2, "irregular"),
    (16000, 3 * 16000, 1, "100ms"),
    (8000, 2 * 8000, 1, "irregular"),
    (96000, 2 * 96000, 1, "pyaudio4096"),
    # Minimal fragments: the first resampled fragment is smaller than the
    # pending FIR-delay skip (44100 -> skip 35, 16000 -> 96, 8000 -> 192,
    # 96000 -> 32). Found by peer review: a 1-sample head dropped the skip.
    (44100, 3 * 44100, 1, "tiny-head"),
    (44100, 2 * 44100, 2, "creeping"),
    (16000, 3 * 16000, 1, "tiny-head"),
    (8000, 2 * 8000, 1, "creeping"),
    (96000, 2 * 96000, 1, "tiny-head"),
    # Whole recording shorter than the input skip: the tail flush in
    # finish() is what consumes it.
    (44100, 20, 1, "oneshot"),
    (16000, 10, 1, "oneshot"),
    (8000, 5, 1, "oneshot"),
]


@pytest.mark.parametrize("rate,frames,channels,style", CASES)
def test_incremental_matches_offline_bit_for_bit(rate, frames, channels, style):
    data = _signal(frames, rate, channels, seed=frames ^ (rate << 2) ^ channels)
    expected = nnr.denoise_pcm16(data, rate, channels, _stand_in())
    assert _incremental(data, rate, channels, style, _stand_in()) == expected


def test_spool_mode_returns_identical_bytes(tmp_path):
    rate, frames, channels = 48000, 5 * 48000, 1
    data = _signal(frames, rate, channels, seed=7)
    expected = nnr.denoise_pcm16(data, rate, channels, _stand_in())
    spool = str(tmp_path / "cleaned.pcm")
    assert _incremental(data, rate, channels, "pyaudio4096", _stand_in(), spool) == expected


def test_non_finite_model_output_raises(tmp_path):
    class _BlownSession(_LateSession):
        def run(self, _outputs, feeds):
            late, state = super().run(_outputs, feeds)
            late[:] = np.nan
            return [late, state]

    state = np.zeros(LATE_FRAMES * BINS * 2, dtype=np.float32)
    denoiser = nnr.NeuralDenoiser(session=_BlownSession(), initial_state=state)
    data = _signal(48000, 48000, 1, seed=3)
    inc = IncrementalDenoiser(48000, 1, denoiser, spool_path=str(tmp_path / "x.pcm"))
    with pytest.raises(ValueError):
        inc.feed(data)


@pytest.mark.skipif(
    not MODEL_PATH or not os.path.isfile(MODEL_PATH),
    reason="pinned DPDFNet model not provided (set DPDFNET_MODEL)",
)
def test_real_model_matches_offline_bit_for_bit():
    digest = hashlib.sha256()
    with open(MODEL_PATH, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    assert digest.hexdigest() == MODEL_SHA256, "model file is not the pinned DPDFNet revision"
    denoiser = nnr.NeuralDenoiser(MODEL_PATH)
    for rate, frames, channels, style in [
        (48000, 5 * 48000, 1, "pyaudio4096"),
        (48000, 281041, 1, "irregular"),
        (44100, 3 * 44100, 1, "100ms"),
        (48000, 2 * 48000, 2, "irregular"),
        (16000, 3 * 16000, 1, "irregular"),
    ]:
        data = _signal(frames, rate, channels, seed=frames ^ (rate << 2) ^ channels)
        expected = nnr.denoise_pcm16(data, rate, channels, denoiser)
        assert _incremental(data, rate, channels, style, denoiser) == expected
