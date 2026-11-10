"""CUDA components for the noise reducer, shared with the local transcription
(core/cuda_components): which ones each device needs, the pinned install,
and the GPU build put in place at startup."""
import base64
import hashlib
import importlib
import io
import os
import sys
import types
import zipfile

import pytest

from core.cuda_components import catalog, model, runtime, store
from core.cuda_components.model import Component


def test_the_devices_offered_follow_the_system():
    assert model.offered_devices("win32") == ("cpu", "cuda12", "cuda13")
    assert model.offered_devices("darwin") == ("cpu", "coreml")
    assert model.offered_devices("linux") == ("cpu",)
    assert model.device_of({"general": {"noise_reduction_device": "cuda13"}}) == "cuda13"
    assert model.device_of({"general": {"noise_reduction_device": "gpu!"}}) == "cpu"
    assert model.device_of(None) == "cpu"


def test_cuda_12_shares_the_transcriptions_cublas_and_every_id_is_pinned():
    assert model.SHARED_CUBLAS_CU12 in model.STACKS["cuda12"]
    assert model.SHARED_CUBLAS_CU12 not in model.STACKS["cuda13"]
    for device in ("cuda12", "cuda13"):
        for cid in model.STACKS[device]:
            assert cid == model.SHARED_CUBLAS_CU12 or cid in catalog.COMPONENTS
        assert model.ONNXRUNTIME_OF[device] in model.STACKS[device]
    for component in catalog.COMPONENTS.values():
        assert component.url.startswith("https://files.pythonhosted.org/")
        assert component.url.endswith(component.filename)
        assert len(component.sha256) == 64 and component.wheel_bytes > 0
        assert bool(component.members) != bool(component.tree)


def _wheel(files, dist="fake-1.0.dist-info/"):
    """A wheel holding `files` {path: bytes}, with its RECORD."""
    lines = []
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        lines.append(f"{path},sha256={digest},{len(data)}")
    lines.append(f"{dist}RECORD,,")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, data in files.items():
            z.writestr(path, data)
        z.writestr(dist + "METADATA", b"meta")
        lines.append(f"{dist}METADATA,sha256="
                     + base64.urlsafe_b64encode(hashlib.sha256(b"meta").digest()).rstrip(b"=").decode()
                     + ",4")
        z.writestr(dist + "RECORD", "\n".join(lines).encode())
    return buf.getvalue()


class _Session:
    def __init__(self, data):
        self.data, self.gets = data, []

    def get(self, url, stream, timeout):
        self.gets.append(url)
        data = self.data

        class R:
            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                for i in range(0, len(data), chunk_size):
                    yield data[i:i + chunk_size]

            def close(self):
                pass
        return R()

    def close(self):
        pass


def _component(data, **kw):
    return Component(id="fake", title="Fake", url="https://x/fake.whl", filename="fake.whl",
                     wheel_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                     dist_prefix="fake-1.0.dist-info/", extracted_bytes=10, **kw)


@pytest.fixture
def fake_catalog(monkeypatch):
    def put(component):
        monkeypatch.setitem(store.COMPONENTS, component.id, component)
    monkeypatch.setattr(store.model_store, "ensure_free_space", lambda folder, size: None)
    monkeypatch.setattr(store, "models_lock", lambda *a, **k: types.SimpleNamespace(
        acquire=lambda: None, release=lambda: None))
    return put


def test_only_the_kept_files_are_installed_checked_and_record_comes_last(tmp_path, fake_catalog):
    data = _wheel({"nvidia/x/bin/a.dll": b"AAAA", "nvidia/x/bin/b.dll": b"BB",
                   "nvidia/x/include/h.h": b"h"})
    fake_catalog(_component(data, members=("nvidia/x/bin/a.dll", "nvidia/x/bin/b.dll")))
    assert store.state("fake", str(tmp_path)) == store.STATE_ABSENT
    session = _Session(data)
    store.install("fake", session=session, root=str(tmp_path))
    folder = tmp_path / "fake"
    assert sorted(os.listdir(folder)) == ["RECORD", "a.dll", "b.dll"]
    assert store.state("fake", str(tmp_path)) == store.STATE_INSTALLED
    store.install("fake", session=session, root=str(tmp_path))      # installed: nothing fetched
    assert len(session.gets) == 1
    (folder / "b.dll").write_bytes(b"B")                             # wrong size
    assert store.state("fake", str(tmp_path)) == store.STATE_INCOMPLETE


def test_a_tree_keeps_its_folders(tmp_path, fake_catalog):
    data = _wheel({"onnxruntime/__init__.py": b"x = 1\n", "onnxruntime/capi/ort.dll": b"D"})
    fake_catalog(_component(data, tree="onnxruntime/"))
    store.install("fake", session=_Session(data), root=str(tmp_path))
    assert (tmp_path / "fake" / "onnxruntime" / "capi" / "ort.dll").read_bytes() == b"D"


def test_a_wheel_that_is_not_the_pinned_one_leaves_nothing(tmp_path, fake_catalog):
    data = _wheel({"nvidia/x/bin/a.dll": b"AAAA"})
    fake_catalog(_component(data, members=("nvidia/x/bin/a.dll",)))
    with pytest.raises(store.ComponentError) as caught:
        store.install("fake", session=_Session(data + b"tampered"), root=str(tmp_path))
    assert caught.value.code == "corrupted"
    assert not (tmp_path / "fake").exists()


