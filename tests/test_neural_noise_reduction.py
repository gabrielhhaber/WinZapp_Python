"""Voice-message noise reduction keeps the recording's format, length and channels.

The model is replaced by a stand-in that returns each spectrum four hops
late, mirroring the reference streamer, so these tests cover framing, delay
removal, resampling
and channel handling with no model file, audio hardware or wx.App. They say
nothing about how the real model sounds.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

from core import neural_noise_reduction as nnr

BINS = nnr.WINDOW // 2 + 1
LATE_FRAMES = 4


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


def _denoiser():
    state = np.zeros(LATE_FRAMES * BINS * 2, dtype=np.float32)
    return nnr.NeuralDenoiser(session=_LateSession(), initial_state=state)


def _tone(frames, rate, channels=1, level=0.3):
    t = np.arange(frames) / rate
    wave = level * (np.sin(2 * np.pi * 440 * t) + 0.5 * np.sin(2 * np.pi * 1250 * t)) / 1.5
    return np.stack([wave] * channels, axis=1).astype(np.float32)


def _pcm(samples):
    return (samples * 32767).round().astype("<i2").tobytes()


def _inner(x, margin=4800):
    return x[margin:-margin]


@pytest.mark.parametrize("rate", [48_000, 44_100, 16_000])
@pytest.mark.parametrize("channels", [1, 2])
def test_length_and_layout_are_those_of_the_recording(rate, channels):
    audio = _pcm(_tone(rate * 2 + 7, rate, channels))
    result = nnr.denoise_pcm16(audio, rate, channels, _denoiser())
    assert len(result) == len(audio)


@pytest.mark.parametrize("rate", [48_000, 44_100])
def test_model_and_resampler_delay_are_removed(rate):
    pcm = _tone(rate * 2, rate)
    out = _denoiser().process(pcm, rate)
    assert out.shape == pcm.shape
    error = _inner(out[:, 0]) - _inner(pcm[:, 0])
    assert np.sqrt(np.mean(error ** 2)) < 0.02 * np.sqrt(np.mean(_inner(pcm[:, 0]) ** 2))


def test_channels_are_processed_separately_and_keep_their_levels():
    pcm = _tone(48_000 * 2, 48_000)
    stereo = np.stack([pcm[:, 0], 0.5 * pcm[:, 0]], axis=1)
    out = _denoiser().process(stereo, 48_000)
    left, right = _inner(out[:, 0]), _inner(out[:, 1])
    ratio_db = 20 * np.log10(np.sqrt(np.mean(left ** 2)) / np.sqrt(np.mean(right ** 2)))
    assert ratio_db == pytest.approx(6.02, abs=0.2)


def test_one_silent_channel_stays_silent():
    stereo = np.stack([_tone(48_000, 48_000)[:, 0], np.zeros(48_000, np.float32)], axis=1)
    out = _denoiser().process(stereo, 48_000)
    assert np.max(np.abs(out[:, 1])) == 0.0


def test_opposite_phase_channels_stay_opposite():
    wave = _tone(48_000 * 2, 48_000)[:, 0]
    out = _denoiser().process(np.stack([wave, -wave], axis=1), 48_000)
    np.testing.assert_allclose(out[:, 0], -out[:, 1], atol=1e-6)


class _BrokenDenoiser:
    """A model session that blows up: non-finite samples must raise, not clip."""

    def __init__(self, value):
        self._value = value

    def process(self, pcm, rate):
        out = np.zeros_like(pcm)
        out[0, 0] = self._value
        return out


@pytest.mark.parametrize("bad", [np.inf, -np.inf, np.nan])
def test_non_finite_model_output_raises_instead_of_clipping_to_full_scale(bad):
    audio = _pcm(_tone(4800, 48_000))
    with pytest.raises(ValueError):
        nnr.denoise_pcm16(audio, 48_000, 1, _BrokenDenoiser(bad))


def test_empty_recording_and_trailing_odd_byte_come_back_unchanged():
    denoiser = _denoiser()
    assert nnr.denoise_pcm16(b"", 48_000, 1, denoiser) == b""
    audio = _pcm(_tone(4800, 48_000)) + b"\x01"
    assert nnr.denoise_pcm16(audio, 48_000, 1, denoiser).endswith(b"\x01")


@pytest.mark.parametrize("frames", [1, 2, 100, 479, 480, 959, 960, 1000, 5000])
def test_recordings_shorter_than_the_model_window_keep_their_length(frames):
    audio = _pcm(_tone(frames, 48_000))
    assert len(nnr.denoise_pcm16(audio, 48_000, 1, _denoiser())) == len(audio)


def test_speech_that_stops_abruptly_keeps_its_last_samples():
    pcm = _tone(48_000, 48_000)
    out = _denoiser().process(pcm, 48_000)
    assert np.max(np.abs(out[-4800:])) == pytest.approx(np.max(np.abs(pcm[-4800:])), rel=0.05)


def test_output_is_clipped_to_pcm16():
    class Loud(_LateSession):
        def run(self, outputs, feeds):
            late, state = super().run(outputs, feeds)
            return [late * 8.0, state]

    state = np.zeros(LATE_FRAMES * BINS * 2, dtype=np.float32)
    denoiser = nnr.NeuralDenoiser(session=Loud(), initial_state=state)
    result = np.frombuffer(nnr.denoise_pcm16(_pcm(_tone(48_000, 48_000, level=0.9)), 48_000, 1, denoiser), "<i2")
    assert result.max() <= 32767 and result.min() >= -32768


def test_missing_model_falls_back_to_the_noise_gate(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(nnr, "_denoiser", None)
    monkeypatch.setattr(nnr, "model_path", lambda: str(tmp_path / "missing.onnx"))
    import core.audio_processing as gate
    monkeypatch.setattr(gate, "apply_noise_gate",
                        lambda *args, **kw: seen.append((args, kw)) or b"gated")
    assert nnr.reduce_voice_noise(b"\0\0" * 480, 48_000, 1) == (b"gated", "gate")
    # strict=True: a gate failure must not come back labelled "gate".
    assert seen == [((b"\0\0" * 480, 48_000, 1), {"strict": True})]


@pytest.mark.skipif(not os.environ.get("WINZAPP_DPDFNET_MODEL"), reason="needs the model file")
def test_real_model_keeps_length_for_both_layouts():
    denoiser = nnr.NeuralDenoiser(os.environ["WINZAPP_DPDFNET_MODEL"])
    for rate, channels in ((48_000, 1), (44_100, 2)):
        audio = _pcm(_tone(rate, rate, channels))
        assert len(nnr.denoise_pcm16(audio, rate, channels, denoiser)) == len(audio)


@pytest.mark.skipif(not os.environ.get("WINZAPP_DPDFNET_MODEL"), reason="needs the model file")
def test_real_model_output_is_neural_when_the_file_exists(monkeypatch):
    monkeypatch.setattr(nnr, "_denoiser", None)
    monkeypatch.setattr(nnr, "model_path", lambda: os.environ["WINZAPP_DPDFNET_MODEL"])
    audio = _pcm(_tone(48_000, 48_000))
    cleaned, engine = nnr.reduce_voice_noise(audio, 48_000, 1)
    assert engine == "neural" and len(cleaned) == len(audio)


def test_model_catalogue_is_pinned_and_flat_locally(tmp_path):
    from core import noise_model
    from core.transcription import model_store

    m = noise_model.DPDFNET2_48K
    assert len(m.revision) == 40 and len(m.sha256) == 64
    assert m.download_bytes == 10_493_337
    assert m.sha256_of(noise_model.MODEL_FILE) == m.sha256
    assert m.sha256_of("other") is None
    url = model_store.file_url(m, noise_model.MODEL_FILE)
    assert url.endswith(f"/resolve/{m.revision}/onnx/dpdfnet2_48khz_hr.onnx")
    assert noise_model.installed_path(str(tmp_path)).endswith("dpdfnet2_48khz_hr.onnx")
    assert not noise_model.is_installed(str(tmp_path))


def test_every_locale_has_the_fallback_notice():
    import polib, glob
    files = glob.glob(os.path.join(os.path.dirname(__file__), "..", "translations", "*", "LC_MESSAGES", "winzapp.po"))
    assert len(files) == 8  # upstream added Arabic after the first adaptation
    for f in files:
        for key in ("noise_reduction_fallback", "noise_reduction_failed"):
            entry = [e for e in polib.pofile(f) if e.msgctxt == key]
            assert len(entry) == 1 and entry[0].msgstr, (f, key)


class _PoI18n:
    """i18n.t() answered from one locale's PO file, as the application does."""

    def __init__(self, locale):
        import polib
        path = os.path.join(os.path.dirname(__file__), "..", "translations", locale,
                            "LC_MESSAGES", "winzapp.po")
        self._t = {e.msgctxt: e.msgstr for e in polib.pofile(path) if e.msgctxt}

    def t(self, key):
        return self._t[key]


