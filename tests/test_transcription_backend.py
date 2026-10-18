"""Running a transcription: the backend, the audio it is fed, and the job.

Everything below is a failure the user experiences as the same thing — they
asked for a transcription and something went wrong, out loud, in a window they
cannot see. The mechanisms behind that are specific:

* **`local_files_only=True` is the offline promise of the whole feature.**
  Without it faster-whisper treats the model directory as a Hugging Face
  repository id the moment the directory does not look right, and fetches over
  the network — the audio of a private conversation transcribed by a component
  that phones home. There is a test here whose only job is to fail if that
  argument is ever dropped.

* **CTranslate2's error strings are not an API.** They come out of a C++ layer
  and are reworded between versions, so they are matched by fragment. Each
  family is pinned here, *including* the unknown one: when a message stops
  matching, the answer has to degrade to BACKEND_ERROR — a generic "internal
  error" that still logs the real text — and never to a different diagnosis.
  Telling a user with plenty of VRAM to pick a smaller model, or one with a
  broken driver to buy memory, sends them off to fix what is not broken.

* **The four audio failures are four different sentences.** "It has not been
  downloaded yet" (wait), "this is not audio" (nothing to do), "it is damaged"
  (nothing to do), "the conversion failed" (report it). ffmpeg does not hand
  those over neatly, so the ones that can be told apart are, and the rest fall
  to the generic code on purpose.

* **A cancelled run must leave nothing behind.** The converted WAV can be an
  hour of audio; the ffmpeg process holds the media file open. So cancellation
  is checked in every phase, and every phase is asserted to have deleted its
  temporary and killed its process rather than abandoned it.

* **The log may not carry what was said.** The issue is explicit: backend,
  model, device, languages, durations and technical error text — never the
  transcribed text, and never the audio path, whose file name *is* the WhatsApp
  message id. The last two tests walk this package's logging calls statically
  and then run a whole job with a distinctive sentence in it.

No model is ever loaded, no network is touched and no GPU is required: the
backend gets a fake model factory, and ffmpeg is a small script pretending to
be one.
"""

import ast
import builtins
import inspect
import logging
import os
import re
import sys
import tempfile
import threading
import time
import wave

import pytest

from core.transcription import (
    _fileops,
    audio_prep,
    backend as backend_module,
    cuda_runtime,
    device,
    errors,
    faster_whisper_backend,
    job as job_module,
    model_store,
    precision,
)


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Segment:
    """What faster-whisper yields: start, end and text, and nothing we need."""

    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class _Info:
    def __init__(self, language="pt", language_probability=0.99, duration=4.0):
        self.language = language
        self.language_probability = language_probability
        self.duration = duration


class _FakeWhisper:
    """A loaded model that yields prepared segments instead of decoding."""

    def __init__(self, factory, segments, info, transcribe_error, vad_error,
                 on_segment):
        self._factory = factory
        self._segments = segments
        self._info = info
        self._transcribe_error = transcribe_error
        self._vad_error = vad_error
        self._on_segment = on_segment
        self.yielded = 0

    def transcribe(self, audio, language=None, vad_filter=False, **kwargs):
        self._factory.transcribe_calls.append(
            {"audio": audio, "language": language, "vad_filter": vad_filter}
        )
        if vad_filter and self._vad_error is not None:
            raise self._vad_error
        if self._transcribe_error is not None:
            raise self._transcribe_error

        def _generate():
            for segment in self._segments:
                self.yielded += 1
                yield segment
                if self._on_segment is not None:
                    self._on_segment(self.yielded)

        return _generate(), self._info


class _Factory:
    """Stands in for `faster_whisper.WhisperModel`, recording every load."""

    def __init__(self, segments=(), info=None, load_error=None,
                 transcribe_error=None, vad_error=None, on_segment=None):
        self.segments = list(segments)
        self.info = info if info is not None else _Info()
        self.load_error = load_error
        self.transcribe_error = transcribe_error
        self.vad_error = vad_error
        self.on_segment = on_segment
        self.loads = []
        self.transcribe_calls = []
        self.models = []

    def __call__(self, path, **kwargs):
        self.loads.append({"path": path, **kwargs})
        if self.load_error is not None:
            raise self.load_error
        model = _FakeWhisper(
            self, self.segments, self.info, self.transcribe_error,
            self.vad_error, self.on_segment,
        )
        self.models.append(model)
        return model


class _FakeBackend(backend_module.TranscriptionBackend):
    """A backend for the job's tests: no model, no audio, no waiting."""

    id = "fake"

    def __init__(self, result=None, error=None, on_load=None, on_transcribe=None,
                 available=True):
        self._result = result
        self._error = error
        self._on_load = on_load
        self._on_transcribe = on_transcribe
        self._available = available
        self.requests = []

    def is_available(self):
        return self._available

    def load_model(self, request, should_cancel=None):
        if self._on_load is not None:
            self._on_load(request)

    def transcribe(self, request, progress=None, should_cancel=None):
        self.requests.append(request)
        if self._on_transcribe is not None:
            self._on_transcribe(request)
        if should_cancel is not None and should_cancel():
            raise errors.TranscriptionError(errors.CANCELLED, "cancelled")
        if self._error is not None:
            raise self._error
        if progress is not None:
            progress(1.0)
        return self._result


def _request(tmp_path, **overrides):
    fields = {
        "audio_path": str(tmp_path / "prepared.wav"),
        "models_root": str(tmp_path / "models"),
        "model_id": "tiny",
        "device": device.DEVICE_CPU,
        "compute_type": device.COMPUTE_INT8,
        "duration_seconds": 4.0,
    }
    fields.update(overrides)
    return backend_module.TranscriptionRequest(**fields)


def _ready_model_dir(monkeypatch, tmp_path, name="tiny"):
    """Pretend the model store has a complete model, and say where.

    tokenizer.json is written because the backend refuses to load without it —
    see the test that pins why.
    """
    directory = tmp_path / "models" / name
    directory.mkdir(parents=True)
    (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        model_store, "ensure_ready", lambda root, model_id: str(directory)
    )
    return str(directory)


def _fake_ffmpeg(tmp_path, name="ffmpeg", returncode=0, stderr="", seconds=1.0,
                 sleep=0.0, marker=None):
    """An executable that behaves like ffmpeg for one scenario.

    A generated script behind a launcher, because the code under test spawns a
    real process on purpose: the cancellation path has to kill one, and the
    stderr has to come back through a real pipe.
    """
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import sys, time, wave\n"
        "output = sys.argv[-1]\n"
        f"time.sleep({sleep!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"seconds = {seconds!r}\n"
        "if seconds >= 0:\n"
        "    handle = wave.open(output, 'wb')\n"
        "    handle.setnchannels(1)\n"
        "    handle.setsampwidth(2)\n"
        "    handle.setframerate(16000)\n"
        "    handle.writeframes(b'\\x00\\x00' * int(16000 * seconds))\n"
        "    handle.close()\n"
        f"marker = {marker!r}\n"
        "if marker:\n"
        "    open(marker, 'w').write('done')\n"
        f"sys.exit({returncode!r})\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        launcher = tmp_path / f"{name}.bat"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
    else:
        launcher = tmp_path / f"{name}.sh"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        os.chmod(launcher, 0o755)
    return str(launcher)


def _voice_note(tmp_path, name="3A1B2C3D4E5F6A7B.wzmedia", data=b"\x00" * 2048):
    """A stand-in media file, named the way WinZapp names them: by message id."""
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


@pytest.fixture
def own_temp_dir(tmp_path, monkeypatch):
    """Every temporary this package makes, in a directory the test can count."""
    private = tmp_path / "temp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    return private


def _leftovers(temp_dir):
    return sorted(p.name for p in temp_dir.glob("winzapp-transcribe-*"))


# ── The backend seam ─────────────────────────────────────────────────────────


class TestBackendAvailability:
    """`is_available()` is asked on the machine where the package is missing.

    It is what the settings UI consults before offering the backend at all, so
    it has to answer on an install that has never had faster-whisper — and
    answer without importing it, since the import alone loads hundreds of
    megabytes of CUDA libraries.
    """

    def test_a_missing_package_answers_false_instead_of_raising(self, monkeypatch):
        import importlib.util

        monkeypatch.setattr(
            importlib.util, "find_spec",
            lambda name, *a, **kw: None if name == "faster_whisper" else object(),
        )
        assert faster_whisper_backend.FasterWhisperBackend().is_available() is False

    def test_find_spec_blowing_up_answers_false_too(self, monkeypatch):
        import importlib.util

        def _explode(name, *args, **kwargs):
            raise ValueError("damaged metadata")

        monkeypatch.setattr(importlib.util, "find_spec", _explode)
        assert faster_whisper_backend.FasterWhisperBackend().is_available() is False

    def test_it_says_yes_when_both_packages_are_importable(self, monkeypatch):
        import importlib.util

        monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **kw: object())
        assert faster_whisper_backend.FasterWhisperBackend().is_available() is True

    def test_the_backend_is_never_imported_to_answer(self, monkeypatch):
        """A yes/no answer may not cost a multi-hundred-megabyte import."""
        imported = []
        real_import = builtins.__import__

        def _watch(name, *args, **kwargs):
            imported.append(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _watch)
        faster_whisper_backend.FasterWhisperBackend().is_available()
        assert not [n for n in imported if n.split(".")[0] in
                    ("faster_whisper", "ctranslate2")]


class TestBackendRegistry:
    def test_the_same_instance_is_handed_out_every_time(self):
        """The loaded model is cached on the instance — a new one per call
        would reload several gigabytes per transcription."""
        first = backend_module.get_backend(backend_module.BACKEND_FASTER_WHISPER)
        second = backend_module.get_backend(backend_module.BACKEND_FASTER_WHISPER)
        assert first is second

    def test_an_unknown_id_is_none_rather_than_an_error(self):
        assert backend_module.get_backend("whisper.cpp-from-2031") is None
        assert backend_module.get_backend(None) is None

    def test_the_configured_backend_wins_when_it_can_run(self, monkeypatch):
        usable = _FakeBackend(available=True)
        monkeypatch.setitem(backend_module._instances, "fake", usable)
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("fake",))
        assert backend_module.resolve_backend("fake") is usable

    def test_a_configured_backend_that_cannot_run_falls_through(self, monkeypatch):
        """The settings value outlives the installation it was written on."""
        broken = _FakeBackend(available=False)
        usable = _FakeBackend(available=True)
        monkeypatch.setitem(backend_module._instances, "broken", broken)
        monkeypatch.setitem(backend_module._instances, "fake", usable)
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("fake",))
        assert backend_module.resolve_backend("broken") is usable

    def test_nothing_usable_is_backend_missing(self, monkeypatch):
        monkeypatch.setitem(backend_module._instances, "fake", _FakeBackend(available=False))
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("fake",))
        with pytest.raises(errors.TranscriptionError) as caught:
            backend_module.resolve_backend("fake")
        assert caught.value.code == errors.BACKEND_MISSING

    def test_two_threads_asking_at_once_still_get_one_instance(self, monkeypatch):
        """Two transcriptions started together would otherwise load the model
        into memory twice, and a release() on one of them would free half."""
        built = []

        def _slow_construct(backend_id):
            time.sleep(0.05)
            instance = _FakeBackend()
            built.append(instance)
            return instance

        monkeypatch.setattr(backend_module, "_instances", {})
        monkeypatch.setattr(backend_module, "_construct", _slow_construct)
        handed_out = []
        threads = [
            threading.Thread(
                target=lambda: handed_out.append(backend_module.get_backend("fake"))
            )
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert len(built) == 1
        assert len({id(backend) for backend in handed_out}) == 1

    def test_available_ids_keep_preference_order(self, monkeypatch):
        monkeypatch.setitem(backend_module._instances, "a", _FakeBackend(available=True))
        monkeypatch.setitem(backend_module._instances, "b", _FakeBackend(available=False))
        monkeypatch.setitem(backend_module._instances, "c", _FakeBackend(available=True))
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("a", "b", "c"))
        assert backend_module.available_backend_ids() == ("a", "c")


