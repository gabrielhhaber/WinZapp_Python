"""The call playback component (NetEq) WinZapp can download, as data and as a folder.

Same contract as the noise reduction model (core/noise_model.py): pinned file
names, exact sizes and SHA-256, fetched through the transcription model store,
so resuming, verification, the shared lock and the free-space gate are not
written a second time. Only the host differs: the DLL is built by this
repository (native/call_neteq, .github/workflows/neteq-dll.yml) and published
as a GitHub release of it, under a ``neteq-*`` tag the updater ignores.

The component is optional. Without it, or with a copy that fails its digest or
its ABI check, calls keep the compatible playback (core/call_neteq_buffer.py).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

from app_paths import global_dir
from core.call_neteq import ABI
from core.transcription import model_store

COMPONENTS_DIRNAME = "call_components"
DLL = "winzapp_call_neteq.dll"
# The licences of the DLL (NetEq, its Rust dependencies, Rust's std), in one
# file: a binary handed out without its notices would break their terms.
NOTICES = "winzapp_call_neteq_NOTICES.txt"
# The pin names this repository, not config.GITHUB_REPO: that one follows an
# environment variable, and a pinned download comes from one place.
REPOSITORY = "gabrielhhaber/WinZapp_Python"

# ── Filled by a maintainer after publishing a neteq-* release ────────────────
# neteq-dll.yml drafts the release and prints these five values (NETEQ_PIN.txt
# in its artifact). While any of them is empty the download is not offered.
RELEASE_TAG = ""
DLL_BYTES = 0
DLL_SHA256 = ""
NOTICES_BYTES = 0
NOTICES_SHA256 = ""
# ─────────────────────────────────────────────────────────────────────────────

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class NetEqComponent:
    id: str
    tag: str
    files: tuple[tuple[str, int], ...]
    digests: tuple[tuple[str, str], ...]
    abi: int

    @property
    def download_bytes(self) -> int:
        return sum(size for _, size in self.files)

    def sha256_of(self, name):
        return dict(self.digests).get(name)

    def url_of(self, name):
        return f"https://github.com/{REPOSITORY}/releases/download/{self.tag}/{name}"


NETEQ = NetEqComponent(
    id="winzapp_call_neteq",
    tag=RELEASE_TAG,
    files=((DLL, DLL_BYTES), (NOTICES, NOTICES_BYTES)),
    digests=((DLL, DLL_SHA256), (NOTICES, NOTICES_SHA256)),
    abi=ABI,
)


def is_pinned(component=NETEQ) -> bool:
    """Whether the entry names a release, a size and a digest for every file:
    never a download without them."""
    return (bool(component.tag) and component.tag.startswith("neteq-")
            and all(size > 0 for _, size in component.files)
            and all(_SHA256.fullmatch(component.sha256_of(name) or "")
                    for name, _ in component.files))


def components_dir() -> str:
    return global_dir(COMPONENTS_DIRNAME)


def installed_dll(root: str | None = None, component=NETEQ) -> str:
    return os.path.join(model_store.model_dir(root or components_dir(), component.id), DLL)


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_dll(root: str | None = None, component=NETEQ) -> str | None:
    """The downloaded DLL's path when it is exactly the pinned file, else None.

    Hashed on every call: the file is half a megabyte, read once per call,
    and a size check alone would load whatever happens to have that size.
    """
    if not is_pinned(component):
        return None
    path = installed_dll(root, component)
    try:
        if os.path.getsize(path) != dict(component.files)[DLL]:
            return None
        return path if file_sha256(path) == component.sha256_of(DLL) else None
    except OSError:
        return None


def installation_state(root: str | None = None, component=NETEQ) -> str:
    """model_store's states, with INCOMPLETE also for a DLL of the right size
    and the wrong digest: the download repairs it."""
    state = model_store.installation_state(root or components_dir(), component).state
    if state == model_store.STATE_INSTALLED and verified_dll(root, component) is None:
        return model_store.STATE_INCOMPLETE
    return state


def download(root: str | None = None, progress=None, should_cancel=None, session=None,
             component=NETEQ) -> str:
    """Fetch the component if it is missing or damaged; returns the DLL's path."""
    if not is_pinned(component):
        raise ValueError("the NetEq component has no published release pinned")
    root = root or components_dir()
    if (model_store.is_installed(root, component)
            and verified_dll(root, component) is None):
        # Every size right, the digest wrong: download_model() trusts sizes
        # and would fetch nothing.
        model_store.repair_model(component, root, progress, should_cancel, session)
    else:
        model_store.download_model(component, root, progress, should_cancel, session)
    return installed_dll(root, component)


def remove(root: str | None = None, component=NETEQ) -> bool:
    """Delete the component's files (only the entry's names). True when the
    DLL is gone afterwards; False when Windows kept it because a WinZapp
    process has it loaded (a call in this or another account since start)."""
    model_store.remove_model_files(root or components_dir(), component)
    return not os.path.exists(installed_dll(root, component))
