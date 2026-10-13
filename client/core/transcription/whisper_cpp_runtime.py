"""The whisper.cpp program (whisper-cli.exe and its DLLs), fetched on demand.

cuda_runtime's counterpart for the second backend: WinZapp does not ship the
binaries, it downloads one of the Windows builds ggml-org publishes with each
whisper.cpp release, the first time the user asks for it. Which release,
which builds a machine is offered and how zip member names are judged are
whisper_cpp_builds' — pure, and pinned there. The decisions here:

* **Verified before it is opened, and extracted defensively.** The zip is
  downloaded through model_store.download_verified_file(), so it reaches its
  final name only once size and sha256 match. Every member is then checked by
  `safe_member_path()` (no absolute path, no drive, no `..`, no `:` stream)
  and no symlink is written — the digest makes a hostile zip unlikely, and the
  check makes it harmless.

* **Published by one rename, with the manifest inside.** The tree is
  extracted into `<build>-<tag>.part/`, each file fsynced, then a manifest
  (`winzapp_manifest.json`: the release, the zip's digest, every file's
  relative path, size and sha256, and where the executable is) is written into
  it, and the folder is renamed into place. Whatever the build folder held
  before is renamed aside (`<build>.old-<n>`) first and deleted after — the
  folder is WinZapp's own, and a stray file in it must not block every later
  install. So `installation_state()` never sees half an install as a whole
  one, and an install from an earlier pin reads as INCOMPLETE with
  `installed_release` saying so. A rename refused because a file is held open
  (an antivirus, a running whisper-cli) is retried briefly, then reported as
  WHISPER_CPP_BUSY — never as a failed download to be fetched again.

* **Shared, so locked.** Under `global_dir()`, like the models; every mutation
  holds model_store's directory lock keyed on this root, and another
  process holding it is WHISPER_CPP_BUSY.

Nothing is resumed: the zip is scratch, deleted once extracted or on failure,
for cuda_runtime's reason — a dead 671 MB `.part` in the global folder is
invisible to the user and nothing else would collect it.

No user-facing text lives here: failures travel as TranscriptionError codes.
"""

from __future__ import annotations

import errno
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass

from app_paths import global_dir
from coord_locks import LockTimeout
from core import tls_trust
from core.transcription import errors, model_store, whisper_cpp_builds
from core.transcription._fileops import (
    check_cancel as _check_cancel,
    remove_empty_dir as _remove_empty_dir,
    report as _report,
    unlink as _unlink,
)
from core.transcription.whisper_cpp_builds import (
    EXECUTABLE_NAME,
    RELEASE_TAG,
    cuda_build_supported,
    locate_executable,
    safe_member_path,
)

# Subdirectory of the global data dir holding one folder per installed build.
RUNTIME_DIRNAME = "whisper_cpp_runtime"

MANIFEST_FILENAME = "winzapp_manifest.json"

STATE_ABSENT = "absent"
STATE_INCOMPLETE = "incomplete"
STATE_INSTALLED = "installed"

# The free-space gate has to answer before the zip is on disk, when only its
# compressed size is known: the zip plus up to three times that unpacked. An
# estimate, deliberately on the high side — the safe direction for a gate — and
# followed by the exact check once the zip's own table of sizes can be read.
INSTALL_SPACE_FACTOR = 4

_CHUNK_BYTES = 1024 * 1024
_PART_SUFFIX = ".part"

# The file-type bits of a zip member's Unix mode, and the value for a symlink.
_S_IFMT = 0o170000
_S_IFLNK = 0o120000

# A rename refused because something holds a file open — on Windows that is an
# antivirus scanning a DLL it has just seen appear more often than anything
# else, and it lets go within a second or two. Retried this many times, this
# far apart, before it is reported; re-downloading 671 MB would not help.
_REPLACE_ATTEMPTS = 5
_REPLACE_PAUSE_SECONDS = 0.5