# ── Loading the model ────────────────────────────────────────────────────────


class TestModelLoading:
    @pytest.fixture(autouse=True)
    def _cuda_runtime(self, monkeypatch):
        # Model factories below replace the native engine, including its CUDA
        # environment. Real preparation has its own loader regression tests.
        monkeypatch.setattr(device, "prepare_cuda_libraries", lambda: (True, (), None))

    def test_the_model_is_loaded_from_the_directory_the_store_vouched_for(
        self, tmp_path, monkeypatch
    ):
        directory = _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 1.0, "a")])
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(_request(tmp_path))

        assert factory.loads[0]["path"] == directory

    def test_the_load_is_offline_and_stays_offline(self, tmp_path, monkeypatch):
        """The one line that keeps a private conversation on this machine.

        Without `local_files_only=True`, faster-whisper falls back to treating
        its first argument as a Hugging Face repository id and downloads —
        which for a feature sold as local transcription is the whole promise
        broken, silently. This test exists to fail if the flag is dropped.
        """
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory()
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(_request(tmp_path))

        assert factory.loads[0]["local_files_only"] is True
        # Nothing may steer it at a cache or a repository either.
        assert "download_root" not in factory.loads[0]
        assert "repo_id" not in factory.loads[0]

    def test_the_device_and_compute_type_are_passed_through_untouched(
        self, tmp_path, monkeypatch
    ):
        """device.py decides these; the backend may not have a second opinion."""
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory()
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(
            _request(tmp_path, device=device.DEVICE_CUDA,
                     compute_type=device.COMPUTE_FLOAT16)
        )

        assert factory.loads[0]["device"] == device.DEVICE_CUDA
        assert factory.loads[0]["compute_type"] == device.COMPUTE_FLOAT16

    def test_a_second_transcription_reuses_the_loaded_model(self, tmp_path, monkeypatch):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 4.0, "a")])
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.transcribe(_request(tmp_path))
        backend.transcribe(_request(tmp_path))

        assert len(factory.loads) == 1

    @pytest.mark.parametrize(
        "changed",
        [
            {"device": device.DEVICE_CUDA},
            {"compute_type": device.COMPUTE_FLOAT32},
        ],
        ids=["device", "compute_type"],
    )
    def test_changing_what_was_loaded_reloads_it(self, tmp_path, monkeypatch, changed):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 4.0, "a")])
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(_request(tmp_path))
        backend.load_model(_request(tmp_path, **changed))

        assert len(factory.loads) == 2

    def test_a_different_model_reloads_too(self, tmp_path, monkeypatch):
        directories = {
            "tiny": tmp_path / "models" / "tiny",
            "small": tmp_path / "models" / "small",
        }
        for path in directories.values():
            path.mkdir(parents=True)
            (path / "tokenizer.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            model_store, "ensure_ready",
            lambda root, model_id: str(directories[model_id]),
        )
        factory = _Factory()
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(_request(tmp_path, model_id="tiny"))
        backend.load_model(_request(tmp_path, model_id="small"))

        assert [load["path"] for load in factory.loads] == [
            str(directories["tiny"]), str(directories["small"])
        ]

    def test_releasing_drops_the_model_so_the_next_run_reloads(
        self, tmp_path, monkeypatch
    ):
        """Part 6 hands the VRAM back; the next transcription must still work."""
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory()
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        backend.load_model(_request(tmp_path))
        backend.release()
        backend.load_model(_request(tmp_path))

        assert len(factory.loads) == 2

    def test_releasing_while_a_load_is_in_flight_still_frees_it(
        self, tmp_path, monkeypatch
    ):
        """The load runs outside the lock, so release() can land mid-load.

        That is not a corner case, it is the case part 6 has: large-v3 takes
        tens of seconds to come up and the user gives up on it. If the load
        that was already running then wrote itself into the cache, the user
        would have been told the memory came back — so they will not ask a
        second time — while several gigabytes of VRAM stayed held for the rest
        of the session.
        """
        _ready_model_dir(monkeypatch, tmp_path)
        loading = threading.Event()
        released = threading.Event()
        loads = []

        def _slow_factory(path, **kwargs):
            loads.append(path)
            if len(loads) == 1:
                loading.set()
                released.wait(5)
            return object()

        backend = faster_whisper_backend.FasterWhisperBackend(
            model_factory=_slow_factory
        )
        worker = threading.Thread(
            target=backend.load_model, args=(_request(tmp_path),), daemon=True
        )
        worker.start()
        assert loading.wait(5), "the load never started"
        backend.release()
        released.set()
        worker.join(5)

        # Observable proof the cache is empty: the next run has to load again.
        backend.load_model(_request(tmp_path))
        assert len(loads) == 2, "the released model was cached by the late load"

    def test_a_model_missing_its_tokenizer_never_reaches_faster_whisper(
        self, tmp_path, monkeypatch
    ):
        """The one file whose absence sends faster-whisper to the network.

        Its loader answers a missing tokenizer.json with
        Tokenizer.from_pretrained("openai/whisper-<size>"), which downloads and
        does not consult local_files_only at all. The model store would have
        caught this first, but "nothing leaves this machine" is too important
        to rest on another module's invariant, so the backend checks too.
        """
        directory = tmp_path / "models" / "tiny"
        directory.mkdir(parents=True)
        monkeypatch.setattr(
            model_store, "ensure_ready", lambda root, model_id: str(directory)
        )
        factory = _Factory()
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.load_model(_request(tmp_path))

        assert caught.value.code == errors.MODEL_CORRUPTED
        assert factory.loads == [], "faster-whisper was given the chance to download"

    def test_a_model_that_is_not_installed_says_so(self, tmp_path):
        """Through the real store: the cheap check runs before every run."""
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=_Factory())
        with pytest.raises(errors.TranscriptionError) as caught:
            backend.load_model(_request(tmp_path, model_id="tiny"))
        assert caught.value.code == errors.MODEL_NOT_INSTALLED

    def test_a_load_failure_is_classified_like_any_other_backend_error(
        self, tmp_path, monkeypatch
    ):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(load_error=RuntimeError("CUDA failed with error out of memory"))
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.load_model(_request(tmp_path, device=device.DEVICE_CUDA))
        assert caught.value.code == errors.INSUFFICIENT_VRAM


# ── The error table ──────────────────────────────────────────────────────────


class TestErrorClassification:
    """CTranslate2's messages, as they actually arrive, mapped to our codes.

    Matched by fragment because they are C++ strings that get reworded between
    versions. The important case is the last one: an unrecognised message has
    to become BACKEND_ERROR, never a guess.
    """

    @pytest.mark.parametrize(
        "message, device_name, expected",
        [
            ("CUDA failed with error out of memory", "cuda", errors.INSUFFICIENT_VRAM),
            ("cublas runtime error : CUBLAS_STATUS_ALLOC_FAILED", "cuda",
             errors.INSUFFICIENT_VRAM),
            # NVIDIA documents this one as "out of memory" too, and it names
            # cuDNN — so the missing-library table would have claimed it and
            # sent a user with a full card off to reinstall a working driver.
            ("cuDNN failed: CUDNN_STATUS_ALLOC_FAILED", "cuda",
             errors.INSUFFICIENT_VRAM),
            ("CUDA out of memory. Tried to allocate 2.00 GiB", "cuda",
             errors.INSUFFICIENT_VRAM),
            ("std::bad_alloc", "cpu", errors.INSUFFICIENT_RAM),
            ("Cannot allocate memory", "cpu", errors.INSUFFICIENT_RAM),
            ("Out of memory", "cpu", errors.INSUFFICIENT_RAM),
            ("Out of memory", "cuda", errors.INSUFFICIENT_VRAM),
            ("no CUDA-capable device is detected", "cuda", errors.CUDA_UNAVAILABLE),
            ("CUDA driver version is insufficient for CUDA runtime version",
             "cuda", errors.CUDA_UNAVAILABLE),
            ("Library cudnn_ops_infer64_8.dll is not found", "cuda",
             errors.CUDA_UNAVAILABLE),
            ("Could not load library cublas64_12.dll", "cuda",
             errors.CUDA_UNAVAILABLE),
            # A card this CTranslate2 build has no kernels for — too new, or
            # older than its compiled -gencode list. Without these two the
            # message fell through to BACKEND_ERROR, which carries no offer to
            # re-run on the processor, where it would have worked.
            ("no kernel image is available for execution on the device", "cuda",
             errors.CUDA_UNAVAILABLE),
            ("CUDA error: invalid device function", "cuda",
             errors.CUDA_UNAVAILABLE),
            ("Invalid model configuration", "cpu", errors.BACKEND_ERROR),
            ("something nobody has ever seen", "cuda", errors.BACKEND_ERROR),
        ],
    )
    def test_each_family_reaches_the_code_the_user_can_act_on(
        self, message, device_name, expected
    ):
        error = faster_whisper_backend.classify_backend_error(
            RuntimeError(message), device_name
        )
        assert error.code == expected

    def test_an_allocation_failure_is_memory_even_though_it_names_cublas(self):
        """CUBLAS_STATUS_ALLOC_FAILED is a full card, not a missing cuBLAS.

        The order of the tables is what decides this, and getting it backwards
        sends a user with a working driver off to reinstall it.
        """
        error = faster_whisper_backend.classify_backend_error(
            RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"), "cuda"
        )
        assert error.code == errors.INSUFFICIENT_VRAM

    def test_a_card_without_kernels_becomes_an_offer_to_use_the_processor(self):
        """The point of classifying it at all: the CPU is the answer.

        The retry allowlist is deliberately short, so a GPU fault the CPU could
        cure has to arrive as one of its two codes rather than as the catch-all.
        """
        for message in ("no kernel image is available for execution on the device",
                        "CUDA error: invalid device function"):
            error = faster_whisper_backend.classify_backend_error(
                RuntimeError(message), "cuda"
            )
            assert device.should_retry_on_cpu(error, device.DEVICE_CUDA) is True

    def test_pythons_own_memory_error_is_recognised(self):
        error = faster_whisper_backend.classify_backend_error(MemoryError(), "cpu")
        assert error.code == errors.INSUFFICIENT_RAM

    def test_the_technical_text_is_kept_for_the_log_and_not_for_the_screen(self):
        error = faster_whisper_backend.classify_backend_error(
            RuntimeError("Invalid model configuration"), "cpu"
        )
        assert "Invalid model configuration" in error.log_line
        assert str(error) == errors.BACKEND_ERROR


