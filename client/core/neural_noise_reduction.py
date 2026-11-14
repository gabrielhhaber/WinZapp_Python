"""Neural noise reduction for recorded voice messages (DPDFNet, 48 kHz).

A voice message is recorded whole, so the model runs offline on the finished
PCM: no real-time budget and no echo canceller to share the stream with. The
model only works at 48 kHz and on one channel, so each channel is converted to
48 kHz, enhanced on its own with its own recurrent state, and converted back to
the recording's rate. A stereo recording keeps both channels (the level
difference between them survives, see tests); averaging them to mono, as the
upstream package does, would not.

The reference streamer's recurrent state makes the network answer with a
fixed delay of four hops (40 ms) relative to the input. That figure comes from
the upstream streaming structure, measured against the real model file on
synthetic signals (mixture correlation peaks at zero lag after this crop,
exactly four leading silent output frames, and a 1920-sample raw delay in
both reference paths). That figure was measured on synthetic signals only. The
resamplers add a few milliseconds more. All of it is cut
off, so the result has exactly as many samples as the recording.

``core.audio_processing.apply_noise_gate`` stays the fallback when the model
file or onnxruntime is missing: noise reduction is a setting the user already
turned on, and it should still do something. The engine that ran is returned
and logged, so a recording cleaned by the gate is never taken for a neural one.

Model: dpdfnet2_48khz_hr from https://huggingface.co/Ceva-IP/DPDFNet
(Apache-2.0). The framing below follows the upstream StreamEnhancer
(github.com/ceva-ip/DPDFNet, package/src/dpdfnet/stream.py).
"""

from __future__ import annotations

import logging
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from core.call_audio_resampling import StreamingMonoResampler

MODEL_RATE = 48_000
MODEL_FILENAME = "dpdfnet2_48khz_hr.onnx"
WINDOW = 960
HOP = WINDOW // 2
# Output lags input by this many hops; measured on synthetic signals, not documented upstream.
MODEL_DELAY = 4 * HOP
_WINDOW_SHAPE = np.sin(0.5 * np.pi * np.sin(0.5 * np.pi * (np.arange(WINDOW) + 0.5) / HOP) ** 2).astype(np.float32)


def model_path() -> str:
    from core import noise_model
    return noise_model.installed_path()


def _load_session(path: str):
    import onnxruntime as ort

    from core.cuda_components import runtime as noise_runtime

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    # The graphics card when the user chose one and its build is in place
    # (CUDA on Windows, Core ML on macOS), else the processor; the session
    # falls back to the next provider by itself if the first will not start.
    providers = noise_runtime.providers(ort.get_available_providers())
    session = ort.InferenceSession(path, sess_options=options, providers=providers)
    logging.info("[noise_reduction] model on %s", session.get_providers()[0])
    meta = session.get_modelmeta().custom_metadata_map
    erb = np.array(meta["erb_norm_init"].split(","), dtype=np.float32)
    spec = np.array(meta["spec_norm_init"].split(","), dtype=np.float32)
    state = np.zeros(int(meta["state_size"]), dtype=np.float32)
    state[:erb.size] = erb
    state[int(meta["erb_norm_state_size"]):int(meta["erb_norm_state_size"]) + spec.size] = spec
    return session, state


class NeuralDenoiser:
    """One loaded model; ``process`` is safe to call for any number of recordings."""

    def __init__(self, path: str | None = None, session=None, initial_state=None):
        if session is None:
            session, initial_state = _load_session(path or model_path())
        self._session = session
        self._initial_state = initial_state
        try:
            self.provider = session.get_providers()[0]
        except Exception:
            self.provider = ""
        inputs, outputs = session.get_inputs(), session.get_outputs()
        self._names = (inputs[0].name, inputs[1].name, outputs[0].name, outputs[1].name)

    def _enhance_48k(self, samples: np.ndarray) -> np.ndarray:
        """One channel at 48 kHz in, the same number of samples out."""
        count = samples.size
        padded = np.concatenate((
            np.zeros(WINDOW, np.float32), samples, np.zeros(MODEL_DELAY + 2 * WINDOW, np.float32)))
        spec_in, state_in, spec_out, state_out = self._names
        state = self._initial_state.copy()
        out = np.zeros_like(padded)
        for start in range(0, padded.size - WINDOW + 1, HOP):
            spectrum = np.fft.rfft(padded[start:start + WINDOW] * _WINDOW_SHAPE)
            frame = np.stack((spectrum.real, spectrum.imag), -1).astype(np.float32)[None, None]
            enhanced, state = self._session.run([spec_out, state_out], {spec_in: frame, state_in: state})
            bins = enhanced[0, 0, :, 0] + 1j * enhanced[0, 0, :, 1]
            out[start:start + WINDOW] += (np.fft.irfft(bins, WINDOW) * _WINDOW_SHAPE).astype(np.float32)
        first = WINDOW + MODEL_DELAY
        return out[first:first + count]

    def process(self, pcm: np.ndarray, rate: int) -> np.ndarray:
        """float32 samples shaped (frames, channels) at ``rate`` -> same shape."""
        pcm = np.asarray(pcm, dtype=np.float32)

        def one_channel(index: int) -> np.ndarray:
            at_model_rate = _convert(pcm[:, index], rate, MODEL_RATE)
            return _convert(self._enhance_48k(at_model_rate), MODEL_RATE, rate, pcm.shape[0])

        count = pcm.shape[1]
        if count == 1:
            return one_channel(0)[:, None]
        # Channels are independent (own recurrent state) and onnxruntime
        # releases the GIL, so a stereo note takes about as long as a mono one.
        with ThreadPoolExecutor(max_workers=count) as pool:
            return np.stack(list(pool.map(one_channel, range(count))), axis=1)


