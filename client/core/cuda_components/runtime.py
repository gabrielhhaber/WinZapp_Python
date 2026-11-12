"""The noise reducer's ONNX Runtime on the graphics card, chosen at startup.

Two builds of onnxruntime cannot share a process: both load a DLL named
onnxruntime.dll, and Windows hands the second import the first one's. So the
choice is made once, before anything imports onnxruntime (the noise reducer
and faster-whisper's voice filter both import it lazily): when the user chose
CUDA 12 or 13 and every component of it is installed, the GPU build's package
is put in front of the bundled one for the whole process (a meta path finder,
which also beats PyInstaller's own importer) and the CUDA libraries' folders
go on the loader's path. The GPU build runs on the processor too, so the
voice filter is unaffected. A change of device therefore applies from the
next start, which the settings dialog says.

On macOS there is no CUDA: the bundled onnxruntime carries Core ML, offered
as its own device with nothing to download (`providers()`).
"""

import importlib.abc
import importlib.machinery
import logging
import os
import sys

from core.cuda_components.model import (
    DEVICE_CPU, DEVICE_COREML, ONNXRUNTIME_OF, SHARED_CUBLAS_CU12, STACKS, device_of,
)

_active = None
#: The device the user chose last, when it differs from the build in place
#: and could still be honoured without a restart (apply_now()).
_chosen = None


class _OnnxRuntimeFinder(importlib.abc.MetaPathFinder):
    """Finds `onnxruntime` (and its submodules) in the GPU build's folder."""

    def __init__(self, root):
        self.root = root

    def find_spec(self, name, path=None, target=None):
        if name == "onnxruntime":
            return importlib.machinery.PathFinder.find_spec(name, [self.root])
        if name.startswith("onnxruntime."):
            parent = sys.modules.get(name.rpartition(".")[0])
            search = list(getattr(parent, "__path__", []) or [])
            if search and os.path.normcase(search[0]).startswith(os.path.normcase(self.root)):
                return importlib.machinery.PathFinder.find_spec(name, search)
        return None


def library_dirs(device, component_dir, cuda_runtime_dir) -> list:
    """The folders the loader must search for `device`'s CUDA libraries."""
    dirs = []
    for cid in STACKS.get(device, ()):
        if cid == SHARED_CUBLAS_CU12:
            dirs.append(cuda_runtime_dir)
        elif cid != ONNXRUNTIME_OF.get(device):
            dirs.append(component_dir(cid))
    root = component_dir(ONNXRUNTIME_OF[device])
    dirs.append(os.path.join(root, "onnxruntime", "capi"))
    return dirs


def activate(settings) -> str:
    """At startup, before anything imports onnxruntime: put the chosen GPU
    build in place. Returns the device in use ("cpu" when none was put)."""
    global _active
    device = device_of(settings)
    if device in (DEVICE_CPU, DEVICE_COREML) or sys.platform != "win32":
        _active = device if sys.platform == "darwin" else DEVICE_CPU
        return _active
    try:
        from core.cuda_components import store
        from core.transcription import cuda_runtime
        lacking = store.missing(device)
        if lacking:
            logging.info("[noise_reduction] %s chosen but %d component(s) missing; "
                         "using the processor", device, len(lacking))
            _active = DEVICE_CPU
            return _active
        if "onnxruntime" in sys.modules:
            logging.warning("[noise_reduction] onnxruntime was imported already; "
                            "the GPU build applies from the next start")
            _active = DEVICE_CPU
            return _active
        dirs = library_dirs(device, store.component_dir,
                            cuda_runtime.default_cuda_runtime_dir())
        for folder in dirs:
            os.add_dll_directory(folder)
        # also for LoadLibrary with an altered search path, which is how
        # onnxruntime opens its CUDA provider
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
        sys.meta_path.insert(0, _OnnxRuntimeFinder(
            store.component_dir(ONNXRUNTIME_OF[device])))
        _active = device
        logging.info("[noise_reduction] GPU build for %s in place", device)
    except Exception as exc:
        logging.warning("[noise_reduction] could not put the GPU build in place: %s",
                        type(exc).__name__)
        _active = DEVICE_CPU
    return _active


def active() -> str:
    return _active or DEVICE_CPU


def needs_restart(device, loaded=None) -> bool:
    """Whether choosing *device* now would only apply from the next start:
    a GPU build other than the one in place, once onnxruntime is loaded."""
    if device in (DEVICE_CPU, DEVICE_COREML) or device == active():
        return False
    return ("onnxruntime" in sys.modules) if loaded is None else bool(loaded)


def apply_now(settings) -> bool:
    """Settings > Audio's noise reducer was saved: honour its device in this
    process when that is possible, and say whether it is (False: only from
    the next start). The processor is always possible: the GPU build runs on
    it too, so only the provider changes. A GPU build is possible while
    nothing has imported onnxruntime yet (no recording this session). The
    loaded model is dropped either way: it holds the old provider."""
    global _chosen
    device = device_of(settings)
    try:
        from core.neural_noise_reduction import forget_denoiser
        forget_denoiser()
    except Exception:
        pass
    if device == active():
        _chosen = None
        return True
    if device in (DEVICE_CPU, DEVICE_COREML):
        _chosen = device
        return True
    if "onnxruntime" not in sys.modules:
        _chosen = None
        return activate(settings) == device
    return False


def providers(available) -> list:
    """The execution providers for the noise model, best first, among
    `available` (onnxruntime.get_available_providers())."""
    device = _chosen if _chosen in (DEVICE_CPU, DEVICE_COREML) else active()
    wanted = {"cuda12": "CUDAExecutionProvider", "cuda13": "CUDAExecutionProvider",
              DEVICE_COREML: "CoreMLExecutionProvider"}.get(device)
    chosen = [wanted] if wanted and wanted in available else []
    return chosen + ["CPUExecutionProvider"]