# ── Transcribing ─────────────────────────────────────────────────────────────


def _write_wav(path, frames=b"\x00\x40" * 1600, rate=16000, channels=1, width=2):
    handle = wave.open(str(path), "wb")
    handle.setnchannels(channels)
    handle.setsampwidth(width)
    handle.setframerate(rate)
    handle.writeframes(frames)
    handle.close()
    return str(path)


class TestTranscribe:
    def test_the_prepared_audio_is_decoded_here_not_by_pyav(
        self, tmp_path, monkeypatch
    ):
        """A path makes faster-whisper open the file with PyAV, which fails
        on an old `av` ('metadata_errors') after the model loaded; the
        prepared WAV is ours, so the samples go in instead."""
        numpy = pytest.importorskip("numpy")
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 0.1, "oi")], info=_Info())
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)
        wav = _write_wav(tmp_path / "prepared.wav")

        backend.transcribe(_request(tmp_path, audio_path=wav))

        audio = factory.transcribe_calls[0]["audio"]
        assert isinstance(audio, numpy.ndarray)
        assert audio.dtype == numpy.float32

    def test_an_unreadable_prepared_file_still_goes_by_path(
        self, tmp_path, monkeypatch
    ):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 0.1, "oi")], info=_Info())
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)
        request = _request(tmp_path)

        backend.transcribe(request)

        assert factory.transcribe_calls[0]["audio"] == request.audio_path

    def test_the_result_carries_the_text_the_language_and_the_times(
        self, tmp_path, monkeypatch
    ):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(
            segments=[_Segment(0.0, 2.0, " primeira "), _Segment(2.0, 4.0, "segunda")],
            info=_Info(language="pt", language_probability=0.87, duration=4.0),
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        result = backend.transcribe(_request(tmp_path))

        assert result.text == "primeira segunda"
        assert result.language == "pt"
        assert result.language_probability == 0.87
        assert result.duration_seconds == 4.0
        assert [(s.start, s.end) for s in result.segments] == [(0.0, 2.0), (2.0, 4.0)]
        assert result.backend == backend_module.BACKEND_FASTER_WHISPER
        assert (result.model_id, result.device, result.compute_type) == (
            "tiny", device.DEVICE_CPU, device.COMPUTE_INT8
        )

    def test_progress_only_moves_forward_and_ends_at_one(self, tmp_path, monkeypatch):
        """A screen reader reads every number it is given, including a
        percentage that went backwards. The last segment can also end past the
        duration we measured, because the model pads the tail."""
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(
            segments=[_Segment(0.0, 1.0, "a"), _Segment(1.0, 2.0, "b"),
                      _Segment(2.0, 8.0, "c")],
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)
        seen = []

        backend.transcribe(_request(tmp_path, duration_seconds=4.0), progress=seen.append)

        assert seen == sorted(seen)
        assert all(0.0 <= value <= 1.0 for value in seen)
        assert seen[0] == pytest.approx(0.25)
        assert seen[-1] == 1.0

    def test_an_unknown_duration_still_finishes_at_one(self, tmp_path, monkeypatch):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(
            segments=[_Segment(0.0, 1.0, "a")], info=_Info(duration=None)
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)
        seen = []

        backend.transcribe(_request(tmp_path, duration_seconds=None), progress=seen.append)

        assert seen == [1.0]

    def test_cancelling_stops_consuming_the_generator(self, tmp_path, monkeypatch):
        """Between two segments is the only moment the C++ call is not on the
        stack, so it is the only place a cancellation can be honoured — and
        honouring it has to mean stopping the generator, since that generator
        *is* the decoding."""
        _ready_model_dir(monkeypatch, tmp_path)
        cancelled = []
        factory = _Factory(
            segments=[_Segment(0.0, 1.0, "a"), _Segment(1.0, 2.0, "b"),
                      _Segment(2.0, 3.0, "c")],
            # Flipped when the generator is asked for its second segment, which
            # is the earliest point a run can be cancelled mid-decode.
            on_segment=lambda count: cancelled.append(True),
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.transcribe(_request(tmp_path), should_cancel=lambda: bool(cancelled))

        assert caught.value.code == errors.CANCELLED
        assert factory.models[0].yielded == 2, "the third segment was still decoded"

    def test_a_cancel_before_the_first_segment_never_loads_anything(
        self, tmp_path, monkeypatch
    ):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 1.0, "a")])
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.transcribe(_request(tmp_path), should_cancel=lambda: True)

        assert caught.value.code == errors.CANCELLED
        assert factory.loads == []

    def test_the_voice_activity_filter_is_asked_for(self, tmp_path, monkeypatch):
        """Whisper answers silence with invented sentences, and a voice note is
        mostly silence at the end. A listener cannot tell an invented sentence
        from a real one."""
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(segments=[_Segment(0.0, 1.0, "a")])
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        result = backend.transcribe(_request(tmp_path))

        assert factory.transcribe_calls[0]["vad_filter"] is True
        assert result.vad_used is True

    def test_a_filter_that_cannot_load_costs_the_filter_not_the_transcription(
        self, tmp_path, monkeypatch
    ):
        """onnxruntime is a separate binary in the frozen build, and the user
        cannot install it into one. Without the filter the transcription is
        worse; without the transcription there is nothing at all."""
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(
            segments=[_Segment(0.0, 1.0, "ola")],
            vad_error=RuntimeError("Failed to load onnxruntime providers"),
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        result = backend.transcribe(_request(tmp_path))

        assert [call["vad_filter"] for call in factory.transcribe_calls] == [True, False]
        assert result.text == "ola"
        # The downgrade may not be invisible: without the filter, a note ending
        # in silence comes back with an invented sentence on the end, and a
        # listener has no way to tell it from a real one. A warning in the log
        # is not a signal to somebody who cannot see the log.
        assert result.vad_used is False

    def test_a_real_failure_is_not_retried_without_the_filter(
        self, tmp_path, monkeypatch
    ):
        _ready_model_dir(monkeypatch, tmp_path)
        factory = _Factory(transcribe_error=RuntimeError("Invalid model configuration"))
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.transcribe(_request(tmp_path))

        assert caught.value.code == errors.BACKEND_ERROR
        assert len(factory.transcribe_calls) == 1

    def test_nothing_said_comes_back_as_an_empty_result_not_an_error(
        self, tmp_path, monkeypatch
    ):
        """With the filter on, a note holding only noise yields no segments.

        It is not an error and must not become one — but part 6 has to know,
        because a window with nothing in it is indistinguishable from a crash
        to somebody listening to it.
        """
        _ready_model_dir(monkeypatch, tmp_path)
        backend = faster_whisper_backend.FasterWhisperBackend(
            model_factory=_Factory(segments=[])
        )

        result = backend.transcribe(_request(tmp_path))

        assert result.text == ""
        assert result.is_empty is True
        assert result.segments == ()

    def test_a_result_with_text_is_not_empty(self):
        assert _result("alguma coisa").is_empty is False
        assert _result("   ").is_empty is True

    def test_a_failure_while_decoding_is_classified_too(self, tmp_path, monkeypatch):
        """The generator does the work, so most failures arrive mid-loop."""
        _ready_model_dir(monkeypatch, tmp_path)

        def _explode(count):
            raise RuntimeError("std::bad_alloc")

        factory = _Factory(
            segments=[_Segment(0.0, 1.0, "a"), _Segment(1.0, 2.0, "b")],
            on_segment=_explode,
        )
        backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

        with pytest.raises(errors.TranscriptionError) as caught:
            backend.transcribe(_request(tmp_path))
        assert caught.value.code == errors.INSUFFICIENT_RAM


# ── Preparing the audio ──────────────────────────────────────────────────────


class TestReadWaveform:
    def test_samples_are_scaled_to_minus_one_to_one(self, tmp_path):
        numpy = pytest.importorskip("numpy")
        wav = _write_wav(tmp_path / "a.wav", frames=b"\x00\x40\x00\xc0")

        samples = audio_prep.read_waveform(wav)

        assert samples.dtype == numpy.float32
        assert list(samples) == [0.5, -0.5]

    @pytest.mark.parametrize(
        "overrides", [{"rate": 44100}, {"channels": 2}, {"width": 1}]
    )
    def test_a_file_that_is_not_16k_mono_pcm16_is_left_to_the_backend(
        self, tmp_path, overrides
    ):
        pytest.importorskip("numpy")
        wav = _write_wav(tmp_path / "a.wav", frames=b"\x00" * 8, **overrides)

        assert audio_prep.read_waveform(wav) is None

    def test_a_missing_file_is_none_rather_than_an_error(self, tmp_path):
        assert audio_prep.read_waveform(str(tmp_path / "gone.wav")) is None


class TestAudioPreparation:
    def test_a_conversion_yields_a_file_and_its_length(self, tmp_path, own_temp_dir):
        """The length is read from the converted file's own header — exact,
        and free next to the ffprobe call it replaces."""
        source = _voice_note(tmp_path)
        ffmpeg = _fake_ffmpeg(tmp_path, seconds=2.0)

        prepared = audio_prep.prepare_audio(ffmpeg, source)
        try:
            assert os.path.isfile(prepared.path)
            assert prepared.duration_seconds == pytest.approx(2.0, abs=0.01)
        finally:
            audio_prep.discard(prepared)
        assert _leftovers(own_temp_dir) == []

    def test_the_command_carries_the_target_format(self):
        """Pinned on the command itself: a conversion at the wrong rate is not
        an error anywhere, it is simply a worse transcription."""
        source = inspect.getsource(audio_prep._run_ffmpeg)
        assert '"-ar", str(TARGET_SAMPLE_RATE)' in source
        assert '"-ac", str(TARGET_CHANNELS)' in source
        assert audio_prep.TARGET_SAMPLE_RATE == 16000
        assert audio_prep.TARGET_CHANNELS == 1

    def test_a_missing_file_is_a_download_that_has_not_finished(self, tmp_path):
        ffmpeg = _fake_ffmpeg(tmp_path)
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, str(tmp_path / "nothing.wzmedia"))
        assert caught.value.code == errors.MEDIA_NOT_DOWNLOADED

    def test_an_empty_file_is_the_same_answer(self, tmp_path):
        ffmpeg = _fake_ffmpeg(tmp_path)
        source = _voice_note(tmp_path, data=b"")
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, source)
        assert caught.value.code == errors.MEDIA_NOT_DOWNLOADED

    def test_a_file_ffmpeg_cannot_parse_is_an_unsupported_format(
        self, tmp_path, own_temp_dir
    ):
        ffmpeg = _fake_ffmpeg(
            tmp_path, returncode=1, seconds=-1,
            stderr="Invalid data found when processing input\n",
        )
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, _voice_note(tmp_path))
        assert caught.value.code == errors.UNSUPPORTED_AUDIO_FORMAT
        assert _leftovers(own_temp_dir) == []

    def test_a_truncated_file_is_told_apart_from_an_unreadable_one(
        self, tmp_path, own_temp_dir
    ):
        """A send that died mid-upload prints both kinds of line, which is why
        the truncation markers are checked first."""
        ffmpeg = _fake_ffmpeg(
            tmp_path, returncode=1, seconds=-1,
            stderr="moov atom not found\nInvalid data found when processing input\n",
        )
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, _voice_note(tmp_path))
        assert caught.value.code == errors.AUDIO_INCOMPLETE
        assert _leftovers(own_temp_dir) == []

    def test_a_conversion_that_produced_no_samples_is_damaged_audio(
        self, tmp_path, own_temp_dir
    ):
        ffmpeg = _fake_ffmpeg(tmp_path, returncode=0, seconds=0.0)
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, _voice_note(tmp_path))
        assert caught.value.code == errors.AUDIO_INCOMPLETE
        assert _leftovers(own_temp_dir) == []

    @pytest.mark.parametrize("stderr", [
        # The C library's strerror(ENOSPC), which ffmpeg prints on its own.
        "[out#0/wav] Error writing trailer: No space left on device\n",
        # A partial write says "partial file" too; the disk is what to fix.
        "partial file\nav_interleaved_write_frame(): No space left on device\n",
        # The Windows system messages for ERROR_DISK_FULL / _HANDLE_DISK_FULL.
        "Error writing output: There is not enough space on the disk.\n",
        "Error writing output: The disk is full.\n",
    ], ids=["enospc", "enospc-after-partial", "winerror-112", "winerror-39"])
    def test_a_full_temp_disk_is_said_as_such_not_blamed_on_the_audio(
        self, stderr, tmp_path, own_temp_dir
    ):
        """The WAV is far larger than the note (~115 MB per hour): the
        decryption fits and the conversion does not. FFMPEG_FAILED would say
        the audio could not be converted — sending the user after a
        recording that is fine. Synthetic stderr: not measured on a real
        full disk."""
        ffmpeg = _fake_ffmpeg(tmp_path, returncode=1, seconds=-1, stderr=stderr)
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, _voice_note(tmp_path))
        assert caught.value.code == errors.TEMP_NO_DISK_SPACE
        assert _leftovers(own_temp_dir) == []

    def test_any_other_failure_stays_generic(self, tmp_path, own_temp_dir):
        """A wrong diagnosis sends a blind user to fix what is not broken."""
        ffmpeg = _fake_ffmpeg(
            tmp_path, returncode=1, seconds=-1, stderr="Error opening output file\n"
        )
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, _voice_note(tmp_path))
        assert caught.value.code == errors.FFMPEG_FAILED
        assert _leftovers(own_temp_dir) == []

    def test_a_missing_ffmpeg_is_reported_as_such(self, tmp_path):
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(
                str(tmp_path / "no-ffmpeg-here.exe"), _voice_note(tmp_path)
            )
        assert caught.value.code == errors.FFMPEG_FAILED

    def test_our_own_file_names_never_reach_the_error_detail(self, tmp_path):
        """ffmpeg echoes the path it read, and that file's name is the message
        id — which the issue forbids the log to carry."""
        source = _voice_note(tmp_path)
        ffmpeg = _fake_ffmpeg(
            tmp_path, returncode=1, seconds=-1,
            stderr=f"Error opening input file {source}.\n",
        )
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(ffmpeg, source)
        assert os.path.basename(source) not in caught.value.log_line
        assert "<audio>" in caught.value.log_line

    def test_cancelling_kills_the_conversion_and_deletes_the_temporary(
        self, tmp_path, own_temp_dir, monkeypatch
    ):
        """Abandoning the process would keep the media file open for the rest
        of the session, and leave an hour of WAV in %TEMP%.

        The process is inspected through `_kill` rather than through a marker
        the fake would write: the fake is a script behind a launcher, so on
        Windows the thing WinZapp spawns is the launcher and the interpreter is
        its child. Real ffmpeg is a direct child, which is what makes the kill
        below the whole story there.
        """
        ffmpeg = _fake_ffmpeg(tmp_path, sleep=1.5, marker=str(tmp_path / "finished"))
        killed = []
        real_kill = _fileops.kill_process

        def _spy(process, program):
            killed.append(process)
            real_kill(process, program)

        monkeypatch.setattr(_fileops, "kill_process", _spy)

        # False the first time, so the run gets as far as spawning ffmpeg: the
        # check before that one is the cheap "already cancelled?" guard, and it
        # would answer without there being a process to kill.
        answers = iter([False])

        started = time.monotonic()
        with pytest.raises(errors.TranscriptionError) as caught:
            audio_prep.prepare_audio(
                ffmpeg, _voice_note(tmp_path),
                should_cancel=lambda: next(answers, True),
            )
        elapsed = time.monotonic() - started

        assert caught.value.code == errors.CANCELLED
        assert elapsed < 1.5, "the cancellation waited for the conversion to end"
        assert killed and killed[0].poll() is not None
        assert _leftovers(own_temp_dir) == []

    def test_the_temporary_is_not_named_after_the_message(self, tmp_path, own_temp_dir):
        ffmpeg = _fake_ffmpeg(tmp_path, seconds=1.0)
        source = _voice_note(tmp_path)
        prepared = audio_prep.prepare_audio(ffmpeg, source)
        try:
            assert os.path.basename(source) not in prepared.path
            assert os.path.splitext(os.path.basename(source))[0] not in prepared.path
        finally:
            audio_prep.discard(prepared)

    def test_the_temporary_is_gone_once_discarded(self, tmp_path, own_temp_dir):
        ffmpeg = _fake_ffmpeg(tmp_path, seconds=1.0)
        prepared = audio_prep.prepare_audio(ffmpeg, _voice_note(tmp_path))
        path = prepared.path
        audio_prep.discard(prepared)
        assert not os.path.exists(path)
        assert _leftovers(own_temp_dir) == []