LOCALES = ["en-US", "es-ES", "pl", "pt-BR", "pt-PT", "ro", "tr-TR"]


def test_noise_model_section_speaks_in_every_locale():
    from core import noise_model_text as nt
    from core.transcription import errors, model_store

    for loc in LOCALES:
        i18n = _PoI18n(loc)
        for state in (model_store.STATE_ABSENT, model_store.STATE_INCOMPLETE, model_store.STATE_INSTALLED):
            assert nt.state_text(i18n, state)
        assert "10" in nt.confirmation_text(i18n) and "{" not in nt.confirmation_text(i18n)
        assert nt.outcome_text(i18n, "x", None)
        err = errors.TranscriptionError(errors.CANCELLED, "x")
        assert nt.outcome_text(i18n, None, err)
        for key in ("noise_model_label", "noise_model_download_btn", "noise_model_progress",
                    "noise_reduction_fallback"):
            assert i18n.t(key)


def test_fallback_notice_says_which_engine_ran():
    es = _PoI18n("es-ES").t("noise_reduction_fallback")
    assert "neuronal" in es and "básica" in es


def test_gate_failure_does_not_pass_for_basic_reduction(monkeypatch):
    """An absorbed gate error used to be announced as 'basic reduction was used'."""
    from core import audio_processing

    def broken_np_frombuffer(*a, **k):
        raise RuntimeError("numpy blew up inside the gate")

    monkeypatch.setattr(audio_processing.np, "frombuffer", broken_np_frombuffer)
    audio = _pcm(_tone(4800, 48_000))
    # Legacy contract: error is absorbed and the original audio comes back.
    assert audio_processing.apply_noise_gate(audio, 48_000, 1) == audio
    # Strict contract (used by the neural fallback): the error propagates.
    with pytest.raises(RuntimeError):
        audio_processing.apply_noise_gate(audio, 48_000, 1, strict=True)
    # So reduce_voice_noise can never label an unprocessed note as "gate".
    monkeypatch.setattr(nnr, "_denoiser", None)
    monkeypatch.setattr(nnr, "model_path", lambda: "/nonexistent/model.onnx")
    with pytest.raises(RuntimeError):
        nnr.reduce_voice_noise(audio, 48_000, 1)


