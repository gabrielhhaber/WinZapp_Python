"""Install and check one CUDA component (catalog.py), install-wide.

The same guarantees as core/transcription/cuda_runtime.py, which installs the
local transcription's cuBLAS, generalised to any pinned wheel: the wheel is
checked against its pinned sha256 while it arrives, every file kept out of it
is checked against the wheel's own RECORD before it gets its final name, and
RECORD is written last, so an interrupted install never looks complete. A
component lives in a folder of its own under global_dir("cuda_components"),
shared by every account and by both features that use it; the cross-process
lock keeps two accounts from writing one another's `.part` files.

cuBLAS for CUDA 12 is not here: it is the transcription's own
(model.SHARED_CUBLAS_CU12), read through cuda_runtime.
"""

import base64
import contextlib
import csv
import hashlib
import logging
import os
import shutil
import time
import zipfile

from app_paths import global_dir
from coord_locks import LockTimeout, models_lock
from core import tls_trust
from core.cuda_components.catalog import COMPONENTS
from core.cuda_components.model import SHARED_CUBLAS_CU12, STACKS
from core.transcription import model_store
from core.transcription._fileops import check_cancel, report

ROOT_DIRNAME = "cuda_components"
RECORD_FILENAME = "RECORD"
_PART = ".part"
_CHUNK = 1024 * 1024
_HTTP_TIMEOUT = (30, 300)
_LOCK_POLL_SECONDS = 2.0
_LOCK_TIMEOUT_SECONDS = 12 * 60 * 60.0
#: how long a removal waits for the folder's lock: it runs on the wx thread,
#: and an install holding the lock can take many minutes (cuDNN, 700 MB)
REMOVE_LOCK_SECONDS = 2.0

STATE_ABSENT = "absent"
STATE_INCOMPLETE = "incomplete"
STATE_INSTALLED = "installed"


class ComponentError(Exception):
    """A component that could not be installed; `code` says why
    (download_failed, corrupted, no_space, busy, in_use)."""

    def __init__(self, code, detail=""):
        super().__init__(detail or code)
        self.code = code


def component_dir(component_id, root=None) -> str:
    return os.path.join(root or global_dir(ROOT_DIRNAME), component_id)


def _parse_record(raw) -> dict:
    """{path inside the wheel: (digest, size)} for the lines with a sha256."""
    entries = {}
    for row in csv.reader(raw.decode("utf-8").splitlines()):
        if len(row) >= 3 and row[1].startswith("sha256=") and row[2].strip().isdigit():
            entries[row[0]] = (row[1].strip(), int(row[2]))
    return entries


def kept_files(component, record) -> list:
    """[(path inside the wheel, path on disk relative to the folder)]."""
    if component.tree:
        return [(path, path) for path in sorted(record) if path.startswith(component.tree)]
    return [(member, member.rsplit("/", 1)[-1]) for member in component.members]


def state(component_id, root=None) -> str:
    """The cheap check: RECORD of this pin, and every kept file at its size."""
    if component_id == SHARED_CUBLAS_CU12:
        from core.transcription import cuda_runtime
        return cuda_runtime.installation_state().state
    component = COMPONENTS[component_id]
    folder = component_dir(component_id, root)
    try:
        with open(os.path.join(folder, RECORD_FILENAME), "rb") as handle:
            record = _parse_record(handle.read())
    except (OSError, UnicodeDecodeError):
        return STATE_INCOMPLETE if os.path.isdir(folder) and os.listdir(folder) else STATE_ABSENT
    if not any(path.startswith(component.dist_prefix) for path in record):
        return STATE_INCOMPLETE          # another pin's files
    for member, relative in kept_files(component, record):
        try:
            if os.path.getsize(os.path.join(folder, relative)) != record[member][1]:
                return STATE_INCOMPLETE
        except (OSError, KeyError):
            return STATE_INCOMPLETE
    return STATE_INSTALLED


def missing(device, root=None) -> list:
    """The component ids `device` needs that are not installed, in order."""
    return [cid for cid in STACKS.get(device, ()) if state(cid, root) != STATE_INSTALLED]


def bytes_to_download(component_ids) -> int:
    """The download of `component_ids` (the shared cuBLAS at its own size)."""
    total = 0
    for cid in component_ids:
        if cid == SHARED_CUBLAS_CU12:
            from core.transcription import cuda_runtime
            total += cuda_runtime.WHEEL_BYTES
        else:
            total += COMPONENTS[cid].wheel_bytes
    return total