# ── The job ──────────────────────────────────────────────────────────────────


def _result(text="tudo certo"):
    return backend_module.TranscriptionResult(
        text=text, language="pt", language_probability=0.9, duration_seconds=1.0,
        segments=(backend_module.TranscriptionSegment(0.0, 1.0, text),),
        backend="fake", model_id="tiny", device="cpu", compute_type="int8",
    )


class _Watcher:
    """Collects what part 6 will turn into announcements."""

    def __init__(self):
        self.phases = []
        self.progress = []
        self.finished = []

    def on_phase(self, phase):
        self.phases.append(phase)

    def on_progress(self, fraction):
        self.progress.append(fraction)

    def on_finished(self, result, error):
        self.finished.append((result, error))


def _run_job(tmp_path, backend, ffmpeg=None, watcher=None, **kwargs):
    watcher = watcher or _Watcher()
    job = job_module.TranscriptionJob(
        audio_path=kwargs.pop("audio_path", _voice_note(tmp_path)),
        ffmpeg=ffmpeg if ffmpeg is not None else _fake_ffmpeg(tmp_path, seconds=1.0),
        models_root=str(tmp_path / "models"),
        model_id="tiny",
        backend=backend,
        on_phase=watcher.on_phase,
        on_progress=watcher.on_progress,
        on_finished=watcher.on_finished,
        probe=kwargs.pop("probe", lambda: device.HardwareProbe(total_ram_mb=16384,
                                                              available_ram_mb=8192)),
        **kwargs,
    )
    job.start()
    job.join(30)
    assert job.phase in job_module.TERMINAL_PHASES, "the job never finished"
    return job, watcher


