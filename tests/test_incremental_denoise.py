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
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from core import neural_noise_reduction as nnr
from core.incremental_denoise import IncrementalDenoiser, drain_capture_frames
from core.voice_recording_mode import prepare_voice_pcm

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
# ── Drain controller (capture pump): fake workers, no model, no wx ─────────

class _FakeWorker:
    """IncrementalDenoiser stand-in for controller tests: optionally blocks
    inside feed() until a gate opens (native-block simulation) or fails."""

    def __init__(self, gate=None, entered=None, fail_at=None):
        self.fed = []
        self.gate = gate
        self.entered = entered
        self.fail_at = fail_at
        self.aborted = False

    def feed(self, chunk):
        if self.fail_at is not None and len(self.fed) >= self.fail_at:
            raise RuntimeError("fake model failure")
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(5.0), "test deadlock: gate never opened"
        self.fed.append(bytes(chunk))

    def abort(self):
        self.aborted = True


def _drain_session(frames, spool, worker=None, **flags):
    session = {
        "frames": frames, "rate": 48000, "ch": 1, "worker_ch": 1,
        "spool": str(spool), "worker": worker, "thread": None,
        "stop": False, "cancel": False, "failed": False,
    }
    session.update(flags)
    return session


def test_drain_completes_on_stop_and_keeps_spool(tmp_path):
    chunks = [b"a" * 512, b"b" * 256, b"c" * 1024]
    spool = tmp_path / "s.pcm"
    spool.write_bytes(b"")
    worker = _FakeWorker()
    session = _drain_session(list(chunks), spool, worker, stop=True)
    drain_capture_frames(session, worker)
    assert not session["failed"] and not session["cancel"]
    assert worker.fed == chunks                     # every chunk, in order
    assert not worker.aborted                       # success: worker stays live
    assert session["worker"] is worker              # ownership passes to send
    assert spool.exists()                           # spool survives for send


def test_drain_abandons_when_preloaded_backlog_exceeds_cap(tmp_path):
    spool = tmp_path / "s.pcm"
    spool.write_bytes(b"x")
    worker = _FakeWorker()
    # cap = 48000*1*2*0.001 = 96 bytes; 4 KiB preloaded is way over.
    session = _drain_session([b"x" * 1024] * 4, spool, worker,
                             stop=True, max_lag_seconds=0.001)
    drain_capture_frames(session, worker)
    assert session["failed"]
    assert worker.fed == []                         # abandoned before feeding
    assert worker.aborted
    assert session["worker"] is None
    assert not spool.exists()                       # spool deleted on abandon


def test_drain_abandons_mid_feed_when_capture_outpaces_blocked_worker(tmp_path):
    spool = tmp_path / "s.pcm"
    spool.write_bytes(b"x")
    gate, entered = threading.Event(), threading.Event()
    worker = _FakeWorker(gate=gate, entered=entered)
    frames = [b"x" * 512]
    # cap = 48000*2*0.011 ~= 1056 bytes: one chunk fits, five do not.
    session = _drain_session(frames, spool, worker, max_lag_seconds=0.011)
    t = threading.Thread(target=drain_capture_frames, args=(session, worker))
    t.start()
    assert entered.wait(5.0)                        # worker blocked in feed()
    frames.extend([b"y" * 512] * 4)                 # capture outpaces it
    gate.set()                                      # native call returns
    t.join(5.0)
    assert not t.is_alive()
    assert session["failed"]
    assert worker.fed == [b"x" * 512]               # only the first went in
    assert worker.aborted and not spool.exists()


def test_drain_cancel_cleans_up_without_feeding(tmp_path):
    spool = tmp_path / "s.pcm"
    spool.write_bytes(b"x")
    worker = _FakeWorker()
    session = _drain_session([b"x" * 512] * 3, spool, worker, cancel=True)
    drain_capture_frames(session, worker)
    assert worker.fed == [] and worker.aborted
    assert session["worker"] is None and not spool.exists()


def test_drain_worker_failure_cleans_up(tmp_path):
    spool = tmp_path / "s.pcm"
    spool.write_bytes(b"x")
    worker = _FakeWorker(fail_at=1)
    session = _drain_session([b"a" * 512, b"b" * 512], spool, worker, stop=True)
    drain_capture_frames(session, worker)
    assert session["failed"]
    assert worker.fed == [b"a" * 512]
    assert worker.aborted and session["worker"] is None and not spool.exists()


def test_drain_late_callback_chunk_is_not_lost(tmp_path):
    """The exit check runs after every feed: a chunk the callback lands while
    the last known chunk is being processed is still seen and fed."""
    spool = tmp_path / "s.pcm"
    spool.write_bytes(b"")
    frames = [b"first"]

    class _CallbackWorker(_FakeWorker):
        def feed(self, chunk):
            super().feed(chunk)
            if len(self.fed) == 1:
                frames.append(b"late")             # callback racing the exit

    worker = _CallbackWorker()
    session = _drain_session(frames, spool, worker, stop=True)
    drain_capture_frames(session, worker)
    assert worker.fed == [b"first", b"late"]
    assert not session["failed"] and spool.exists()


def test_drain_consecutive_sessions_are_isolated(tmp_path):
    spool_a, spool_b = tmp_path / "a.pcm", tmp_path / "b.pcm"
    spool_a.write_bytes(b"done-a")
    spool_b.write_bytes(b"live-b")
    worker_a, worker_b = _FakeWorker(), _FakeWorker()
    session_a = _drain_session([b"a" * 128], spool_a, worker_a, stop=True)
    drain_capture_frames(session_a, worker_a)      # completes, spool kept
    session_b = _drain_session([b"b" * 128], spool_b, worker_b, cancel=True)
    drain_capture_frames(session_b, worker_b)      # cancelled, spool deleted
    assert spool_a.read_bytes() == b"done-a"        # session A untouched
    assert not spool_b.exists()
    assert not worker_a.aborted and worker_b.aborted


def test_drain_transform_preserves_offline_mono_mix_order(tmp_path):
    """Explicit-mono wiring: per-chunk (L+R)//2 BEFORE the model through the
    controller equals the offline join -> prepare_voice_pcm -> denoise."""
    rate, frames, seed = 44100, 3 * 44100, 11
    stereo = _signal(frames, rate, 2, seed)
    expected = nnr.denoise_pcm16(prepare_voice_pcm(stereo, 2, "mono")[0],
                                 rate, 1, _stand_in())
    spool = tmp_path / "s.pcm"
    spool.write_bytes(b"")
    worker = IncrementalDenoiser(rate, 1, _stand_in(), spool_path=str(spool))
    chunks = [stereo[i:i + 4096 * 4] for i in range(0, len(stereo), 4096 * 4)]
    session = _drain_session(chunks, spool, worker, rate=rate, ch=2,
                             worker_ch=1, stop=True)
    drain_capture_frames(session, worker,
                         transform=lambda c: prepare_voice_pcm(c, 2, "mono")[0])
    assert not session["failed"]
    out_path, engine = worker.finish()
    assert engine == "neural"
    with open(out_path, "rb") as f:
        assert f.read() == expected


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
