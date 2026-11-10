"""CUDA components shared between the local transcription and the noise reducer.

The noise reducer (core/neural_noise_reduction.py) runs on ONNX Runtime; on a
graphics card that needs an onnxruntime-gpu build and NVIDIA's libraries for
it. Two builds are offered, because the two CUDA generations do not mix:

* CUDA 12: onnxruntime-gpu 1.26 (the last for CUDA 12), sharing the cuBLAS 12
  the local transcription already downloads (core/transcription/cuda_runtime.py):
  a user who has it does not download it again. Older cards too.
* CUDA 13: onnxruntime-gpu 1.29, with its own CUDA 13 libraries: more space,
  a recent driver.

The local transcription stays on CUDA 12 on Windows x64: neither CTranslate2's
wheels nor whisper.cpp's x64 builds exist for CUDA 13.

* model.py — what a component is, which ones each device needs;
* catalog.py — the pinned wheels (URL, size, sha256, files kept), generated;
* store.py — install and check a component, install-wide;
* runtime.py — put the chosen build in place at startup, and the providers.

Every component is downloaded once and only when missing: `store.missing()`
is what the settings show and download.
"""
