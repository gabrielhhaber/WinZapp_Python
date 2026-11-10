"""What a CUDA component is, and which ones each use needs (no I/O here)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Component:
    """One pinned NVIDIA (or ONNX Runtime) wheel and what is kept out of it.

    `members` are paths inside the wheel, kept flat under their file names
    (DLLs, put on the loader's path); `tree` instead keeps every file under
    that prefix with its relative path (the onnxruntime package itself)."""

    id: str
    title: str
    url: str
    filename: str
    wheel_bytes: int
    sha256: str
    dist_prefix: str
    extracted_bytes: int
    members: tuple = ()
    tree: str = ""

    @property
    def install_bytes(self) -> int:
        """The peak on disk: the wheel and what comes out of it, at once."""
        return self.wheel_bytes + self.extracted_bytes


#: cuBLAS for CUDA 12 is the local transcription's own download
#: (core/transcription/cuda_runtime.py): the noise reducer on CUDA 12 uses
#: that one rather than a copy, and a user who has it pays nothing for it.
SHARED_CUBLAS_CU12 = "cublas_cu12"

#: The noise reducer's devices, as stored in settings.json
#: (general.noise_reduction_device).
DEVICE_CPU = "cpu"
DEVICE_CUDA12 = "cuda12"
DEVICE_CUDA13 = "cuda13"
#: macOS only: the bundled onnxruntime's Core ML provider, nothing to download
DEVICE_COREML = "coreml"
DEVICES = (DEVICE_CPU, DEVICE_CUDA12, DEVICE_CUDA13, DEVICE_COREML)

#: Every component a device needs, in download order (small first: a cancel
#: late in the big ones leaves the small ones done).
STACKS = {
    DEVICE_CPU: (),
    DEVICE_COREML: (),
    DEVICE_CUDA12: ("cudart_cu12", "nvjitlink_cu12", "nvrtc_cu12", "curand_cu12",
                    "cufft_cu12", SHARED_CUBLAS_CU12, "ort_gpu_cu12", "cudnn_cu12"),
    DEVICE_CUDA13: ("cudart_cu13", "nvjitlink_cu13", "nvrtc_cu13", "curand_cu13",
                    "cufft_cu13", "cublas_cu13", "ort_gpu_cu13", "cudnn_cu13"),
}

#: The component holding the onnxruntime package of each device.
ONNXRUNTIME_OF = {DEVICE_CUDA12: "ort_gpu_cu12", DEVICE_CUDA13: "ort_gpu_cu13"}


def device_of(settings) -> str:
    """The noise reducer's device from settings.json; anything else is the CPU."""
    general = settings.get("general") if isinstance(settings, dict) else None
    value = general.get("noise_reduction_device") if isinstance(general, dict) else None
    return value if value in DEVICES else DEVICE_CPU


def offered_devices(platform) -> tuple:
    """The devices the settings offer on `platform` (sys.platform): CUDA only
    on Windows, Core ML only on macOS."""
    if platform == "win32":
        return (DEVICE_CPU, DEVICE_CUDA12, DEVICE_CUDA13)
    if platform == "darwin":
        return (DEVICE_CPU, DEVICE_COREML)
    return (DEVICE_CPU,)