def _convert(samples: np.ndarray, source: int, target: int, length: int | None = None) -> np.ndarray:
    """Whole-buffer rate conversion without the resampler's fixed delay."""
    wanted = samples.size * target // source if length is None else length
    if source == target:
        return samples
    delay = math.ceil(32 * max(1, source / target))
    tail = np.zeros(delay + 1, dtype=np.float32)
    converted = StreamingMonoResampler().process(np.concatenate((samples, tail)), source, target)
    skip = round(delay * target / source)
    return converted[skip:skip + wanted]


def denoise_pcm16(audio: bytes, rate: int, channels: int, denoiser: NeuralDenoiser) -> bytes:
    """Interleaved little-endian PCM16 in, the same layout and length out."""
    frame_bytes = 2 * channels
    frames = len(audio) // frame_bytes
    if not frames:
        return audio
    samples = np.frombuffer(audio, dtype="<i2", count=frames * channels)
    pcm = samples.reshape(frames, channels).astype(np.float32) / 32768.0
    cleaned = denoiser.process(pcm, rate)
    if not np.isfinite(cleaned).all():
        # Check before clipping: clip maps inf to full scale, which would hide
        # a blown-up model output as a maximum-amplitude spike.
        raise ValueError("the model produced non-finite samples")
    cleaned = np.clip(cleaned, -1.0, 1.0)
    return (cleaned * 32767.0).round().astype("<i2").tobytes() + audio[frames * frame_bytes:]


_denoiser: NeuralDenoiser | None = None
_denoiser_lock = threading.Lock()


def reduce_voice_noise(audio: bytes, rate: int, channels: int,
                       neural: bool = True) -> tuple[bytes, str]:
    """The recording with its noise reduced, and which engine did it.

    The engine is "neural", or "gate" when the model could not be used and the
    old noise gate ran instead; the caller decides what to tell the user.
    `neural` False is the user's choice of the common reducer (Settings >
    Audio, general.noise_reduction_neural): faster, the gate at once.
    """
    global _denoiser
    if not neural:
        from core.audio_processing import apply_noise_gate
        return apply_noise_gate(audio, rate, channels, strict=True), "gate"
    if os.path.isfile(model_path()):
        cleaned = _on_the_graphics_card(audio, rate, channels)
        if cleaned is not None:
            return cleaned, "neural"
    try:
        with _denoiser_lock:
            if _denoiser is None:
                if not os.path.isfile(model_path()):
                    raise FileNotFoundError(model_path())
                _denoiser = NeuralDenoiser()
            denoiser = _denoiser
        return denoise_pcm16(audio, rate, channels, denoiser), "neural"
    except Exception:
        logging.warning("[noise_reduction] neural model unavailable, using the noise gate", exc_info=True)
        from core.audio_processing import apply_noise_gate
        # strict: a gate failure must reach the caller, not look like success.
        return apply_noise_gate(audio, rate, channels, strict=True), "gate"


def _on_the_graphics_card(audio: bytes, rate: int, channels: int) -> bytes | None:
    """The recording cleaned by the noise reducer's own process, when a
    graphics card build is chosen (cuda_components/noise_worker_host.py);
    None when it is not, or could not: the processor here does it then."""
    from core.cuda_components import noise_worker_host, runtime
    if not runtime.uses_worker(noise_worker_host.host.device):
        return None
    try:
        return noise_worker_host.host.denoise(audio, rate, channels)
    except Exception as exc:
        logging.warning("[noise_reduction] the graphics card could not clean it (%s); "
                        "using the processor", exc)
        return None


def forget_denoiser() -> None:
    """Drop the loaded model (its file was removed): the next recording
    loads it again, or falls back to the gate when it is gone."""
    global _denoiser
    with _denoiser_lock:
        _denoiser = None