@dataclass(frozen=True)
class RuntimeState:
    """What one build's folder holds. `missing` names what is wrong."""

    state: str
    missing: tuple[str, ...] = ()
    #: The release an install from an earlier pin carries, None otherwise. Its
    #: state is INCOMPLETE — it has to be replaced — but "update" and "finish
    #: the interrupted download" are different sentences.
    installed_release: str | None = None


# ── Paths and state ──────────────────────────────────────────────────────────


def default_runtime_dir() -> str:
    """Where the builds live: install-wide, beside the models. Not movable."""
    return global_dir(RUNTIME_DIRNAME)


# A folder of another WinZapp's builds the user asked to reuse instead of
# downloading them again (core/transcription/reused_runtimes.py). Process-wide
# and set at startup and on Apply. **Read-only**: nothing here installs into,
# repairs or removes from it — only usable_root() looks at it, and the files
# are held to the same pinned digests as WinZapp's own (verify_executable()).
_extra_root = None


def set_extra_root(root) -> None:
    """The folder to reuse builds from, or None/"" for none."""
    global _extra_root
    _extra_root = _resolve(root) if root else None


def extra_root():
    return _extra_root


def usable_root(build):
    """The root `build` can be launched from: WinZapp's own when it is
    installed there, else the reused folder when it holds this build complete,
    else None. Never raises."""
    try:
        own = _resolve(None)
        if installation_state(build, own).state == STATE_INSTALLED:
            return own
        extra = _extra_root
        if extra and extra != own and installation_state(build, extra).state == STATE_INSTALLED:
            return extra
    except Exception:
        pass
    return None


def build_dir(root, build) -> str:
    return os.path.join(root, build.id)


def installation_state(build, root=None) -> RuntimeState:
    """The cheap answer: the manifest, then every file it lists by exact size."""
    root = _resolve(root)
    directory = build_dir(root, build)
    manifest = _read_manifest(directory)
    if manifest is None:
        if not os.path.exists(directory) and not _leftovers(root, build):
            return RuntimeState(STATE_ABSENT)
        return RuntimeState(STATE_INCOMPLETE, (MANIFEST_FILENAME,))

    release = manifest.get("release")
    if release != RELEASE_TAG:
        return RuntimeState(STATE_INCOMPLETE, (), str(release or "") or None)
    if manifest.get("archive_sha256") != build.archive_sha256:
        # Same tag, another zip: not this build's install at all.
        return RuntimeState(STATE_INCOMPLETE, (MANIFEST_FILENAME,))

    missing = []
    for relative, size, _digest in manifest["files"]:
        try:
            actual = os.path.getsize(os.path.join(directory, *relative.split("/")))
        except OSError:
            missing.append(relative)
            continue
        if actual != size:
            missing.append(relative)
    if not missing:
        return RuntimeState(STATE_INSTALLED)
    return RuntimeState(STATE_INCOMPLETE, tuple(missing))


def executable_path(build, root=None):
    """whisper-cli.exe of an installed `build`, or None when it is not installed."""
    root = _resolve(root)
    if installation_state(build, root).state != STATE_INSTALLED:
        return None
    manifest = _read_manifest(build_dir(root, build))
    return os.path.join(build_dir(root, build), *manifest["executable"].split("/"))


def server_path(build, root=None):
    """whisper-server.exe of an installed `build`, beside whisper-cli.exe, when
    the manifest lists it with the digest whisper_cpp_builds pins; else None.

    Call verify_executable() first: that is what holds the file on disk to
    the manifest's digest, and this is what holds the manifest's digest to the
    pinned one. None for a build with no pin, a folder without the program, or
    one whose copy is not the pinned file: the caller then runs whisper-cli.
    """
    if not build.server_sha256:
        return None
    root = _resolve(root)
    directory = build_dir(root, build)
    manifest = _read_manifest(directory)
    if manifest is None:
        return None
    folder = manifest["executable"].rpartition("/")[0]
    relative = f"{folder}/{whisper_cpp_builds.SERVER_NAME}" if folder else whisper_cpp_builds.SERVER_NAME
    listed = {name: digest for name, _size, digest in manifest["files"]}
    if listed.get(relative) != build.server_sha256:
        return None
    return os.path.join(directory, *relative.split("/"))