def test_double_failure_notice_exists_and_is_spoken_on_total_failure():
    for loc in LOCALES:
        text = _PoI18n(loc).t("noise_reduction_failed")
        assert text
    source = open(os.path.join(os.path.dirname(__file__), "..", "client", "ui",
                               "conversation_panel", "voice_recording.py"),
                  encoding="utf-8").read()
    # If both engines fail, the note goes out unprocessed and the user hears it.
    assert 't("noise_reduction_failed")' in source


def test_download_button_follows_state():
    from core import noise_model_text as nt
    from core.transcription import model_store as ms
    assert nt.download_enabled(ms.STATE_ABSENT)
    assert nt.download_enabled(ms.STATE_INCOMPLETE)
    assert not nt.download_enabled(ms.STATE_INSTALLED)
    assert not nt.download_enabled(ms.STATE_ABSENT, job_running=True)


def test_download_job_installs_and_reports_once(tmp_path, monkeypatch):
    """The job the settings button runs, with the network replaced by the
    model file from the environment."""
    from core import noise_model
    from core.noise_model_job import NoiseModelDownloadJob

    calls = []
    monkeypatch.setattr(noise_model, "download",
                        lambda root, progress, should_cancel, session: (progress(5, 10), calls.append(root), "p")[2])
    done, ticks = [], []
    job = NoiseModelDownloadJob(root=str(tmp_path), on_progress=ticks.append,
                                on_finished=lambda r, e: done.append((r, e)))
    job.start()
    job.join(5)
    assert done == [("p", None)] and calls == [str(tmp_path)]


def test_download_job_failure_is_a_report_not_a_crash(tmp_path, monkeypatch):
    from core import noise_model
    from core.noise_model_job import NoiseModelDownloadJob

    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(noise_model, "download", boom)
    done = []
    job = NoiseModelDownloadJob(root=str(tmp_path), on_finished=lambda r, e: done.append((r, e)))
    job.start()
    job.join(5)
    assert len(done) == 1 and done[0][0] is None and done[0][1] is not None


def test_stereo_channels_processed_in_parallel_match_sequential():
    denoiser = _denoiser()
    left = _tone(4800, 48_000).reshape(-1)
    pcm = np.stack([left, left * 0.5], axis=1).astype(np.float32)
    both = denoiser.process(pcm, 48_000)
    for index in range(2):
        alone = denoiser.process(pcm[:, index:index + 1], 48_000)
        assert np.allclose(both[:, index], alone[:, 0])