class TestJob:
    def test_the_phases_arrive_in_the_order_the_user_will_hear_them(
        self, tmp_path, own_temp_dir
    ):
        backend = _FakeBackend(result=_result())
        _job, watcher = _run_job(tmp_path, backend)
        assert watcher.phases == [
            job_module.PHASE_PREPARING_AUDIO,
            job_module.PHASE_LOADING_MODEL,
            job_module.PHASE_TRANSCRIBING,
            job_module.PHASE_DONE,
        ]

    def test_the_result_is_reported_exactly_once(self, tmp_path, own_temp_dir):
        """The UI holds a row and a spoken "transcribing…" until it hears back."""
        expected = _result()
        _job, watcher = _run_job(tmp_path, _FakeBackend(result=expected))
        assert watcher.finished == [(expected, None)]

    def test_the_backend_receives_the_prepared_audio_and_the_decisions(
        self, tmp_path, own_temp_dir
    ):
        backend = _FakeBackend(result=_result())
        job, _watcher = _run_job(tmp_path, backend, language="pt")
        request = backend.requests[0]
        assert request.audio_path.endswith(".wav")
        assert request.duration_seconds == pytest.approx(1.0, abs=0.01)
        assert request.language == "pt"
        assert (request.device, request.compute_type) == (job.device, job.compute_type)

    def test_the_measured_compute_capability_travels_with_the_request(
        self, tmp_path, own_temp_dir
    ):
        # whisper.cpp's CUDA build cannot run on sm_120, and the backend must
        # not re-probe to find out: the job's own measurement is handed over.
        backend = _FakeBackend(result=_result())
        probe = device.HardwareProbe(cuda_available=True, cuda_device_count=1,
                                     compute_capability=(12, 0), total_vram_mb=16384,
                                     free_vram_mb=12000, total_ram_mb=16384,
                                     available_ram_mb=8192)
        _run_job(tmp_path, backend, probe=lambda: probe)
        assert backend.requests[0].compute_capability == (12, 0)

    def test_the_prepared_file_is_deleted_when_the_run_ends(
        self, tmp_path, own_temp_dir
    ):
        backend = _FakeBackend(result=_result())
        _job, _watcher = _run_job(tmp_path, backend)
        assert not os.path.exists(backend.requests[0].audio_path)
        assert _leftovers(own_temp_dir) == []

    def test_the_hardware_is_probed_when_the_decision_is_made(
        self, tmp_path, own_temp_dir
    ):
        """device.py is explicit: a probe taken at startup describes a machine
        that no longer exists by the time a model is loaded, because free
        memory is what is being planned against."""
        probes = []

        def _probe():
            probes.append(time.monotonic())
            return device.HardwareProbe(total_ram_mb=16384, available_ram_mb=8192)

        backend = _FakeBackend(result=_result())
        job, _watcher = _run_job(tmp_path, backend, probe=_probe)
        assert len(probes) == 1
        assert job.device == device.DEVICE_CPU
        assert job.device_reason == device.REASON_NO_CUDA_FOUND
        assert job.compute_type == device.COMPUTE_INT8

    def test_a_chosen_precision_reaches_the_backend_and_is_readable(
        self, tmp_path, own_temp_dir
    ):
        """Part 11: the stored choice is resolved against the device the job
        landed on, and the choice travels with the run for the loading line."""
        backend = _FakeBackend(result=_result())
        job, _watcher = _run_job(
            tmp_path, backend, compute_type_preference=device.COMPUTE_INT16,
            probe=lambda: device.HardwareProbe(
                total_ram_mb=16384, available_ram_mb=8192,
                cpu_compute_types=("float32", "int16", "int8", "int8_float32")),
        )
        assert backend.requests[0].compute_type == device.COMPUTE_INT16
        assert job.compute_type == device.COMPUTE_INT16
        assert job.precision == precision.PrecisionChoice(
            device.COMPUTE_INT16, device.COMPUTE_INT16)

    def test_a_precision_the_device_cannot_run_is_replaced_before_the_load(
        self, tmp_path, own_temp_dir
    ):
        """float16 on the processor is a refused load in CTranslate2, not a
        slower one: what reaches the backend is the replacement."""
        backend = _FakeBackend(result=_result())
        job, _watcher = _run_job(
            tmp_path, backend, compute_type_preference=device.COMPUTE_FLOAT16,
            probe=lambda: device.HardwareProbe(
                total_ram_mb=16384, available_ram_mb=8192,
                cpu_compute_types=("float32", "int16", "int8", "int8_float32")),
        )
        assert backend.requests[0].compute_type == device.COMPUTE_FLOAT32
        assert job.precision.replaced
        assert job.precision.requested == device.COMPUTE_FLOAT16

    def test_automatic_is_what_it_always_was(self, tmp_path, own_temp_dir):
        backend = _FakeBackend(result=_result())
        job, _watcher = _run_job(tmp_path, backend)
        assert job.precision == precision.PrecisionChoice(device.COMPUTE_INT8)

    def test_the_device_is_readable_when_the_loading_phase_is_announced(
        self, tmp_path, own_temp_dir
    ):
        """Part 6 says which processor is being used from inside this callback.

        Asserting job.device after join() is not the same assertion: it passed
        happily while the phase was announced *before* the probe ran, leaving
        the attributes None at the only moment anyone reads them — and
        device_reason_i18n_key(None) does not fail, it falls back to "you asked
        for the CPU". A user on a GPU machine would then hear the app name the
        wrong reason, confidently, while the model loads onto CUDA.
        """
        seen = {}

        # Built here rather than through _run_job() because the callback has to
        # be able to see the job while it is still running, not after it ends.
        def _on_phase(phase):
            # setdefault, not assignment: what part 6 reacts to is the FIRST
            # time it hears the phase, so a later, better-populated repeat of
            # the same announcement must not be able to paper over it.
            if phase == job_module.PHASE_LOADING_MODEL:
                seen.setdefault("device", job.device)
                seen.setdefault("reason", job.device_reason)
                seen.setdefault("compute", job.compute_type)

        job = job_module.TranscriptionJob(
            audio_path=_voice_note(tmp_path),
            ffmpeg=_fake_ffmpeg(tmp_path, seconds=1.0),
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            backend=_FakeBackend(result=_result()),
            on_phase=_on_phase,
            probe=lambda: device.HardwareProbe(
                total_ram_mb=16384, available_ram_mb=8192
            ),
        )
        job.start()
        job.join(30)
        assert job.phase == job_module.PHASE_DONE

        assert seen["device"] == device.DEVICE_CPU
        assert seen["reason"] == device.REASON_NO_CUDA_FOUND
        assert seen["compute"] == device.COMPUTE_INT8

    def test_a_failure_becomes_a_failed_phase_and_an_error(
        self, tmp_path, own_temp_dir
    ):
        error = errors.TranscriptionError(errors.BACKEND_ERROR, "something technical")
        _job, watcher = _run_job(tmp_path, _FakeBackend(error=error))
        assert watcher.phases[-1] == job_module.PHASE_FAILED
        assert watcher.finished == [(None, error)]

    def test_an_unexpected_exception_still_reports_something(
        self, tmp_path, own_temp_dir
    ):
        """A worker thread that dies quietly leaves the UI waiting forever."""
        backend = _FakeBackend(error=ValueError("a programming mistake"))
        _job, watcher = _run_job(tmp_path, backend)
        result, error = watcher.finished[0]
        assert result is None
        assert error.code == errors.BACKEND_ERROR
        assert watcher.phases[-1] == job_module.PHASE_FAILED

    def test_a_callback_that_raises_does_not_cost_the_final_report(
        self, tmp_path, own_temp_dir
    ):
        watcher = _Watcher()

        def _bad_phase(phase):
            watcher.phases.append(phase)
            raise RuntimeError("the panel was already destroyed")

        watcher.on_phase = _bad_phase
        _job, watcher = _run_job(tmp_path, _FakeBackend(result=_result()), watcher=watcher)
        assert len(watcher.finished) == 1

    @pytest.mark.parametrize("phase", ["preparing", "loading", "transcribing"])
    def test_cancelling_in_any_phase_ends_cancelled_and_leaves_nothing_behind(
        self, tmp_path, own_temp_dir, phase
    ):
        """Three different waits, one guarantee: no process, no temporary."""
        holder = {}
        ffmpeg = _fake_ffmpeg(tmp_path, seconds=1.0)

        if phase == "preparing":
            ffmpeg = _fake_ffmpeg(tmp_path, name="slow", seconds=1.0, sleep=2.0)
            backend = _FakeBackend(result=_result())
        elif phase == "loading":
            backend = _FakeBackend(
                result=_result(), on_load=lambda request: holder["job"].cancel()
            )
        else:
            backend = _FakeBackend(
                result=_result(), on_transcribe=lambda request: holder["job"].cancel()
            )

        watcher = _Watcher()
        job = job_module.TranscriptionJob(
            audio_path=_voice_note(tmp_path),
            ffmpeg=ffmpeg,
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            backend=backend,
            on_phase=watcher.on_phase,
            on_finished=watcher.on_finished,
            probe=lambda: device.HardwareProbe(total_ram_mb=16384, available_ram_mb=8192),
        )
        holder["job"] = job
        job.start()
        if phase == "preparing":
            # From the caller's thread, while ffmpeg is still sleeping: that is
            # the only way to reach the branch that has to kill a process.
            job.cancel()
        job.join(30)

        assert watcher.phases[-1] == job_module.PHASE_CANCELLED
        result, error = watcher.finished[0]
        assert result is None and error.code == errors.CANCELLED
        assert _leftovers(own_temp_dir) == []

    def test_a_prepared_file_is_reused_and_never_deleted(
        self, tmp_path, own_temp_dir
    ):
        """Part 4 retries a failed GPU run on the CPU with the same audio.

        Converting it again is the whole wait a second time — 40 minutes of
        audio is 40 minutes of ffmpeg — and the file belongs to whoever passed
        it, so this job neither creates nor removes it.
        """
        already = tmp_path / "already-converted.wav"
        already.write_bytes(b"RIFF")
        prepared = audio_prep.PreparedAudio(path=str(already), duration_seconds=12.0)
        backend = _FakeBackend(result=_result())
        watcher = _Watcher()

        job = job_module.TranscriptionJob(
            audio_path=_voice_note(tmp_path),
            # Would raise FFMPEG_FAILED if the conversion were attempted, which
            # is the point: nothing may run ffmpeg on this path.
            ffmpeg=str(tmp_path / "must-not-run.exe"),
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            backend=backend,
            prepared=prepared,
            on_phase=watcher.on_phase,
            on_finished=watcher.on_finished,
            probe=lambda: device.HardwareProbe(total_ram_mb=16384, available_ram_mb=8192),
        )
        job.start()
        job.join(30)

        assert watcher.phases == [
            job_module.PHASE_LOADING_MODEL,
            job_module.PHASE_TRANSCRIBING,
            job_module.PHASE_DONE,
        ]
        assert backend.requests[0].audio_path == str(already)
        assert backend.requests[0].duration_seconds == 12.0
        assert already.exists(), "the job deleted a file it does not own"

    def test_a_gpu_failure_worth_redoing_hands_the_audio_over(
        self, tmp_path, own_temp_dir
    ):
        """The converted audio outlives the run that failed on the GPU.

        Re-converting is the whole wait a second time — 40 minutes of audio is
        40 minutes of ffmpeg — for a file that is already correct. So the job
        hands it to the caller instead of deleting it, and from that point the
        caller is the one who deletes it.
        """
        backend = _FakeBackend(
            error=errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "alloc failed")
        )
        job, watcher = _run_job(
            tmp_path, backend,
            probe=lambda: device.HardwareProbe(
                cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
                free_vram_mb=8192, total_vram_mb=8192,
            ),
        )

        assert job.device == device.DEVICE_CUDA
        assert watcher.finished[0][1].code == errors.INSUFFICIENT_VRAM
        assert job.prepared_handover is not None
        assert os.path.isfile(job.prepared_handover.path)
        assert job.prepared_handover.duration_seconds > 0
        # Still in the temp directory, on purpose: it is a leak only until the
        # caller discards it, which is the other half of the contract.
        assert _leftovers(own_temp_dir) != []
        audio_prep.discard(job.prepared_handover)
        assert _leftovers(own_temp_dir) == []

    def test_the_handed_over_file_transcribes_without_ffmpeg_running_again(
        self, tmp_path, own_temp_dir
    ):
        """The point of the handover: the CPU run reuses the same audio."""
        failing = _FakeBackend(
            error=errors.TranscriptionError(errors.CUDA_UNAVAILABLE, "no cublas")
        )
        gpu_job, _watcher = _run_job(
            tmp_path, failing,
            probe=lambda: device.HardwareProbe(
                cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
                free_vram_mb=8192, total_vram_mb=8192,
            ),
        )
        prepared = gpu_job.prepared_handover
        assert prepared is not None

        second = _FakeBackend(result=_result())
        cpu_job, cpu_watcher = _run_job(
            tmp_path, second,
            # Would raise FFMPEG_FAILED if the conversion were attempted, which
            # is exactly what must not happen a second time.
            ffmpeg=str(tmp_path / "must-not-run.exe"),
            prepared=prepared,
            probe=lambda: device.HardwareProbe(total_ram_mb=16384,
                                               available_ram_mb=8192),
        )

        assert cpu_watcher.finished[0][1] is None
        assert cpu_job.device == device.DEVICE_CPU
        assert second.requests[0].audio_path == prepared.path
        # The second job did not take ownership either — it never created the
        # file — so the caller is still the one holding it.
        assert cpu_job.prepared_handover is None
        assert os.path.isfile(prepared.path)
        audio_prep.discard(prepared)
        assert _leftovers(own_temp_dir) == []

    @pytest.mark.parametrize("code", [
        errors.CANCELLED,
        errors.MODEL_NOT_INSTALLED,
        errors.MODEL_CORRUPTED,
        errors.BACKEND_ERROR,
    ])
    def test_a_failure_the_cpu_cannot_cure_deletes_the_audio_as_before(
        self, tmp_path, own_temp_dir, code
    ):
        # The path that hands nothing over is the path that used to exist, and
        # it must keep cleaning up after itself.
        backend = _FakeBackend(error=errors.TranscriptionError(code, "detail"))
        job, _watcher = _run_job(
            tmp_path, backend,
            probe=lambda: device.HardwareProbe(
                cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
                free_vram_mb=8192, total_vram_mb=8192,
            ),
        )
        assert job.prepared_handover is None
        assert _leftovers(own_temp_dir) == []

    def test_a_handover_with_nobody_to_hand_it_to_is_not_made(
        self, tmp_path, own_temp_dir
    ):
        """A job with no finished callback has no caller to become the owner.

        The transfer presumes a reader. Without one, nothing would ever learn
        the file exists, and a WAV of the whole recording would sit in %TEMP%
        unowned for good — a leak that grows by one recording per failure.
        """
        backend = _FakeBackend(
            error=errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "alloc failed")
        )
        job = job_module.TranscriptionJob(
            audio_path=_voice_note(tmp_path),
            ffmpeg=_fake_ffmpeg(tmp_path, seconds=1.0),
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            backend=backend,
            on_finished=None,
            probe=lambda: device.HardwareProbe(
                cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
                free_vram_mb=8192, total_vram_mb=8192,
            ),
        )
        job.start()
        job.join(30)

        assert job.phase == job_module.PHASE_FAILED
        assert job.device == device.DEVICE_CUDA
        assert job.prepared_handover is None
        assert _leftovers(own_temp_dir) == []

    def test_the_re_run_needs_the_preference_and_not_just_the_audio(
        self, tmp_path, own_temp_dir
    ):
        """Handing the audio in does not imply the processor — the flag does.

        After INSUFFICIENT_VRAM the card is still counted and its libraries
        still load, so a re-run built without PREFERENCE_CPU re-decides its way
        straight back to CUDA and fails identically, having paid for the model
        load a second time. The probe here says CUDA precisely so that only the
        preference can be what moves it.
        """
        prepared = audio_prep.PreparedAudio(
            path=str(tmp_path / "already-converted.wav"), duration_seconds=12.0
        )
        (tmp_path / "already-converted.wav").write_bytes(b"RIFF")
        cuda_probe = lambda: device.HardwareProbe(  # noqa: E731 - one line, one use
            cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
            free_vram_mb=8192, total_vram_mb=8192,
        )

        went_back, _watcher = _run_job(
            tmp_path, _FakeBackend(result=_result()),
            ffmpeg=str(tmp_path / "must-not-run.exe"),
            prepared=prepared, probe=cuda_probe,
        )
        assert went_back.device == device.DEVICE_CUDA, (
            "the audio alone was enough to move the run off the GPU"
        )

        forced, _watcher = _run_job(
            tmp_path, _FakeBackend(result=_result()),
            ffmpeg=str(tmp_path / "must-not-run.exe"),
            prepared=prepared, probe=cuda_probe,
            device_preference=device.PREFERENCE_CPU,
        )
        assert forced.device == device.DEVICE_CPU
        # Part 6 announces this one: a forced re-run says "you asked for the
        # processor", which this user did not — that is the announcement to
        # suppress on the retry path.
        assert forced.device_reason == device.REASON_CPU_REQUESTED

    def test_a_gpu_failure_on_a_run_that_was_on_the_cpu_hands_nothing_over(
        self, tmp_path, own_temp_dir
    ):
        # Same error code, CPU run: there is nothing to fall back to, so
        # keeping the file would leak it for an offer nobody can make.
        backend = _FakeBackend(
            error=errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "alloc failed")
        )
        job, _watcher = _run_job(tmp_path, backend)
        assert job.device == device.DEVICE_CPU
        assert job.prepared_handover is None
        assert _leftovers(own_temp_dir) == []

    def test_a_successful_run_hands_nothing_over(self, tmp_path, own_temp_dir):
        job, watcher = _run_job(tmp_path, _FakeBackend(result=_result()))
        assert watcher.finished[0][1] is None
        assert job.prepared_handover is None
        assert _leftovers(own_temp_dir) == []

    def test_the_handover_is_readable_from_the_finished_callback(
        self, tmp_path, own_temp_dir
    ):
        """Part 6 reads it from there, so it has to be set before the report.

        The callback is where the offer to re-run is decided, and the finished
        report is the only thing the UI is waiting on — a handover published
        after it would be invisible.
        """
        seen = {}
        backend = _FakeBackend(
            error=errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "alloc failed")
        )
        holder = {}

        def _on_finished(result, error):
            seen["handover"] = holder["job"].prepared_handover
            seen["offer"] = device.cpu_retry_i18n_key(error, holder["job"].device)

        job = job_module.TranscriptionJob(
            audio_path=_voice_note(tmp_path),
            ffmpeg=_fake_ffmpeg(tmp_path, seconds=1.0),
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            backend=backend,
            on_finished=_on_finished,
            probe=lambda: device.HardwareProbe(
                cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
                free_vram_mb=8192, total_vram_mb=8192,
            ),
        )
        holder["job"] = job
        job.start()
        job.join(30)

        assert seen["handover"] is not None
        assert seen["offer"] == "transcription_retry_on_cpu_vram"
        audio_prep.discard(seen["handover"])
        assert _leftovers(own_temp_dir) == []

    def test_the_configured_backend_is_resolved_when_none_is_handed_in(
        self, tmp_path, own_temp_dir, monkeypatch
    ):
        backend = _FakeBackend(result=_result())
        monkeypatch.setitem(backend_module._instances, "fake", backend)
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("fake",))
        _job, watcher = _run_job(tmp_path, None, backend_id="fake")
        assert watcher.finished[0][1] is None

    def test_no_usable_backend_fails_the_job_rather_than_the_thread(
        self, tmp_path, own_temp_dir, monkeypatch
    ):
        monkeypatch.setitem(
            backend_module._instances, "fake", _FakeBackend(available=False)
        )
        monkeypatch.setattr(backend_module, "BACKEND_IDS", ("fake",))
        _job, watcher = _run_job(tmp_path, None, backend_id="fake")
        assert watcher.finished[0][1].code == errors.BACKEND_MISSING
        assert _leftovers(own_temp_dir) == []