#: What counts as code a launch could load: refused when the manifest omits it.
_CODE_SUFFIXES = (".dll", ".exe")


def unlisted_code_files(directory, listed) -> list:
    """The `.dll`/`.exe` files under `directory` that `listed` (relative paths
    with "/") does not name, compared without case as Windows does."""
    known = {relative.replace("\\", "/").casefold() for relative in listed}
    found = []
    for folder, _dirs, files in os.walk(directory):
        for name in files:
            if not name.casefold().endswith(_CODE_SUFFIXES):
                continue
            relative = os.path.relpath(os.path.join(folder, name), directory)
            if relative.replace(os.sep, "/").casefold() not in known:
                found.append(relative)
    return sorted(found)


# What verify_executable() has already hashed: (path, size, mtime_ns, digest).
# Every file of the build is read once per change of that file, not once per
# transcription — on the CUDA build that is over a gigabyte.
_verified_files = set()


def verify_executable(build, root=None) -> None:
    """Hash every file the manifest lists against known digests before launch.

    installation_state() only compares sizes, and the folder is writable by
    anything running as the user: a swapped whisper-cli.exe, or a DLL it loads,
    of the same size would run with the user's audio. The manifest sits in that
    same folder, so it cannot vouch for the files alone: the digests of the
    executable and the DLLs are pinned in whisper_cpp_builds, and the manifest
    has to agree with them and list every one. What is not pinned (the other
    exes of the zip, never launched) is held to the manifest. A swap that keeps
    size and mtime_ns (os.utime can) is not seen again once a file has passed:
    the accepted limit of a cache that costs one pass per process.

    **Nothing the manifest does not list may sit in the folder as code.** ggml
    picks its CPU backend by scanning the executable's own folder for
    `ggml-cpu-*.dll`, and Windows looks in the application folder first for any
    DLL loaded by name, so an extra `.dll` (or `.exe`) planted beside the
    verified ones would run with the user's audio unseen. A folder the user
    chose to reuse may be anywhere, writable by other accounts too; WinZapp's
    own is held to the same rule.
    """
    root = _resolve(root)
    directory = build_dir(root, build)
    manifest = _read_manifest(directory)
    if manifest is None:
        raise errors.TranscriptionError(
            errors.WHISPER_CPP_CORRUPTED, f"{build.id}: no manifest"
        )
    listed = {relative: digest for relative, _s, digest in manifest["files"]}
    unlisted = unlisted_code_files(directory, listed)
    if unlisted:
        # The basename only: the folder may name the Windows user.
        raise errors.TranscriptionError(
            errors.WHISPER_CPP_CORRUPTED,
            f"{os.path.basename(unlisted[0])}: code file not in the manifest",
        )
    pinned = dict(build.pinned_files)
    for relative, digest in pinned.items():
        if listed.get(relative) != digest:
            raise errors.TranscriptionError(
                errors.WHISPER_CPP_CORRUPTED,
                f"{relative}: not in the manifest, or not the pinned digest",
            )
    if pinned and manifest["executable"] not in pinned:
        raise errors.TranscriptionError(
            errors.WHISPER_CPP_CORRUPTED, f"{manifest['executable']}: not a pinned file"
        )
    for relative, manifest_digest in listed.items():
        expected = pinned.get(relative, manifest_digest)
        path = os.path.join(directory, *relative.split("/"))
        try:
            info = os.stat(path)
        except OSError as exc:
            raise errors.TranscriptionError(
                errors.WHISPER_CPP_CORRUPTED, f"{relative}: {exc}"
            ) from exc
        key = (path, info.st_size, info.st_mtime_ns, expected)
        if key in _verified_files:
            continue
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise errors.TranscriptionError(
                errors.WHISPER_CPP_CORRUPTED, f"{relative}: {exc}"
            ) from exc
        if digest.hexdigest() != expected:
            raise errors.TranscriptionError(
                errors.WHISPER_CPP_CORRUPTED,
                f"{relative}: sha256 {digest.hexdigest()}, expected {expected}",
            )
        _verified_files.add(key)