@contextlib.contextmanager
def _locked(folder, should_cancel, timeout=_LOCK_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while True:
        lock = models_lock(folder, global_dir(), timeout=min(_LOCK_POLL_SECONDS, timeout))
        try:
            lock.acquire()
            break
        except LockTimeout:
            check_cancel(should_cancel)
            if time.monotonic() >= deadline:
                raise ComponentError("busy")
    try:
        yield
    finally:
        lock.release()


def install(component_id, progress=None, should_cancel=None, session=None, root=None):
    """Put one component on this machine (a no-op when it is installed)."""
    if component_id == SHARED_CUBLAS_CU12:
        from core.transcription import cuda_runtime
        cuda_runtime.install_cuda_runtime(progress=progress, should_cancel=should_cancel)
        return
    component = COMPONENTS[component_id]
    folder = component_dir(component_id, root)
    os.makedirs(folder, exist_ok=True)
    with _locked(folder, should_cancel):
        if state(component_id, root) == STATE_INSTALLED:
            return
        try:
            model_store.ensure_free_space(folder, component.install_bytes)
        except Exception as exc:
            raise ComponentError("no_space", str(exc)) from exc
        owned = session is None
        session = tls_trust.create_session() if owned else session
        wheel = os.path.join(folder, component.filename + _PART)
        try:
            _download(component, session, wheel, progress, should_cancel)
            _extract(component, wheel, folder, progress, should_cancel)
        except ComponentError:
            _sweep(folder)
            raise
        except Exception as exc:
            _sweep(folder)
            if getattr(exc, "code", "") == "cancelled":
                raise
            if isinstance(exc, PermissionError):
                raise ComponentError("in_use", str(exc)) from exc
            raise ComponentError("download_failed", str(exc)) from exc
        finally:
            if owned:
                session.close()
    logging.info("[cuda-components] %s installed", component_id)


def _download(component, session, part_path, progress, should_cancel):
    digest, written = hashlib.sha256(), 0
    response = session.get(component.url, stream=True, timeout=_HTTP_TIMEOUT)
    try:
        response.raise_for_status()
        with open(part_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=_CHUNK):
                check_cancel(should_cancel)
                if chunk:
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    report(progress, written, component.install_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        response.close()
    if written != component.wheel_bytes or digest.hexdigest() != component.sha256:
        raise ComponentError("corrupted", f"{component.filename}: not the pinned wheel")


def _record_digest(hasher) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hasher.digest()).rstrip(b"=").decode("ascii")


def _extract(component, wheel_path, folder, progress, should_cancel):
    done = component.wheel_bytes
    with zipfile.ZipFile(wheel_path) as archive:
        record_member = component.dist_prefix + "RECORD"
        try:
            raw = archive.read(record_member)
        except KeyError as exc:
            raise ComponentError("corrupted", f"no {record_member}") from exc
        record = _parse_record(raw)
        kept = kept_files(component, record)
        if not kept or any(member not in record for member, _r in kept):
            raise ComponentError("corrupted", "RECORD does not describe the files")
        for member, relative in kept:
            target = os.path.join(folder, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            digest, written = hashlib.sha256(), 0
            with archive.open(member) as source, open(target + _PART, "wb") as handle:
                for chunk in iter(lambda: source.read(_CHUNK), b""):
                    check_cancel(should_cancel)
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    report(progress, min(done + written, component.install_bytes),
                           component.install_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            expected_digest, expected_size = record[member]
            if written != expected_size or _record_digest(digest) != expected_digest:
                raise ComponentError("corrupted", f"{relative}: not what RECORD states")
            os.replace(target + _PART, target)
            done += written
        with open(os.path.join(folder, RECORD_FILENAME + _PART), "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(os.path.join(folder, RECORD_FILENAME + _PART),
                   os.path.join(folder, RECORD_FILENAME))
    report(progress, component.install_bytes, component.install_bytes)
    try:
        os.unlink(wheel_path)
    except OSError:
        pass


def _sweep(folder):
    """After a failed attempt: everything of the attempt goes (the folder is
    this component's alone, and RECORD is only ever written last)."""
    shutil.rmtree(folder, ignore_errors=True)


def removable(device, root=None) -> list:
    """The components of `device` with anything on disk, which may be removed:
    never the shared cuBLAS, which the local transcription uses (it is removed
    from the Transcription tab). One with a `.part` file stays offered: after
    a crash that file is an orphan the user must be able to clear, and a
    download really running is refused by the dialog (_background_busy) or by
    remove()'s lock ("busy")."""
    return [cid for cid in STACKS.get(device, ())
            if cid != SHARED_CUBLAS_CU12 and state(cid, root) != STATE_ABSENT]


def remove(component_ids, root=None, lock_timeout=REMOVE_LOCK_SECONDS) -> list:
    """Delete `component_ids`' folders. Returns those that could not go whole:
    a DLL this process already loaded (the build in use) stays until WinZapp
    restarts. ComponentError("busy") when another window holds a folder's
    lock for longer than *lock_timeout* (an install): it runs on the wx thread
    and used to wait for the whole download."""
    left = []
    for cid in component_ids:
        folder = component_dir(cid, root)
        with _locked(folder, None, lock_timeout):
            shutil.rmtree(folder, ignore_errors=True)
        if os.path.exists(folder):
            left.append(cid)
    return left