# ── Privacy of the log ───────────────────────────────────────────────────────


# What the issue forbids the log to carry, as identifiers a logging argument
# might name. `text`/`segments` are the transcription itself; the paths are the
# audio file, whose name is the WhatsApp message id and therefore identifies
# the message and the conversation behind it.
_FORBIDDEN_IN_LOG = (
    r"\btext\b", r"\.text\b", r"\bsegments\b", r"\baudio_path\b",
    r"\bsource_path\b", r"\bpath\b", r"\bjid\b", r"\bmessage_id\b",
    r"\bcontact\b", r"\bphone\b",
)

# Part 3's own modules. Restricted to them deliberately: the model store logs
# the models directory the user chose, which is neither a message nor a
# conversation, and rewriting its rules is not this part's business.
#
# management.py joins them: it logs the outcome of every action the settings
# tab runs, and nothing it logs may ever reach past the model id. So does part
# 6b's message_run.py, the one module here that holds a message record — its
# stricter scan (and the UI modules') is in test_transcription_message_run.py.
_PART3_MODULES = (
    "audio_prep.py", "backend.py", "faster_whisper_backend.py", "job.py",
    "management.py", "message_run.py",
    # Part 9a's backend runs a program that prints the transcription, and its
    # helpers read that program's output: held to the same rule.
    "whisper_cpp_backend.py", "whisper_cpp_cli.py",
)


def _logging_arguments(path):
    """Every expression handed to a `logging.*` call in one module."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "logging"):
            continue
        for argument in list(node.args) + [kw.value for kw in node.keywords]:
            found.append((target.attr, ast.unparse(argument)))
    return found


class TestLogPrivacy:
    """The issue's own requirement, enforced rather than remembered.

    A transcription is the content of a private conversation. The log may say
    how the machine did the work — backend, model, device, compute type,
    languages, durations, the technical text of a failure — and nothing about
    what was said or who said it. That includes the audio path: WinZapp names
    media files after the message id.
    """

    def test_no_logging_call_can_be_handed_the_transcribed_text(self):
        package = os.path.dirname(job_module.__file__)
        offenders = []
        for name in _PART3_MODULES:
            for level, expression in _logging_arguments(os.path.join(package, name)):
                for pattern in _FORBIDDEN_IN_LOG:
                    if re.search(pattern, expression):
                        offenders.append(f"{name}: logging.{level}({expression})")
        assert offenders == [], f"the log could carry private content: {offenders}"

    def test_a_whole_run_never_writes_what_was_said(
        self, tmp_path, own_temp_dir, caplog
    ):
        """Belt and braces: the static check cannot see through a helper."""
        spoken = "zzqq um segredo que ninguem deveria ver no log yyww"
        source = _voice_note(tmp_path, name="ABCDEF0123456789.wzmedia")
        caplog.set_level(logging.DEBUG)

        backend = _FakeBackend(result=_result(spoken))
        _run_job(tmp_path, backend, audio_path=source)

        for word in ("zzqq", "segredo", "yyww"):
            assert word not in caplog.text
        assert os.path.basename(source) not in caplog.text
        assert source not in caplog.text

    def test_a_failing_conversion_never_writes_the_file_name_either(
        self, tmp_path, own_temp_dir, caplog
    ):
        source = _voice_note(tmp_path, name="FEDCBA9876543210.wzmedia")
        ffmpeg = _fake_ffmpeg(
            tmp_path, returncode=1, seconds=-1,
            stderr=(
                f"Input #0, ogg, from '{source}':\n"
                f"{source}: Invalid data found when processing input\n"
            ),
        )
        caplog.set_level(logging.DEBUG)

        _job, watcher = _run_job(
            tmp_path, _FakeBackend(result=_result()), ffmpeg=ffmpeg, audio_path=source
        )

        assert watcher.finished[0][1].code == errors.UNSUPPORTED_AUDIO_FORMAT
        assert os.path.basename(source) not in caplog.text
        assert source not in caplog.text

    # A path the way an OSError about the media file prints it: the file is
    # named after the message id, and the id is what may never reach the log.
    # The folder around it is left alone on purpose (errors.scrub_media_names()),
    # so these look for the id and nothing else — never for a folder name like
    # "Users", which the frames of any report carry too, wherever the
    # repository happens to be checked out.
    _LEAKY_ID = "3EB0FEEDFACE00998877"
    _LEAKY_PATH = r"C:\Users\Ana Souza\AppData\Local\WinZapp\media\3EB0FEEDFACE00998877.wzmedia"

    @pytest.mark.parametrize("make", [
        lambda path: OSError(22, "Invalid argument", path),
        lambda path: RuntimeError(f"Unable to open file '{path}'"),
        lambda path: ValueError(f"cannot read {path}"),
    ], ids=["oserror", "quoted", "bare"])
    def test_an_unexpected_exception_keeps_its_wording_and_loses_the_message_id(
        self, make, tmp_path, own_temp_dir, caplog
    ):
        """The job's catch-all used to put `{exc}` in the detail and
        logging.exception() in the log, and the text of an exception about a
        media file carries that file's name: the message id."""
        caplog.set_level(logging.DEBUG)
        backend = _FakeBackend(error=make(self._LEAKY_PATH))
        _job, watcher = _run_job(tmp_path, backend)

        error = watcher.finished[0][1]
        assert error.code == errors.BACKEND_ERROR
        for text in (error.log_line, caplog.text):
            assert self._LEAKY_ID not in text
        # Still a diagnosis: the type, the folder the file was in, and the
        # frames of our own code.
        assert type(backend._error).__name__ in error.log_line
        assert "<message id>.wzmedia" in error.log_line
        assert "AppData" in error.log_line
        assert "job.py" in caplog.text

    def test_an_ffmpeg_that_will_not_die_logs_no_message_id(self, caplog):
        """TimeoutExpired prints the whole command, and the command holds the
        media file's path — which exc_info=True used to write out whole."""
        import subprocess

        path = self._LEAKY_PATH

        class _Stuck:
            def kill(self):
                pass

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(["ffmpeg.exe", "-i", path], timeout)

        caplog.set_level(logging.DEBUG)
        _fileops.kill_process(_Stuck(), "ffmpeg")
        assert "could not stop ffmpeg: TimeoutExpired" in caplog.text
        assert self._LEAKY_ID not in caplog.text

    def test_a_callback_that_raises_logs_no_message_id_either(
        self, tmp_path, own_temp_dir, caplog
    ):
        caplog.set_level(logging.DEBUG)
        watcher = _Watcher()

        def _bad_phase(phase):
            watcher.phases.append(phase)
            raise OSError(2, "No such file or directory", self._LEAKY_PATH)

        watcher.on_phase = _bad_phase
        _run_job(tmp_path, _FakeBackend(result=_result()), watcher=watcher)
        assert "a job callback raised: FileNotFoundError errno=2" in caplog.text
        assert self._LEAKY_ID not in caplog.text