# ── Install, verify, remove ──────────────────────────────────────────────────


def install_build(build, root=None, progress=None, should_cancel=None, session=None,
                  compute_capability=None):
    """Put `build` on this machine. Returns the path of its whisper-cli.exe.

    The CUDA build is refused (CUDA_UNAVAILABLE) unless `compute_capability`
    is one `cuda_build_supported()` accepts: the rule is enforced where the
    671 MB would be spent, not only where the offer is drawn, so no caller can
    download it for a card it cannot run on.

    `progress(done, total)` counts the download and the extraction as one bar
    of twice the zip's size (the extraction is measured in compressed bytes),
    reported per 1 MB — the UI has to throttle it. `should_cancel()` is asked
    between chunks and while waiting for the lock; `session` is the tests' way
    in without a network.
    """
    _refuse_unsupported(build, compute_capability)
    root = _resolve(root)
    root_created = not os.path.isdir(root)
    try:
        with model_store.hold_directory_lock(root, should_cancel):
            # Asked under the lock: another account may have just finished it.
            if installation_state(build, root).state != STATE_INSTALLED:
                _install_locked(build, root, root_created, progress, should_cancel,
                                session)
    except LockTimeout as exc:
        raise errors.TranscriptionError(errors.WHISPER_CPP_BUSY, str(exc)) from exc
    executable = executable_path(build, root)
    if executable is None:
        raise errors.TranscriptionError(
            errors.WHISPER_CPP_CORRUPTED, f"{build.id}: not installed after the install"
        )
    logging.info("[transcription] whisper.cpp %s %s is installed", build.id, RELEASE_TAG)
    return executable


def repair_build(build, root=None, progress=None, should_cancel=None, session=None,
                 compute_capability=None):
    """Remove `build` and install it again, under one hold of the lock.

    The way out of the state install_build() trusts: every file the right size
    and the bytes wrong, which only verify_build() can see. The CUDA gate is
    asked first, so a refused repair removes nothing.
    """
    _refuse_unsupported(build, compute_capability)
    root = _resolve(root)
    try:
        with model_store.hold_directory_lock(root, should_cancel):
            remaining = remove_build(build, root, should_cancel)
            if remaining:
                # A whisper-cli still running holds its files open.
                raise errors.TranscriptionError(
                    errors.WHISPER_CPP_BUSY,
                    f"{build.id}: still open: {', '.join(remaining)}",
                )
            return install_build(build, root, progress, should_cancel, session,
                                 compute_capability)
    except LockTimeout as exc:
        raise errors.TranscriptionError(errors.WHISPER_CPP_BUSY, str(exc)) from exc


