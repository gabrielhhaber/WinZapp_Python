"""The echo canceller removes a delayed, filtered copy of the far-end signal."""

import numpy as np

from core.echo_canceller import AEC_BLOCK, EchoCanceller


def _echo_path(far, delay=2400, gain=0.5):
    out = np.zeros_like(far)
    out[delay:] = far[:-delay] * gain
    return out


def _rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def _run(aec, far, mic, chunk=AEC_BLOCK):
    result = []
    for i in range(0, len(far), chunk):
        aec.push_reference(far[i:i + chunk])
        result.append(aec.process(mic[i:i + chunk]))
    return np.concatenate(result)


def test_echo_is_reduced_after_convergence():
    rng = np.random.default_rng(1)
    far = (rng.standard_normal(48_000 * 6) * 0.1).astype(np.float32)
    mic = _echo_path(far)
    out = _run(EchoCanceller(), far, mic)
    tail = slice(-48_000, None)
    assert _rms(out[tail]) < _rms(mic[-48_000:]) * 0.1


def test_near_end_speech_survives():
    rng = np.random.default_rng(2)
    far = (rng.standard_normal(48_000 * 6) * 0.1).astype(np.float32)
    t = np.arange(len(far)) / 48_000
    near = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    near[: 48_000 * 3] = 0  # far-end alone first, so the filter converges
    out = _run(EchoCanceller(), far, _echo_path(far) + near)
    tail = slice(-48_000, None)
    assert _rms(out[tail]) > _rms(near[tail]) * 0.7


def test_no_reference_passes_microphone_through():
    mic = (np.random.default_rng(3).standard_normal(AEC_BLOCK * 3) * 0.1).astype(np.float32)
    out = EchoCanceller().process(mic)
    assert np.allclose(out, mic)


def test_stream_length_is_conserved_across_odd_chunks():
    aec = EchoCanceller()
    mic = np.zeros(AEC_BLOCK * 4 + 100, dtype=np.float32)
    total = sum(len(aec.process(mic[i:i + 333])) for i in range(0, len(mic), 333))
    assert total == AEC_BLOCK * 4
