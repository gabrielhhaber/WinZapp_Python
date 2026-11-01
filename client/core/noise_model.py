"""The noise reduction model WinZapp downloads, as data and as a folder.

Same contract as the Whisper catalogue (core/transcription/model_catalog.py):
the model is not in the installer, it is fetched from Hugging Face at a pinned
revision with its exact size and SHA-256, and goes through the transcription
model store, so resuming, verification, the shared lock and the free-space gate
are not written a second time. It lives in its own folder, not in the Whisper
one, because the Whisper picker lists every folder it does not know as foreign.

Read from the Hugging Face API on 2026-10-28: the repository's commit and the
LFS digest of onnx/dpdfnet2_48khz_hr.onnx. The licence is Apache-2.0
(https://huggingface.co/Ceva-IP/DPDFNet).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app_paths import global_dir
from core.transcription import model_store

MODELS_DIRNAME = "noise_models"
MODEL_FILE = "dpdfnet2_48khz_hr.onnx"
# Where the file is inside the Hugging Face repository.
REMOTE_FOLDER = "onnx"


@dataclass(frozen=True)
class NoiseModel:
    id: str
    repo: str
    revision: str
    files: tuple[tuple[str, int], ...]
    sha256: str

    @property
    def download_bytes(self) -> int:
        return sum(size for _, size in self.files)

    def remote_path(self, name):
        return f"{REMOTE_FOLDER}/{name}"

    def sha256_of(self, name):
        return self.sha256 if name == MODEL_FILE else None


DPDFNET2_48K = NoiseModel(
    id="dpdfnet2_48khz_hr",
    repo="Ceva-IP/DPDFNet",
    revision="dd6818d00f50c836fed43a6243ebe49116de5964",
    files=((MODEL_FILE, 10_493_337),),
    sha256="7f0575a5cec0ba4ffd8f8bd657e06d007e4ccdd955d76faab922b9d3291dc14b",
)


def models_dir() -> str:
    return global_dir(MODELS_DIRNAME)


def is_installed(root: str | None = None) -> bool:
    return model_store.is_installed(root or models_dir(), DPDFNET2_48K)


def installed_path(root: str | None = None) -> str:
    return os.path.join(model_store.model_dir(root or models_dir(), DPDFNET2_48K.id), MODEL_FILE)


def download(root: str | None = None, progress=None, should_cancel=None, session=None) -> str:
    """Fetch the model if it is missing and return the path of the .onnx file."""
    model_store.download_model(DPDFNET2_48K, root or models_dir(), progress, should_cancel, session)
    return installed_path(root)