def verify_build(build, root=None, progress=None, should_cancel=None) -> None:
    """The expensive check: every installed file's sha256 against the manifest."""
    root = _resolve(root)
    state = installation_state(build, root)
    if state.state != STATE_INSTALLED:
        raise errors.TranscriptionError(
            errors.WHISPER_CPP_CORRUPTED,
            f"{build.id}: missing or wrong size: {', '.join(state.missing)}",
        )
    directory = build_dir(root, build)
    files = _read_manifest(directory)["files"]
    total = sum(size for _relative, size, _digest in files)
    done = 0
    for relative, _size, expected in files:
        digest = hashlib.sha256()
        try:
            with open(os.path.join(directory, *relative.split("/")), "rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                    _check_cancel(should_cancel)
                    digest.update(chunk)
                    done += len(chunk)
                    _report(progress, done, total)
        except OSError as exc:
            raise errors.TranscriptionError(
                errors.WHISPER_CPP_CORRUPTED, f"{relative}: {exc}"
            ) from exc
        if digest.hexdigest() != expected:
            raise errors.TranscriptionError(
                errors.WHISPER_CPP_CORRUPTED,
                f"{relative}: sha256 {digest.hexdigest()}, expected {expected}",
            )


def remove_build(build, root=None, should_cancel=None) -> tuple:
    """Delete `build`'s files — those its manifest names — and its leftovers.

    Returns the relative names still on disk afterwards: a whisper-cli that is
    running holds its own files open on Windows. Never a recursive delete of
    whatever is there; folders go through os.rmdir(), which refuses one that
    still holds anything.
    """
    root = _resolve(root)
    try:
        with model_store.hold_directory_lock(root, should_cancel):
            directory = build_dir(root, build)
            manifest = _read_manifest(directory)
            names = [relative for relative, _s, _d in manifest["files"]] if manifest else []
            remaining = _remove_listed(directory, names + [MANIFEST_FILENAME])
            _sweep_leftovers(root, build)
            _sweep_aside(root, build)
            return remaining
    except LockTimeout as exc:
        raise errors.TranscriptionError(errors.WHISPER_CPP_BUSY, str(exc)) from exc


# ── Internals ────────────────────────────────────────────────────────────────


def _resolve(root) -> str:
    # Normalised, so the aside-folder guard (_discard_aside compares a parent
    # against this exact string) holds however a caller spells the root —
    # a trailing separator included.
    return os.path.normpath(os.path.abspath(str(root) if root else default_runtime_dir()))


def _refuse_unsupported(build, compute_capability) -> None:
    if build.uses_cuda and not cuda_build_supported(compute_capability):
        raise errors.TranscriptionError(
            errors.CUDA_UNAVAILABLE,
            f"{build.id}: not for compute capability {compute_capability}",
        )


def _staging_dir(root, build) -> str:
    # The tag is in the name so a crash under one pin never leaves a folder the
    # next pin would extract on top of.
    return os.path.join(root, f"{build.id}-{RELEASE_TAG}{_PART_SUFFIX}")


def _leftovers(root, build) -> bool:
    paths = (_staging_dir(root, build), os.path.join(root, build.archive),
             os.path.join(root, build.archive + _PART_SUFFIX))
    return any(os.path.exists(path) for path in paths)


def _install_locked(build, root, root_created, progress, should_cancel, session):
    total = build.archive_bytes * 2
    model_store.ensure_free_space(root, build.archive_bytes * INSTALL_SPACE_FACTOR)
    archive_path = os.path.join(root, build.archive)
    staging = _staging_dir(root, build)
    owned_session = session is None
    # Through tls_trust, so a machine whose HTTPS is intercepted locally (an
    # antivirus, a proxy) can download at all — see that module.
    session = tls_trust.create_session() if owned_session else session
    try:
        os.makedirs(root, exist_ok=True)
        _sweep_leftovers(root, build)
        _sweep_aside(root, build)
        logging.info("[transcription] downloading whisper.cpp %s %s (%d bytes)",
                     build.id, RELEASE_TAG, build.archive_bytes)
        model_store.download_verified_file(
            session, build.url, root, build.archive, build.archive_bytes,
            build.archive_sha256,
            progress=lambda done, _total: _report(progress, done, total),
            should_cancel=should_cancel,
        )
        files, executable = _extract(build, archive_path, staging, root, total,
                                     progress, should_cancel)
        _write_manifest(staging, build, files, executable)
        _publish(root, build, staging)
        _unlink(archive_path)
        _report(progress, total, total)
    except Exception as exc:
        _sweep_leftovers(root, build)
        if root_created:
            _remove_empty_dir(root)
        raise _as_runtime_error(exc, build) from exc
    finally:
        if owned_session:
            session.close()


def _extract(build, archive_path, staging, root, total, progress, should_cancel):
    """Unpack the verified zip into `staging`. Returns (files, executable)."""
    with zipfile.ZipFile(archive_path) as archive:
        members = []
        for info in archive.infolist():
            if info.is_dir():
                continue
            relative = safe_member_path(info.filename)
            if relative is None or (info.external_attr >> 16) & _S_IFMT == _S_IFLNK:
                raise errors.TranscriptionError(
                    errors.WHISPER_CPP_CORRUPTED,
                    f"{build.archive}: refusing member {info.filename!r}",
                )
            members.append((info, relative))
        executable = locate_executable([relative for _info, relative in members])
        if executable is None:
            raise errors.TranscriptionError(
                errors.WHISPER_CPP_CORRUPTED, f"{build.archive}: no {EXECUTABLE_NAME}"
            )
        # The exact figure, now that the zip can say it.
        model_store.ensure_free_space(root, sum(info.file_size for info, _r in members))

        files = []
        done = build.archive_bytes
        for info, relative in members:
            _check_cancel(should_cancel)
            target = os.path.join(staging, *relative.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            digest = hashlib.sha256()
            written = 0
            ratio = info.compress_size / max(1, info.file_size)
            with archive.open(info) as source, open(target, "wb") as sink:
                for chunk in iter(lambda: source.read(_CHUNK_BYTES), b""):
                    _check_cancel(should_cancel)
                    sink.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    _report(progress, min(total, done + int(written * ratio)), total)
                sink.flush()
                # The publish is a rename, and NTFS journals the rename, not
                # the data: without this a power cut can leave a file of the
                # right size full of zeros, which the size check believes.
                os.fsync(sink.fileno())
            if written != info.file_size:
                raise errors.TranscriptionError(
                    errors.WHISPER_CPP_CORRUPTED,
                    f"{relative}: expected {info.file_size} bytes, got {written}",
                )
            done += info.compress_size
            files.append((relative, written, digest.hexdigest()))
    return files, executable


def _publish(root, build, staging) -> None:
    """Put the extracted tree in place of whatever the build folder holds.

    The old folder — an earlier pin, an interrupted install, or one holding a
    file no manifest names — is renamed aside first and discarded after, so
    nothing in it can block the new install, and a failed publish puts it back.
    """
    directory = build_dir(root, build)
    aside = None
    if os.path.exists(directory):
        aside = _aside_path(root, build)
        _replace(directory, aside)
    try:
        _replace(staging, directory)
    except BaseException:
        if aside is not None:
            try:
                os.replace(aside, directory)
            except OSError:
                pass
        raise
    if aside is not None:
        _discard_aside(root, build, aside)


def _replace(source, target) -> None:
    """os.replace(), retried briefly while something holds a file open."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_PAUSE_SECONDS)


def _aside_path(root, build) -> str:
    number = 1
    while os.path.exists(os.path.join(root, f"{build.id}.old-{number}")):
        number += 1
    return os.path.join(root, f"{build.id}.old-{number}")


def _discard_aside(root, build, path) -> None:
    """Delete a folder this module renamed aside — and refuse anything else.

    The one recursive delete here, and bounded by construction: only a folder
    named `<build id>.old-<n>` directly inside the runtime root, which only
    _publish() creates, out of a build folder that is WinZapp's own. What will
    not go (a file a running program holds) stays until the next install or
    removal sweeps it.
    """
    pattern = re.escape(build.id) + r"\.old-\d+"
    if os.path.dirname(path) != root or not re.fullmatch(pattern, os.path.basename(path)):
        raise ValueError(f"not an aside folder of {build.id}: {path}")
    shutil.rmtree(path, ignore_errors=True)


def _sweep_aside(root, build) -> None:
    """Discard every aside folder an earlier publish could not finish removing."""
    for path in glob.glob(os.path.join(glob.escape(root), glob.escape(build.id) + ".old-*")):
        try:
            _discard_aside(root, build, path)
        except ValueError:
            continue


def _write_manifest(staging, build, files, executable) -> None:
    manifest = {
        "release": RELEASE_TAG,
        "build": build.id,
        "archive_sha256": build.archive_sha256,
        "executable": executable,
        "files": [list(entry) for entry in files],
    }
    with open(os.path.join(staging, MANIFEST_FILENAME), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1)
        handle.flush()
        os.fsync(handle.fileno())


def _read_manifest(directory):
    """The manifest in `directory`, or None if absent, unreadable or malformed."""
    try:
        path = os.path.join(directory, MANIFEST_FILENAME)
        with open(path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        files = [
            (str(relative), int(size), str(digest))
            for relative, size, digest in manifest["files"]
        ]
        executable = str(manifest["executable"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # A manifest is only believed about names it could have written itself.
    if any(safe_member_path(relative) != relative for relative, _s, _d in files):
        return None
    if executable not in {relative for relative, _s, _d in files}:
        return None
    manifest["files"] = files
    return manifest


def _sweep_leftovers(root, build) -> None:
    """Remove the zip, its `.part` and the staging folder of `build`, if any.

    The staging folder's names are read off the zip when the zip is there; a
    folder of ours that still holds something afterwards is left, by rmdir's
    own refusal, rather than walked and emptied blind.
    """
    archive_path = os.path.join(root, build.archive)
    staging = _staging_dir(root, build)
    if os.path.isdir(staging):
        names = []
        try:
            with zipfile.ZipFile(archive_path) as archive:
                names = [safe_member_path(info.filename) for info in archive.infolist()
                         if not info.is_dir()]
        except (OSError, zipfile.BadZipFile):
            pass
        _remove_listed(staging, [name for name in names if name] + [MANIFEST_FILENAME])
    _unlink(archive_path)
    _unlink(archive_path + _PART_SUFFIX)


def _remove_listed(directory, relative_names) -> tuple:
    """Unlink the listed files under `directory`, then every emptied folder.

    Returns the names that are still there afterwards.
    """
    remaining = []
    folders = set()
    for relative in dict.fromkeys(relative_names):
        path = os.path.join(directory, *relative.split("/"))
        _unlink(path)
        if os.path.exists(path):
            remaining.append(relative)
        parent = os.path.dirname(path)
        while len(parent) > len(directory):
            folders.add(parent)
            parent = os.path.dirname(parent)
    # Deepest first, so a parent is only tried once its children are gone.
    for folder in sorted(folders, key=len, reverse=True):
        _remove_empty_dir(folder)
    _remove_empty_dir(directory)
    return tuple(remaining)


def _as_runtime_error(exc, build):
    """Whatever went wrong, as one of this module's codes."""
    if isinstance(exc, errors.TranscriptionError):
        if exc.code == errors.MODEL_CORRUPTED:
            # download_verified_file() speaks for the models; here the bytes
            # that failed their digest are the program's.
            return errors.TranscriptionError(errors.WHISPER_CPP_CORRUPTED, exc.detail)
        return exc
    if isinstance(exc, zipfile.BadZipFile):
        return errors.TranscriptionError(
            errors.WHISPER_CPP_CORRUPTED, f"{build.archive}: {exc}"
        )
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return errors.TranscriptionError(errors.NO_DISK_SPACE, f"{build.id}: {exc}")
    if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM):
        # A file held open — an antivirus scanning the fresh DLLs, or a
        # whisper-cli still running — that outlasted _replace()'s retries.
        # "Check your connection" would send the user to fetch it all again,
        # and the connection is fine.
        return errors.TranscriptionError(errors.WHISPER_CPP_BUSY, f"{build.id}: {exc}")
    return errors.TranscriptionError(
        errors.WHISPER_CPP_DOWNLOAD_FAILED, f"{build.id}: {exc}"
    )