class TestScrubMediaNames:
    """errors.scrub_media_names(): the media file name goes, nothing else does.

    The file is named after the WhatsApp message id, which the issue forbids
    the log to hold. Folders, error codes and DLL or model names are the
    diagnosis, and a cleaner that took them — as the first version did, on
    the idea that a profile folder was private — left log.log unable to say
    which DLL failed to load or which model failed to open.
    """

    _ID = "3EB0C0FFEE00112233"

    @pytest.mark.parametrize("text, kept", [
        (str(OSError(2, "No such file or directory",
                     r"C:\Users\Ana Souza\AppData\Local\WinZapp\voice_messages\3EB0C0FFEE00112233.msv")),
         r"[Errno 2] No such file or directory: 'C:\\Users\\Ana Souza\\AppData\\Local"
         r"\\WinZapp\\voice_messages\\<message id>.msv'"),
        (r"cannot read D:\WinZapp\media\3EB0C0FFEE00112233.wzmedia",
         r"cannot read D:\WinZapp\media\<message id>.wzmedia"),
        (r"C:\m\3EB0C0FFEE00112233.wzmedia: Invalid data found when processing input",
         r"C:\m\<message id>.wzmedia: Invalid data found when processing input"),
        ("3EB0C0FFEE00112233.wzmedia is bad", "<message id>.wzmedia is bad"),
        ("3EB0C0FFEE00112233.MSV", "<message id>.MSV"),
        # A sentence's own full stop still ends the name.
        ("failed on 3EB0C0FFEE00112233.msv.", "failed on <message id>.msv."),
        # An own voice note not yet sent is named after its local uuid4.
        ("b3c1e0de-8f5a-4c2b-9d7e-0a1b2c3d4e5f.msv", "<message id>.msv"),
    ])
    def test_the_media_name_goes_and_the_rest_of_the_line_stays(self, text, kept):
        assert errors.scrub_media_names(text) == kept
        assert self._ID not in errors.scrub_media_names(text)

    @pytest.mark.parametrize("text", [
        # The four lines the path cleaner used to ruin, as measured.
        r"Could not load library C:\Users\Ana\AppData\Local\WinZapp\cuda\cudnn_ops64_9.dll. "
        r"Error code 126",
        r"Expected D:\x\y, got E:\z",
        r"C:\Users\Ana\AppData\Local\WinZapp\cuda\cublas64_12.dll: [WinError 32] The process "
        r"cannot access the file because it is being used by another process: "
        r"'C:\Users\Ana\AppData\Local\WinZapp\cuda\cublas64_12.dll'",
        r"Unable to open file 'model.bin' in model "
        r"'C:\Users\Ana Souza\AppData\Local\WinZapp\whisper_models\large-v3'",
        r"share \\srv\ana.souza\tmp is gone",
        "CUDA failed with error out of memory",
        "Max retries exceeded with url: /org/repo/resolve/0123abcd/model.bin",
        "model.bin sha256 " + "a" * 64 + ", expected " + "b" * 64,
        "rc=1: Error writing trailer: No space left on device",
        # Only a name that ends in the suffix is a media file.
        r"C:\backup\sub.msv.bak",
        "vcvars.msvc",
    ])
    def test_everything_else_is_left_exactly_as_it_was(self, text):
        assert errors.scrub_media_names(text) == text

    def test_the_dll_and_its_error_code_survive(self):
        """126 is a missing dependency, 193 the wrong architecture: the code is
        the diagnosis of a CUDA DLL conflict, and the name says which DLL."""
        line = errors.scrub_media_names(
            r"Could not load library C:\Program Files\x\cudnn_ops64_9.dll. Error code 126")
        assert "cudnn_ops64_9.dll" in line and "Error code 126" in line

    def test_the_model_tried_survives(self):
        exc = errors.TranscriptionError(
            errors.BACKEND_ERROR,
            r"RuntimeError: Unable to open file 'model.bin' in model "
            r"'C:\Users\Ana\AppData\Local\WinZapp\whisper_models\large-v3'")
        assert r"whisper_models\large-v3" in exc.log_line

    def test_a_dll_held_by_the_antivirus_is_named(self, tmp_path):
        """cuda_runtime._hash_file()'s detail is `f"{path}: {exc}"`: which DLL
        could not be read is the only useful half of it."""
        missing = tmp_path / "cublas64_12.dll"
        with pytest.raises(errors.TranscriptionError) as caught:
            cuda_runtime._hash_file(str(missing), 0, 1, None, None)
        assert caught.value.code == errors.CUDA_RUNTIME_CORRUPTED
        assert str(missing) in caught.value.log_line
        assert "Errno 2" in caught.value.log_line

    def test_the_detail_is_cleaned_wherever_it_is_raised(self):
        exc = errors.TranscriptionError(
            errors.BACKEND_ERROR, r"cannot read C:\Users\Ana\voice_messages\3EB0C0FFEE00112233.msv")
        assert exc.detail == r"cannot read C:\Users\Ana\voice_messages\<message id>.msv"
        assert self._ID not in exc.log_line
        assert str(exc) == errors.BACKEND_ERROR


class TestExceptionReport:
    """errors.exception_report(): logging.exception()'s chain, minus the id."""

    _PATH = r"C:\Users\Ana\AppData\Local\WinZapp\media\3EB0C0FFEE00112233.wzmedia"

    def test_a_cause_is_reported_with_its_errno(self):
        """`RuntimeError(...) from OSError(2, ...)` without the OSError would
        have lost the only line that says what actually failed."""
        try:
            try:
                raise OSError(2, "No such file or directory", self._PATH)
            except OSError as inner:
                raise RuntimeError("model load failed") from inner
        except RuntimeError as exc:
            report = errors.exception_report(exc)

        assert "FileNotFoundError errno=2" in report
        assert "The above exception was the direct cause of the following exception" in report
        assert "RuntimeError: model load failed" in report
        # Oldest first, as traceback prints it.
        assert report.index("FileNotFoundError") < report.index("RuntimeError")
        assert "3EB0C0FFEE00112233" not in report
        assert "<message id>.wzmedia" in report

    def test_an_exception_raised_while_handling_another_reports_both(self):
        try:
            try:
                raise OSError(28, "No space left on device")
            except OSError:
                raise ValueError("cleanup failed")
        except ValueError as exc:
            report = errors.exception_report(exc)

        assert "OSError errno=28" in report
        assert "During handling of the above exception" in report
        assert "ValueError: cleanup failed" in report

    def test_from_none_keeps_the_original_out(self):
        """message_audio.py raises `from None` so the exception carrying the
        media path never travels; the report must respect that exactly as
        traceback does, even though it still sits in __context__."""
        try:
            try:
                raise OSError(5, "Access denied", self._PATH)
            except OSError:
                raise errors.TranscriptionError(errors.MEDIA_NOT_DOWNLOADED) from None
        except errors.TranscriptionError as exc:
            assert exc.__context__ is not None
            report = errors.exception_report(exc)

        assert "TranscriptionError: media_not_downloaded" in report
        assert "OSError" not in report and "Access denied" not in report
        assert "3EB0C0FFEE00112233" not in report
        assert "direct cause" not in report and "During handling" not in report

    def test_a_transcription_error_cause_keeps_its_detail(self):
        """str() of a TranscriptionError is the code alone, for the UI; as a
        link in the chain it is read through log_line, or the detail it was
        raised with — the one line saying what went wrong — never shows."""
        try:
            try:
                raise errors.TranscriptionError(errors.BACKEND_ERROR, f"decoder died on {self._PATH}")
            except errors.TranscriptionError as inner:
                raise RuntimeError("unexpected") from inner
        except RuntimeError as exc:
            report = errors.exception_report(exc)

        assert "TranscriptionError: backend_error: decoder died on" in report
        assert "<message id>.wzmedia" in report
        assert "3EB0C0FFEE00112233" not in report

    def test_a_transcription_error_context_keeps_its_detail(self):
        try:
            try:
                raise errors.TranscriptionError(errors.BACKEND_ERROR, f"decoder died on {self._PATH}")
            except errors.TranscriptionError:
                raise ValueError("cleanup failed")
        except ValueError as exc:
            report = errors.exception_report(exc)

        assert "During handling of the above exception" in report
        assert "TranscriptionError: backend_error: decoder died on" in report
        assert "3EB0C0FFEE00112233" not in report

    def test_a_single_exception_reads_as_before(self):
        report = errors.exception_report(PermissionError(13, "Permission denied"))
        assert report == "PermissionError errno=13: [Errno 13] Permission denied"


# ── Every module of the issue: nothing that names a message ──────────────────
#
# The stricter scans (here for part 3, in test_transcription_message_run.py for
# the modules that hold a message) forbid names like `id` outright, which the
# model store and the CUDA runtime cannot live with: a model id is exactly the
# technical detail their log is for. What every module shares is narrower and
# absolute — nothing that names a message (its id, its media file, its text,
# its contact or number), and no exception whose text has not been through
# errors.scrub_media_names(). Folders are not on the list: they are the
# diagnosis of a model or CUDA failure, and they name no message.