def test_what_is_missing_counts_the_shared_cublas_at_its_own_size(monkeypatch):
    states = {"cudart_cu12": store.STATE_INSTALLED}
    monkeypatch.setattr(store, "state", lambda cid, root=None: states.get(cid, store.STATE_ABSENT))
    lacking = store.missing("cuda12")
    assert "cudart_cu12" not in lacking and model.SHARED_CUBLAS_CU12 in lacking
    from core.transcription import cuda_runtime
    assert store.bytes_to_download([model.SHARED_CUBLAS_CU12]) == cuda_runtime.WHEEL_BYTES
    assert store.missing("cpu") == []


def test_the_providers_put_the_card_first_only_when_its_build_is_active(monkeypatch):
    monkeypatch.setattr(runtime, "_active", "cuda12")
    assert runtime.providers(["CUDAExecutionProvider", "CPUExecutionProvider"]) == [
        "CUDAExecutionProvider", "CPUExecutionProvider"]
    assert runtime.providers(["CPUExecutionProvider"]) == ["CPUExecutionProvider"]
    monkeypatch.setattr(runtime, "_active", "coreml")
    assert runtime.providers(["CoreMLExecutionProvider", "CPUExecutionProvider"])[0] == \
        "CoreMLExecutionProvider"
    monkeypatch.setattr(runtime, "_active", None)
    assert runtime.providers(["CUDAExecutionProvider", "CPUExecutionProvider"]) == [
        "CPUExecutionProvider"]


def test_the_libraries_folders_use_the_transcriptions_cublas():
    dirs = runtime.library_dirs("cuda12", lambda cid: f"C:/c/{cid}", "C:/t/cuda_runtime")
    assert "C:/t/cuda_runtime" in dirs and "C:/c/cudnn_cu12" in dirs
    assert "C:/c/ort_gpu_cu12" not in dirs
    assert dirs[-1] == os.path.join("C:/c/ort_gpu_cu12", "onnxruntime", "capi")


def test_the_finder_takes_onnxruntime_from_the_gpu_folder(tmp_path, monkeypatch):
    package = tmp_path / "onnxruntime"
    (package / "capi").mkdir(parents=True)
    (package / "__init__.py").write_text("WHERE = 'gpu'\n", encoding="utf-8")
    (package / "capi" / "__init__.py").write_text("", encoding="utf-8")
    finder = runtime._OnnxRuntimeFinder(str(tmp_path))
    for name in [n for n in sys.modules if n == "onnxruntime" or n.startswith("onnxruntime.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [finder] + sys.meta_path)
    module = importlib.import_module("onnxruntime")
    assert module.WHERE == "gpu"
    assert importlib.import_module("onnxruntime.capi").__file__.startswith(str(tmp_path))
    for name in [n for n in sys.modules if n == "onnxruntime" or n.startswith("onnxruntime.")]:
        monkeypatch.delitem(sys.modules, name)


def test_the_components_field_says_what_is_missing_and_when_it_applies():
    from core.cuda_components import text as ctext
    i18n = types.SimpleNamespace(t=lambda key: {
        "decimal_separator": ",",
        "noise_reducer_components_none_needed": "Nothing to download.",
        "noise_reducer_components_ready": "All installed.",
        "noise_reducer_components_missing": "{count} missing ({size}):",
        "noise_reducer_device_next_start": "Applies next start.",
        "noise_reducer_shared_cublas": "cuBLAS 12 (shared)"}[key])
    assert ctext.components_text(i18n, "cpu", [], 0, "cpu") == "Nothing to download."
    assert ctext.components_text(i18n, "cuda12", [], 0, "cuda12") == "All installed."
    text = ctext.components_text(i18n, "cuda12", ["cudart_cu12", model.SHARED_CUBLAS_CU12],
                                 2 * 1024 ** 3, "cpu")
    assert text.startswith("2 missing (2,0 GB): CUDA Runtime 12, cuBLAS 12 (shared).")
    assert text.endswith("Applies next start.")


def test_only_this_processors_own_components_may_be_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "state", lambda cid, root=None: store.STATE_INSTALLED)
    removable = store.removable("cuda12")
    assert model.SHARED_CUBLAS_CU12 not in removable and "cudnn_cu12" in removable
    monkeypatch.setattr(store, "models_lock", lambda *a, **k: types.SimpleNamespace(
        acquire=lambda: None, release=lambda: None))
    (tmp_path / "cudart_cu12").mkdir()
    (tmp_path / "cudart_cu12" / "cudart64_12.dll").write_bytes(b"x")
    assert store.remove(["cudart_cu12"], root=str(tmp_path)) == []
    assert not (tmp_path / "cudart_cu12").exists()


def test_the_neural_box_only_with_the_reducer_on_and_the_model_there():
    from ui.dialogs.noise_reducer_dialog import neural_box_shown
    assert neural_box_shown(True, True) is True
    assert neural_box_shown(True, False) is False
    assert neural_box_shown(False, True) is False


def test_the_common_reducer_is_used_when_chosen(monkeypatch):
    from core import neural_noise_reduction as nnr
    import core.audio_processing as ap
    monkeypatch.setattr(ap, "apply_noise_gate", lambda audio, rate, channels, strict: b"gated")
    assert nnr.reduce_voice_noise(b"\x00\x00", 48000, 1, neural=False) == (b"gated", "gate")


def test_the_components_job_says_the_reducers_own_sentence():
    from core.cuda_components.job import ComponentsInstallJob, total_bytes
    job = ComponentsInstallJob(["cudart_cu12"])
    assert job.announcement(("cudart_cu12",), None).i18n_key == "noise_reducer_components_done"
    assert total_bytes(["cudart_cu12"]) == catalog.COMPONENTS["cudart_cu12"].install_bytes
