"""A successful absolute-path probe must survive CTranslate2's bare-name load.

AddDllDirectory does not by itself fix legacy LoadLibrary in a frozen app.
After that first native failure, CTranslate2 4.8.2 can hang on the next encode,
even with a replacement model. Prepare the DLLs before entering the engine.
The Windows regression uses a renamed Windows DLL, so CI needs no CUDA/GPU.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from core.transcription import backend, device, errors, faster_whisper_backend


class TestPreparingCuda:
    @pytest.fixture(autouse=True)
    def isolated_loader(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(device, "_prepared_cuda_libraries", {})
        monkeypatch.setattr(device, "_extra_cuda_library_dirs", {})
        self.references = 0
        self.loads = []

        def load(name, winmode=None):
            self.loads.append((name, winmode))
            if not os.path.dirname(name) and not self.references:
                raise OSError("not on the legacy search path")
            self.references += 1
            return SimpleNamespace(_handle=123)

        def release(library):
            self.references -= 1

        monkeypatch.setattr(device.ctypes, "WinDLL", load, raising=False)
        monkeypatch.setattr(device, "_release_library", release)
        monkeypatch.setattr(device, "cuda_library_directories", lambda: ("runtime",))

    def test_absolute_load_is_kept_for_the_engines_legacy_lookup(self):
        assert device.prepare_cuda_libraries() == (True, (), None)
        assert self.references == 1
        assert "cublas64_12.dll" in device._prepared_cuda_libraries
        # The last load validates the same bare-name lookup as CTranslate2.
        assert self.loads[-1] == ("cublas64_12.dll", 0)

    def test_repeated_runs_and_probe_invalidation_do_not_reload_or_unload(self):
        device.prepare_cuda_libraries()
        loads = list(self.loads)
        device.forget_cuda_library_answer()
        assert device.prepare_cuda_libraries() == (True, (), None)
        assert self.loads == loads
        assert self.references == 1

    def test_missing_libraries_never_become_prepared(self, monkeypatch):
        def missing(*args, **kwargs):
            raise OSError("missing dependency")

        monkeypatch.setattr(device.ctypes, "WinDLL", missing)
        ok, missing_names, detail = device.prepare_cuda_libraries()
        assert ok is False
        assert missing_names == ("cublas64_12.dll",)
        assert "missing dependency" in detail
        assert device._prepared_cuda_libraries == {}

    def test_a_failed_preparation_can_be_retried_after_installing(self, monkeypatch):
        monkeypatch.setattr(device, "cuda_library_directories", lambda: ())
        assert device.prepare_cuda_libraries()[0] is False
        monkeypatch.setattr(device, "cuda_library_directories", lambda: ("runtime",))
        assert device.prepare_cuda_libraries()[0] is True

    def test_absolute_success_without_legacy_success_is_rejected(self, monkeypatch):
        def only_absolute(name, winmode=None):
            self.loads.append((name, winmode))
            if not os.path.dirname(name):
                raise OSError("legacy lookup still fails")
            self.references += 1
            return SimpleNamespace(_handle=123)

        monkeypatch.setattr(device.ctypes, "WinDLL", only_absolute)
        assert device.prepare_cuda_libraries()[0] is False
        assert self.references == 0
        assert not device._prepared_cuda_libraries

    def test_non_windows_keeps_its_existing_cuda_loading(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert device.prepare_cuda_libraries() == (None, (), None)
        assert self.loads == []


class TestPreparingBeforeTheEngine:
    @pytest.fixture
    def model(self, tmp_path, monkeypatch):
        (tmp_path / "tokenizer.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(faster_whisper_backend.external_models, "model_directory",
                            lambda *args: str(tmp_path))
        self.loads = []

        def factory(*args, **kwargs):
            self.loads.append(kwargs)
            return object()

        self.factory = factory
        return faster_whisper_backend.FasterWhisperBackend(model_factory=factory)

    def request(self, device_name):
        return backend.TranscriptionRequest(
            audio_path="unused.wav", models_root="models", model_id="tiny",
            device=device_name, compute_type="float16")

    def test_cuda_prepares_before_loading_and_reuses_the_prepared_model(self, model, monkeypatch):
        preparations = []

        def prepare():
            assert self.loads == []
            preparations.append(True)
            return True, (), None

        monkeypatch.setattr(device, "prepare_cuda_libraries", prepare)
        request = self.request("cuda")
        model.load_model(request)
        model.load_model(request)
        assert preparations == [True]
        assert len(self.loads) == 1

    @pytest.mark.parametrize("trial", [False, True])
    def test_failed_preparation_never_enters_the_native_model(self, model, monkeypatch, tmp_path, trial):
        monkeypatch.setattr(device, "prepare_cuda_libraries",
                            lambda: (False, ("cublas64_12.dll",), "cannot load cuBLAS"))
        with pytest.raises(errors.TranscriptionError) as caught:
            if trial:
                model.trial_load(str(tmp_path), "cuda", "float16")
            else:
                model.load_model(self.request("cuda"))
        assert caught.value.code == errors.CUDA_UNAVAILABLE
        assert self.loads == []

    @pytest.mark.parametrize("trial", [False, True])
    def test_cpu_does_not_attempt_windows_cuda_loading(self, model, monkeypatch, tmp_path, trial):
        def forbidden():
            pytest.fail("CPU transcription must not prepare CUDA")

        monkeypatch.setattr(device, "prepare_cuda_libraries", forbidden)
        if trial:
            model.trial_load(str(tmp_path), "cpu", "int8")
        else:
            model.load_model(self.request("cpu"))
        assert len(self.loads) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL loader regression")
def test_real_windows_legacy_lookup_after_absolute_preparation(tmp_path):
    # A signed Windows library, renamed to a name absent from the system path.
    # The child releases all mappings on exit, allowing tmp_path cleanup.
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    library_name = "winzapp_cuda_loader_regression.dll"
    shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32/version.dll",
                    runtime / library_name)
    legacy = tmp_path / "frozen_internal"
    legacy.mkdir()
    script = textwrap.dedent("""
        import ctypes, sys
        from core.transcription import device
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        kernel.SetDllDirectoryW.restype = ctypes.c_int
        assert kernel.SetDllDirectoryW(sys.argv[2])
        device._CUDA_RUNTIME_LIBRARIES = (sys.argv[3],)
        assert device.register_cuda_library_directory(sys.argv[1])
        assert device.probe_cuda_libraries() == (True, (), None)
        assert device.prepare_cuda_libraries() == (True, (), None)
        ctypes.WinDLL(sys.argv[3], winmode=0)
    """)
    env = dict(os.environ, PYTHONPATH=str(Path(device.__file__).resolve().parents[2]))
    result = subprocess.run([sys.executable, "-c", script, str(runtime), str(legacy), library_name],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
