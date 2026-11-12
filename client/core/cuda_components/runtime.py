"""The noise reducer's ONNX Runtime: which build, and on which device.

Two builds of onnxruntime cannot share a process: both load a DLL named
onnxruntime.dll, and Windows hands the second import the first one's, and a
loaded DLL never leaves. So the GPU builds (CUDA 12 or 13) never enter
WinZapp's own process: the noise reducer runs them in a process of its own
(noise_worker.py, driven by noise_worker_host.py), which `activate()` sets up
before anything there imports onnxruntime: the GPU build's package goes in
front of the bundled one (a meta path finder, which also beats PyInstaller's
own importer) and the CUDA libraries' folders go on the loader's path.
Changing the device restarts that process, never WinZapp.

WinZapp's own process keeps the bundled build, for the processor (and the
voice filter of faster-whisper) and, on macOS, Core ML, offered as its own
device with nothing to download (`set_in_process()`, `providers()`).
"""

import importlib.abc
import importlib.machinery
import logging
import os
import sys

from core.cuda_components.model import (
    DEVICE_CPU, DEVICE_COREML, ONNXRUNTIME_OF, SHARED_CUBLAS_CU12, STACKS, device_of,
)

#: The device this process's onnxruntime serves (`active()`).
_active = None


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


def uses_worker(device, platform=None) -> bool:
    """Whether *device* runs in the noise reducer's own process: the GPU
    builds, which only Windows offers."""
    return device in ONNXRUNTIME_OF and (platform or sys.platform) == "win32"


def set_in_process(device) -> str:
    """The device of the model WinZapp's own process loads: Core ML on macOS
    when chosen, else the processor (a GPU build never comes in here)."""
    global _active
    _active = DEVICE_COREML if (device == DEVICE_COREML and sys.platform == "darwin") else DEVICE_CPU
    return _active


def activate(device_or_settings) -> str:
    """In the noise reducer's own process, before anything imports
    onnxruntime: put the chosen GPU build in place. Returns the device in use
    ("cpu" when none was put)."""
    global _active
    device = (device_or_settings if isinstance(device_or_settings, str)
              else device_of(device_or_settings))
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
                            "the GPU build cannot come in")
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


def providers(available) -> list:
    """The execution providers for the noise model, best first, among
    `available` (onnxruntime.get_available_providers())."""
    device = active()
    wanted = {"cuda12": "CUDAExecutionProvider", "cuda13": "CUDAExecutionProvider",
              DEVICE_COREML: "CoreMLExecutionProvider"}.get(device)
    chosen = [wanted] if wanted and wanted in available else []
    return chosen + ["CPUExecutionProvider"]