def _issue_modules():
    package = os.path.dirname(job_module.__file__)
    client = os.path.dirname(os.path.dirname(package))
    modules = sorted(
        os.path.join(package, name) for name in os.listdir(package) if name.endswith(".py")
    )
    modules += [
        os.path.join(client, "ui", "transcription_flow.py"),
        os.path.join(client, "ui", "dialogs", "transcription_progress.py"),
        os.path.join(client, "ui", "dialogs", "transcription_result.py"),
    ]
    return modules


_LOG_LEVELS = frozenset(
    ("debug", "info", "warning", "warn", "error", "exception", "critical", "log"))

# Names an exception is routinely held by even outside an `except ... as`,
# e.g. a finished report's `error` parameter.
_EXCEPTION_NAMES = frozenset(("exc", "err", "error", "e"))

# The readings of an exception that may reach the log: each either cleans the
# media name out of the text or reads only a number, a code or a class.
_SAFE_EXCEPTION_READINGS = (
    r"errors\.exception_report\({0}\)",
    r"errors\.scrub_media_names\(str\({0}\)\)",
    r"type\({0}\)\.__name__",
    r"{0}\.(?:errno|log_line|code)\b",
    r"getattr\({0}, 'winerror', None\)",
    r"{0} is (?:not )?None",
    r"traceback\.format_tb\({0}\.__traceback__\)",
)

# The fields of a finished result that describe the run, not the recording.
_SAFE_RESULT_READINGS = re.compile(
    r"getattr\(result, '(?:backend|duration_seconds|language)', None\)"
    r"|result\.(?:backend|duration_seconds|language|is_empty)\b"
    # How long something is says nothing about what it holds.
    r"|len\([^()]*\)"
)

# Names that carry a message: its id or record, its media file (named after
# the id), the transcribed text, the contact and their number.
_NAMES_A_MESSAGE = (
    r"\bmsg\b", r"\bmsgs\b", r"\bmessage\b", r"\brecord\b", r"\bmsg_id\b",
    r"\bmessage_id\b", r"\breal_id\b", r"\blocal_id\b", r"\bclean_id\b",
    r"\bmedia_file\b", r"\bmedia_path\b", r"\baudio_path\b", r"\bsource_path\b",
    r"\bfile_?names?\b",
    r"\btext\b", r"\.text\b", r"\bsegments?\b", r"\bresult\b", r"\btranscript\w*",
    r"\bjid\b", r"_jid\b", r"\bcontact\w*", r"\bphone\w*", r"\bnumber\b",
    r"\bsender\w*", r"\bpush_?name\b", r"\bchat_name\b", r"\btitle\b",
)


def _exception_names(tree):
    """Every name an `except ... as` binds in `tree`, plus the usual ones."""
    names = set(_EXCEPTION_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _is_log_call(node):
    target = node.func
    return (isinstance(target, ast.Attribute)
            and target.attr in _LOG_LEVELS
            and isinstance(target.value, ast.Name)
            and "log" in target.value.id.lower())


def _unsafe_logging(source, label):
    """Every logging call in `source` that could print something of a message."""
    tree = ast.parse(source)
    exception_names = _exception_names(tree)
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_log_call(node)):
            continue
        if node.func.attr == "exception":
            # Its lines are str(exc) verbatim: errors.exception_report().
            offenders.append(f"{label}: logging.exception at line {node.lineno}")
            continue
        for keyword in node.keywords:
            if keyword.arg == "exc_info":
                offenders.append(f"{label}: exc_info at line {node.lineno}")
        for argument in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(argument, ast.Constant):
                continue
            expression = ast.unparse(argument)
            for name in exception_names:
                for reading in _SAFE_EXCEPTION_READINGS:
                    expression = re.sub(reading.format(re.escape(name)), "", expression)
            expression = _SAFE_RESULT_READINGS.sub("", expression)
            raw_exception = any(re.search(rf"\b{re.escape(name)}\b", expression)
                                for name in exception_names)
            names_a_message = any(re.search(p, expression) for p in _NAMES_A_MESSAGE)
            if raw_exception or names_a_message:
                offenders.append(f"{label}:{node.lineno}: {ast.unparse(argument)}")
    return offenders


def test_no_module_of_the_issue_logs_a_message_or_a_raw_exception():
    offenders = []
    for path in _issue_modules():
        with open(path, "r", encoding="utf-8") as handle:
            offenders += _unsafe_logging(handle.read(), os.path.basename(path))
    assert offenders == [], offenders


def test_the_issue_wide_scan_covers_every_module_it_should():
    names = {os.path.basename(path) for path in _issue_modules()}
    for expected in ("model_store.py", "cuda_runtime.py", "device.py", "management.py",
                     "narration.py", "preferences.py", "stored.py", "message_audio.py",
                     "transcription_flow.py", "transcription_progress.py",
                     "transcription_result.py"):
        assert expected in names
    # The app-wide TLS module logs no message and is not the issue's code.
    assert "tls_trust.py" not in names


@pytest.mark.parametrize("bad", [
    # Any exception handed over raw, whatever it is called.
    'try:\n    pass\nexcept OSError as exc:\n    logging.info("x %s", exc)\n',
    'try:\n    pass\nexcept OSError as e:\n    logging.info("%s", e)\n',
    'try:\n    pass\nexcept OSError as failure:\n    logging.warning("x %s", failure)\n',
    'try:\n    pass\nexcept OSError as exc:\n    logging.info("x %s", str(exc))\n',
    'try:\n    pass\nexcept OSError as exc:\n    logger.info("x %s", exc)\n',
    'try:\n    pass\nexcept OSError as err:\n    logging.error("x %s", errors.exception_report(exc))\n    log.info("%s", err)\n',
    'def f(error):\n    logging.info("x %s", error)\n',
    'def f():\n    logging.exception("x")\n',
    'def f():\n    logging.warning("x", exc_info=True)\n',
    # Anything that names the message.
    'def f(msg_id):\n    logging.info("at %s", msg_id)\n',
    'def f(msg):\n    logger.info("for %s", msg)\n',
    'def f(audio_path):\n    logging.info("at %s", audio_path)\n',
    'def f(filename):\n    logging.info("at %s", filename)\n',
    'def f(result):\n    logging.info("got %s", result.text)\n',
    'def f(result):\n    logging.info("got %s", result)\n',
    'def f(jid):\n    logging.info("chat %s", jid)\n',
    'def f(contact_name):\n    logging.info("from %s", contact_name)\n',
    'def f(phone):\n    logging.info("from %s", phone)\n',
])
def test_the_issue_wide_scan_would_see_it(bad):
    """The scan passing proves nothing unless it can fail."""
    assert _unsafe_logging(bad, "probe")


@pytest.mark.parametrize("good", [
    'try:\n    pass\nexcept OSError as exc:\n    logging.info("x %s", errors.scrub_media_names(str(exc)))\n',
    'try:\n    pass\nexcept OSError as err:\n    logging.error("x %s", errors.exception_report(err))\n',
    'try:\n    pass\nexcept OSError as exc:\n    logging.info("%s %s", type(exc).__name__, exc.errno)\n',
    # Folders are the diagnosis, not a message.
    'def f(model, directory):\n    logging.info("%s at %s", model.id, directory)\n',
    'def f(new_root, model_dir, part_path):\n    logging.info("%s %s %s", new_root, model_dir, part_path)\n',
    # How much, never what.
    'def f(text, n):\n    logging.info("%d chars, %s", len(text), str(n))\n',
    'def f(result):\n    logging.info("%s %s", result.language, getattr(result, \'backend\', None))\n',
])
def test_the_issue_wide_scan_lets_the_legitimate_forms_through(good):
    assert _unsafe_logging(good, "probe") == []


def test_nothing_here_reaches_for_the_hugging_face_cache():
    """The weights come from the folder the model store filled, and only there.

    A frozen WinZapp has no HF_HOME, and even where one exists it is not what
    the user chose in the settings or what the store verified. Anything reading
    it would work on the developer's machine and download, silently, on the
    user's.
    """
    package = os.path.dirname(job_module.__file__)
    offenders = []
    for name in _PART3_MODULES:
        with open(os.path.join(package, name), "r", encoding="utf-8") as handle:
            source = handle.read()
        for token in ("HF_HOME", "HUGGINGFACE", "TRANSFORMERS_CACHE", "download_root"):
            if token in source:
                offenders.append(f"{name}: {token}")
    assert offenders == []


def test_the_build_ships_what_a_local_transcription_needs():
    """A frozen WinZapp has no pip: the backend is either in the build or gone.

    faster-whisper reaches ctranslate2, av, tokenizers and huggingface_hub
    through imports PyInstaller's static analysis does not follow, and the
    voice-activity filter's own model is a data file inside the package —
    `--collect-all` is what brings each package's binaries and data along with
    its modules.

    onnxruntime is collected the same way even though a hidden import would be
    enough on paper (contrib ships a hook for its provider DLLs): if it fails to
    import in a frozen build only, the filter falls back silently and Whisper
    starts inventing sentences in a voice note's trailing silence, which the
    person this feature is for cannot detect. See the comment in build.py.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "build.py"), "r", encoding="utf-8") as handle:
        source = handle.read()
    collect_all = source.split("collect_all = [", 1)[1].split("]", 1)[0]
    for package in ("faster_whisper", "ctranslate2", "av", "tokenizers",
                    "huggingface_hub", "truststore"):
        assert f'"{package}"' in collect_all, f"{package} is not collected by build.py"
    assert '"--collect-all", "onnxruntime"' in source


def test_the_requirements_pin_the_backend():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "requirements.txt"), "r", encoding="utf-8") as handle:
        requirements = handle.read()
    for line in ("faster-whisper==", "ctranslate2==", "av==", "onnxruntime==",
                 "tokenizers==", "truststore=="):
        assert line in requirements


class _ResidencySpy:
    def __init__(self):
        self.events = []

    def using(self, backend):
        spy = self

        class _Use:
            def __enter__(self_inner):
                spy.events.append("begin")

            def __exit__(self_inner, *exc_info):
                spy.events.append("end")
                return False

        return _Use()


class TestTheJobTellsTheResidencyManager:
    """The idle clock of a loaded model (core/transcription/residency.py)
    starts when the run ends, whatever way it ends."""

    def test_load_and_transcribe_are_inside_one_use(self, tmp_path, own_temp_dir, monkeypatch):
        spy = _ResidencySpy()
        monkeypatch.setattr(job_module.residency_module, "residency", spy)
        backend = _FakeBackend(
            result=_result(),
            on_load=lambda request: spy.events.append("load"),
            on_transcribe=lambda request: spy.events.append("transcribe"),
        )
        _run_job(tmp_path, backend)
        assert spy.events == ["begin", "load", "transcribe", "end"]

    def test_a_failed_run_ends_the_use_too(self, tmp_path, own_temp_dir, monkeypatch):
        spy = _ResidencySpy()
        monkeypatch.setattr(job_module.residency_module, "residency", spy)
        backend = _FakeBackend(error=errors.TranscriptionError(errors.BACKEND_ERROR, "x"))
        _run_job(tmp_path, backend)
        assert spy.events == ["begin", "end"]
