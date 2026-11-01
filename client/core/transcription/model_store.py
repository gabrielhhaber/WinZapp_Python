"""Where the Whisper models live on disk, and how they get there.

The catalogue (see model_catalog) says what a model is; this module is the only
thing that puts those bytes on a disk, checks them, and takes them away again —
for the whisper.cpp GGML files too (whisper_cpp_catalog entries answer the same
names; see whisper_cpp_store).
These decisions are worth more than the code implementing them:

* **The root is global, not per account.** WinZapp runs one account per
  process, so anything under ``data_path()`` is downloaded once *per account* —
  and large-v3 is 3 GB. The weights are read-only and hold nothing
  account-specific, so ``default_models_dir()`` answers with ``global_dir()``.
  Every other function takes the root as an argument, because part 5 lets the
  user point it somewhere else entirely.

* **Being global means being shared, so every mutation takes a lock.** Two
  account processes asking for the same model would otherwise open the same
  ``model.bin.part``, interleave their writes, both fail the digest, and each
  one's cleanup would delete the file the other is still streaming into.
  ``coord_locks.models_lock()`` is that lock; it is keyed on the *canonical*
  root, which is also how two spellings of one directory are recognised as one
  (Windows folds case and resolves junctions and ``subst`` drives — ``abspath``
  does not).

* **A file under its final name is a promise.** "Is this model installed?" is
  asked every time the model list is drawn and before every transcription, so
  it can only afford existence plus an exact size — 3 GB of hashing cannot run
  on that path. Which means a file carrying the catalogue's name and size
  *will* be believed, so nothing may ever appear under its final name before it
  has been verified — and the data has to be on the platter before the rename,
  since NTFS journals the rename and not the bytes.

* **The digest is computed while the bytes go past**, and re-seeded from the
  ``.part`` already on disk when a transfer resumes. model.bin is up to 3 GB;
  hashing it in a second pass would double the I/O of every download for a
  number the transfer already had in its hands, and re-fetching it because a
  user cancelled at 90% is how a feature gets used exactly once.

* **Only the catalogue's own file names are ever deleted.** ``remove_model()``
  unlinks exactly those names and then calls ``os.rmdir()``, which refuses a
  directory still holding anything else. Never ``shutil.rmtree()``: the root is
  user-chosen, somebody will point it at a folder that already has their own
  files in it, and a recursive delete of a user-chosen path only has to be
  wrong once.

**A note for the UI layer on the progress callbacks.** ``progress(done, total)``
is reported per chunk, which is every 1 MB — around 3000 calls for large-v3, and
`total` means something different per call site: the whole model for
``download_model()``, the digested files for ``verify_model()`` (model.bin for
a faster-whisper model, the one .bin for a whisper.cpp file), and every file
being moved for ``move_models()``. Part 5 has to throttle before any
``wx.CallAfter`` or spoken percentage; forwarding these straight through would
flood the UI thread and have the screen reader read thousands of numbers.

No user-facing text lives here either: failures travel as TranscriptionError
codes, and turning one into a sentence is the UI layer's job.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass

import requests

from app_paths import global_dir
from coord_locks import LockTimeout, canonical_dir, models_lock
from core import tls_trust
from core.transcription import errors, model_catalog, whisper_cpp_catalog
from core.transcription._fileops import (
    check_cancel as _check_cancel,
    remove_empty_dir as _remove_empty_dir,
    report as _report,
    unlink as _unlink,
)

# Subdirectory of the global data dir holding every model.
MODELS_DIRNAME = "whisper_models"

_HF_FILE_URL = "https://huggingface.co/{repo}/resolve/{revision}/{filename}"

# What a model directory currently holds. INCOMPLETE is what an interrupted
# download leaves behind, and it is deliberately distinct from ABSENT: the
# picker offers a download for one and a repair for the other, and only
# INCOMPLETE can name the files that are wrong.
STATE_ABSENT = "absent"
STATE_INCOMPLETE = "incomplete"
STATE_INSTALLED = "installed"

_PART_SUFFIX = ".part"

# Big enough that a 3 GB transfer is not three million iterations, small enough
# that cancelling between chunks still feels immediate on a slow connection.
_CHUNK_BYTES = 1024 * 1024

# (connect, read) — the same pair the portable Node.js download uses. The read
# timeout has to survive a stalled CDN; it is per read, not for the transfer.
_HTTP_TIMEOUT = (30, 300)

# Room left on the volume beyond the transfer itself. Not politeness: the
# models directory normally shares a volume with messages.db, the media cache
# and log.log, all of which are being written while a multi-gigabyte download
# runs, so a download that "just fits" takes the rest of the app down with it.
_FREE_SPACE_SLACK_BYTES = 256 * 1024 * 1024

# How long to wait for another process to finish with the models directory, and
# in what slices. Sliced because NamedLock's own wait cannot be interrupted:
# between attempts is the only place a user's Cancel can be noticed, and being
# told "another window is busy" with a Cancel button that does nothing is worse
# than the wait itself.
#
# The user's Cancel is therefore the real bound on the wait, and the deadline
# below is only a backstop against a lock nobody will ever release. It is
# deliberately far past any plausible transfer: at five minutes it fired while
# another account was legitimately half way through a 3 GB download on a slow
# line, and reported it as a failure.
_LOCK_TIMEOUT_SECONDS = 12 * 60 * 60.0
_LOCK_POLL_SECONDS = 2.0


@dataclass(frozen=True)
class InstallState:
    """What a root currently holds for one model.

    `missing` names the files that are absent *or* the wrong size — one
    condition, not two, because the catalogue pins a revision and with it an
    exact size for every file, so "present but 4 KB short" is an interrupted
    download and nothing else.
    """

    state: str
    missing: tuple[str, ...] = ()
    # Bytes of this model already on disk and correct — what a resumed download
    # starts its progress bar at.
    present_bytes: int = 0


def default_models_dir() -> str:
    """Where models live unless the user has chosen somewhere else.

    Global rather than per account: see the module docstring.
    """
    return global_dir(MODELS_DIRNAME)


def model_dir(root, model_id) -> str:
    """The directory holding one model's files under `root`."""
    return os.path.join(root, str(model_id))


def file_url(model, filename) -> str:
    """The Hugging Face URL for one of `model`'s files.

    Always ``/resolve/<revision>/``, never ``/resolve/main/``. The sizes and the
    model.bin digest in the catalogue were measured at that commit, so a URL
    following the branch would start failing as MODEL_CORRUPTED the day
    upstream republishes — and, far worse, would silently accept *different*
    weights for any file whose size happened to be unchanged.
    """
    # An entry whose file sits in a subfolder of its repository (the noise
    # reduction model) names it here; the local file stays flat in the model
    # folder, where the rest of this module expects it.
    remote_path = getattr(model, "remote_path", None)
    return _HF_FILE_URL.format(
        repo=model.repo, revision=model.revision,
        filename=remote_path(filename) if remote_path else filename,
    )


def installation_state(root, model) -> InstallState:
    """The cheap answer to "is this model usable?" — names and exact sizes.

    No hashing at all, on purpose: this runs whenever the model list is drawn
    and before every transcription, and model.bin is up to 3 GB. verify_model()
    is the expensive answer, for the caller with a reason to pay for it.
    """
    directory = model_dir(root, model.id)
    missing = []
    present_bytes = 0
    found_any = False

    for name, size in model.files:
        try:
            actual = os.path.getsize(os.path.join(directory, name))
        except OSError:
            missing.append(name)
            continue
        found_any = True
        if actual != size:
            missing.append(name)
            continue
        present_bytes += size

    if not missing:
        return InstallState(STATE_INSTALLED, (), present_bytes)
    # "Nothing of it is here" is keyed on nothing of it being here, which
    # includes the `.part` files of a transfer that was interrupted before its
    # first file landed: zero correct bytes and a resumable 2 GB `.part` is an
    # interrupted download, not an absence, and the two offer the user
    # different buttons.
    if not found_any and not _has_parts(directory, model):
        return InstallState(STATE_ABSENT, tuple(missing), 0)
    return InstallState(STATE_INCOMPLETE, tuple(missing), present_bytes)


def is_installed(root, model) -> bool:
    """Whether every file of `model` is under `root` at its exact size."""
    return installation_state(root, model).state == STATE_INSTALLED


def list_installed(root) -> tuple[str, ...]:
    """Ids of the complete models under `root`, in the catalogue's own order."""
    return tuple(
        model.id for model in model_catalog.list_models() if is_installed(root, model)
    )


def find_entry(model_id):
    """The entry of either catalogue with this id, or None.

    One models root holds both backends' files — a faster-whisper folder per
    model and a `ggml-<name>` folder per GGML file, the voice-activity model
    included — so whatever walks or deletes inside the root has to know both.
    The ids cannot collide (the `ggml-` prefix; whisper_cpp_catalog pins it).
    """
    return model_catalog.get_model(model_id) or whisper_cpp_catalog.get_model(model_id)


def all_entries() -> tuple:
    """Every entry that can have a folder under a models root, in list order:
    the faster-whisper models, the GGML files, then the voice-activity model."""
    return (
        model_catalog.list_models()
        + whisper_cpp_catalog.list_models()
        + (whisper_cpp_catalog.VAD_MODEL,)
    )


def list_unknown_dirs(root) -> tuple[str, ...]:
    """Directory names under `root` that no catalogue entry claims.

    When a WinZapp update retires a model, whatever the user downloaded for it
    stops being listed anywhere — and, since remove_model() refuses to delete
    names it cannot look up in the catalogue, it also becomes undeletable from
    inside the app. Up to 3 GB, invisible. This is what lets part 5 show those
    folders and hand them to the user. Both catalogues count as known: the
    GGML files live in the same root (see find_entry()).
    """
    known = {model.id for model in all_entries()}
    try:
        entries = os.listdir(root)
    except OSError:
        return ()
    return tuple(
        sorted(
            name
            for name in entries
            if name not in known and os.path.isdir(os.path.join(root, name))
        )
    )


def ensure_ready(root, model_id) -> str:
    """The directory to load `model_id` from, or the right error code.

    What part 3 calls immediately before handing a path to CTranslate2. The
    cheap check only: MODEL_NOT_INSTALLED and MODEL_CORRUPTED are two different
    sentences for the user — download it, versus repair it — and choosing
    between them must not cost 3 GB of hashing before a transcription that has
    not even started.
    """
    model = model_catalog.get_model(model_id)
    if model is None:
        # A settings file naming a model this version dropped. "Not installed"
        # is both true and the one thing the UI can act on.
        raise errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, str(model_id))
    return ensure_model_ready(root, model)


def ensure_model_ready(root, model) -> str:
    """ensure_ready() for an entry already in hand, from either catalogue.

    The same cheap check and the same two codes; the whisper.cpp store looks
    its own ids up and then asks this, so "installed" means one thing for both
    backends.
    """
    state = installation_state(root, model)
    if state.state == STATE_ABSENT:
        raise errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, model.id)
    if state.state == STATE_INCOMPLETE:
        raise errors.TranscriptionError(
            errors.MODEL_CORRUPTED,
            f"{model.id}: missing or wrong size: {', '.join(state.missing)}",
        )
    return model_dir(root, model.id)


def free_bytes(path):
    """Free bytes on the volume `path` lives on, or None if unmeasurable.

    Walks up to the nearest existing ancestor, because the models directory
    does not exist yet the first time this is asked — and a "does it fit?" gate
    answering "unknown" because the folder has not been created yet is a gate
    that blocks the first download on every install.
    """
    candidate = os.path.abspath(path)
    while True:
        try:
            return int(shutil.disk_usage(candidate).free)
        except OSError:
            parent = os.path.dirname(candidate)
            if parent == candidate:
                return None
            candidate = parent


def required_free_bytes(needed_bytes) -> int:
    """Free bytes the gate below demands for a transfer of `needed_bytes`.

    The transfer plus the slack, as one public answer. Part 5c quotes this to
    the user before a download, and a figure worked out anywhere else is one
    that can say "it fits" about a transfer ensure_free_space() then refuses.
    """
    return int(needed_bytes) + _FREE_SPACE_SLACK_BYTES


def remaining_download_bytes(root, model) -> int:
    """Bytes download_model() still has to put on the disk for `model`.

    The whole model, less every file already complete *and* less the `.part`
    prefixes a resume will not fetch again — the figure the free-space gate
    counts, because this is what the gate calls. InstallState.present_bytes is
    not a substitute: it counts finished files only, and model.bin is 95-99% of
    a model, so a download interrupted at 90% would be quoted as the whole
    model again and a user with room for the remaining tenth told there is no
    space. Sizes only, never a hash, so part 5c can ask before offering.
    """
    directory = model_dir(root, model.id)
    done_bytes, pending = _download_plan(directory, model)
    return model.download_bytes - done_bytes - _resumable_bytes(directory, model, pending)


def ensure_free_space(root, needed_bytes) -> None:
    """Raise NO_DISK_SPACE unless `needed_bytes` fit under `root`, with slack.

    A volume nothing could be measured on passes: refusing the download because
    disk_usage() failed would make transcription unavailable on a setup that
    works perfectly well.
    """
    free = free_bytes(root)
    if free is None:
        logging.info(
            "[transcription] free space at %s is unknown; allowing the transfer",
            os.path.basename(os.path.normpath(root)),
        )
        return
    if free < required_free_bytes(needed_bytes):
        raise errors.TranscriptionError(
            errors.NO_DISK_SPACE,
            f"{root}: {needed_bytes} bytes needed plus "
            f"{_FREE_SPACE_SLACK_BYTES} of slack, {free} free",
        )


def download_model(model, root, progress=None, should_cancel=None, session=None):
    """Fetch every missing file of `model` into `root`, verified. Returns its dir.

    `progress(done, total)` counts the **whole model**, not the file in flight:
    the user wants one bar for "downloading large-v3", not six of them in a row.

    This repairs an **incomplete** model — files missing or the wrong size —
    and only that. Anything already there at its catalogued size is trusted on
    the size alone, exactly as installation_state() trusts it, so a model whose
    files are all the right size and whose weights are wrong is not repaired
    here: this call would fetch nothing and report success. repair_model() is
    the way out of that one, and verify_model() is what detects it.

    An interrupted file is picked up where it stopped, by asking for a byte
    range: model.bin is 95-99% of a model, so restarting it is restarting
    everything.

    `should_cancel()` is consulted between chunks and while waiting for the
    shared models lock; `session` exists so the download can be driven from a
    test without a network.
    """
    # Sampled before the lock, which may create the root itself on the
    # non-Windows fallback path.
    root_created = not os.path.isdir(root)
    try:
        with _hold_models_lock(root, should_cancel):
            return _download_locked(
                model, root, root_created, progress, should_cancel, session
            )
    except LockTimeout as exc:
        raise errors.TranscriptionError(errors.MODELS_BUSY, f"{model.id}: {exc}") from exc


def repair_model(model, root, progress=None, should_cancel=None, session=None):
    """Delete whatever is on disk for `model` and download it again.

    The only way out of the state download_model() cannot fix: every file the
    right size, model.bin's digest wrong (a silent corruption, or weights from
    a revision this catalogue never measured). installation_state() calls that
    installed and a re-download then fetches nothing at all, so without this the
    bad weights survive every attempt to replace them.

    Both halves run under one hold of the models lock — it is re-entrant within
    the process — so no other process can start a download into the directory
    between the delete and the fetch.
    """
    try:
        with _hold_models_lock(root, should_cancel):
            # By the entry, not by id: an id is looked up in the faster-whisper
            # catalogue, which would find nothing to delete for a whisper.cpp
            # file and leave the bad bytes in place for the download to trust.
            remove_model_files(root, model)
            return download_model(
                model,
                root,
                progress=progress,
                should_cancel=should_cancel,
                session=session,
            )
    except LockTimeout as exc:
        raise errors.TranscriptionError(errors.MODELS_BUSY, f"{model.id}: {exc}") from exc


def verify_model(root, model, progress=None, should_cancel=None) -> None:
    """The expensive check: every size, then the sha256 of every digested file.

    For a faster-whisper model that is model.bin alone, and for a whisper.cpp
    one its single .bin (see `sha256_of()`); `progress` counts those files
    together.

    Separate from installation_state() because it reads up to 3 GB, which is
    also why it takes a progress callback and a cancel check of its own: a user
    who asked to verify a model has to be able to change their mind, and to be
    told how far it got while they wait.
    """
    directory = ensure_model_ready(root, model)
    digested = [
        (name, size, model.sha256_of(name))
        for name, size in model.files
        if model.sha256_of(name)
    ]
    total = sum(size for _name, size, _expected in digested)
    done = 0
    for name, size, expected in digested:
        digest = _hash_file(
            os.path.join(directory, name), total, progress, should_cancel, done
        )
        done += size
        if digest != expected:
            raise errors.TranscriptionError(
                errors.MODEL_CORRUPTED,
                f"{model.id}: {name} sha256 {digest}, expected {expected}",
            )


def remove_model(root, model_id, should_cancel=None) -> bool:
    """Delete one model's files from `root` — and nothing else. True if any went.

    Only the names the catalogue lists (plus their `.part` leftovers) are
    unlinked, and the directory itself goes through os.rmdir(), which refuses a
    directory that still holds anything. See the module docstring for why this
    is never shutil.rmtree().

    False means "there was nothing to delete" and nothing else. Another process
    holding the models directory raises MODELS_BUSY instead: part 5 wires this
    to a delete button, and a user who is told nothing was removed has no way
    to know whether their 3 GB is still there. `should_cancel` is honoured while
    waiting for that lock, so the button's own Cancel works.
    """
    model = find_entry(model_id)
    if model is None:
        # An id the catalogue no longer knows is exactly the case where the
        # names to delete are unknown, so nothing here can be removed safely.
        # The folder is left alone; list_unknown_dirs() is how the user sees it.
        logging.info("[transcription] not removing unknown model id %s", model_id)
        return False
    return remove_model_files(root, model, should_cancel)


def remove_model_files(root, model, should_cancel=None) -> bool:
    """remove_model() for an entry already in hand, from either catalogue.

    Deletes exactly the names `model.files` lists, under the same lock and
    with the same rmdir — what makes remove_model() safe is that it never
    deletes a name it did not look up, and an entry is that lookup.
    """
    try:
        with _hold_models_lock(root, should_cancel):
            directory = model_dir(root, model.id)
            removed = False
            for name, _size in model.files:
                removed = _unlink(os.path.join(directory, name)) or removed
                removed = _unlink(os.path.join(directory, name + _PART_SUFFIX)) or removed
            _remove_empty_dir(directory)
            return removed
    except LockTimeout as exc:
        raise errors.TranscriptionError(errors.MODELS_BUSY, f"{model.id}: {exc}") from exc


def move_models(old_root, new_root, progress=None, should_cancel=None):
    """Move the models from `old_root` to `new_root`. Returns the ids moved.

    Copy, verify, then delete — one model at a time, and deliberately not a
    per-file ``shutil.move()``. A move empties the source as it fills the
    destination, so an interruption half way through leaves *both* roots
    incomplete and the user with no usable model at all; copying into `.part`
    names, publishing them only once every size matches, and deleting the
    source afterwards means each model is whole in exactly one of the two roots
    at every instant. The price is both copies on disk at once, which is what
    the free-space gate is measured against — and why a same-volume move is
    tried as a single rename first, where it is instant and costs nothing.

    The copy is chunked here rather than handed to shutil.copy2() so that it can
    be cancelled and can report progress: a 3 GB copy is a minute or more of
    silence, which for a screen-reader user is indistinguishable from a hang.

    Incomplete models travel too, and arrive as incomplete as they left: left
    behind, they would sit in a directory no screen in part 5 ever looks at
    again. (Their `.part` files ride along only on the rename path, which moves
    the whole directory; the copy path is per catalogued file, and the leftovers
    go with the source.)
    """
    if canonical_dir(old_root) == canonical_dir(new_root):
        # Two spellings of one directory. Windows folds case and resolves
        # junctions and `subst` drives; os.path.abspath does neither, so
        # "...\\Whisper_Models" and "...\\whisper_models" would pass a string
        # comparison, be found "already at the destination" — because they ARE
        # the destination — and be deleted as duplicates of themselves.
        logging.info("[transcription] models root unchanged (%s); nothing to move",
                     new_root)
        return ()

    root_created = not os.path.isdir(new_root)
    # Locked in a fixed order, so two processes moving in opposite directions
    # cannot each hold the other's root.
    first, second = sorted((old_root, new_root), key=canonical_dir)
    try:
        with _hold_models_lock(first, should_cancel), _hold_models_lock(
            second, should_cancel
        ):
            return _move_locked(
                old_root, new_root, root_created, progress, should_cancel
            )
    except LockTimeout as exc:
        raise errors.TranscriptionError(errors.MODELS_BUSY, str(exc)) from exc


def hold_directory_lock(directory, should_cancel=None):
    """The cross-process lock this module takes, keyed on `directory`.

    For the other stores of the package that write into a shared folder of
    their own (the whisper.cpp runtime): the same sliced, cancellable wait,
    rather than a third copy of it. LockTimeout is the caller's to translate,
    since only the caller knows which folder its "busy" sentence names.
    """
    return _hold_models_lock(directory, should_cancel)


def download_verified_file(session, url, directory, name, expected_bytes,
                           expected_sha256, progress=None, should_cancel=None):
    """Fetch `url` into `directory/name`, published only once size and digest match.

    The single-file, from-byte-0 form of download_model(), for an artifact that
    is not hosted on Hugging Face (the whisper.cpp release zips). Through
    `_write_part()`, so the "a final name has been verified" rule is the same
    code here as for the models. A failure raises MODEL_CORRUPTED for a size or
    digest mismatch and leaves the `.part` behind; the caller sweeps it and
    picks the code its user hears.
    """
    response = session.get(url, stream=True, timeout=_HTTP_TIMEOUT)
    try:
        response.raise_for_status()
        return _write_part(
            directory,
            name,
            response.iter_content(chunk_size=_CHUNK_BYTES),
            expected_bytes,
            expected_sha256,
            0,
            expected_bytes,
            progress,
            should_cancel,
        )
    finally:
        response.close()


# ── Internals ────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _hold_models_lock(root, should_cancel=None):
    """Hold the cross-process models lock, still answering the Cancel button.

    Waited for in short slices rather than one long block: NamedLock's own wait
    is not interruptible, and between attempts is the only place a user who has
    been told "another window is downloading" can give up. The cancel check sits
    on the LockTimeout branch alone, so an uncontended lock is not slowed by it
    and a contended one notices the first cancellation at t≈2 s — which is the
    responsiveness this is for, not a deadline anyone can measure.

    LockTimeout is left to the caller, which knows which of its codes a failed
    wait belongs to; for every caller here that is MODELS_BUSY.
    """
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        # The lock FILE goes in the global dir, never inside the models root:
        # the flock fallback creates it, and a file of ours inside a folder the
        # user chose is both litter and the reason an empty-root cleanup would
        # stop working.
        lock = models_lock(root, global_dir(), timeout=_LOCK_POLL_SECONDS)
        try:
            lock.acquire()
            break
        except LockTimeout:
            _check_cancel(should_cancel)
            if time.monotonic() >= deadline:
                raise
    try:
        yield lock
    finally:
        lock.release()


def _download_locked(model, root, root_created, progress, should_cancel, session):
    """download_model()'s body, with the models lock already held."""
    directory = model_dir(root, model.id)
    total = model.download_bytes
    done_bytes, pending = _download_plan(directory, model)

    _report(progress, done_bytes, total)
    if not pending:
        # A complete model can still be sitting next to the `.part` of an older
        # attempt at a file that has since arrived — up to 3 GB of dead weight
        # that nothing else on this path would ever sweep.
        _remove_parts(directory, model)
        return directory

    # Before a single byte is fetched: the point of the gate is to fail while
    # the user can still pick a smaller model, not half way into the big one.
    # What a resume will not re-fetch is already on the disk, so it is not
    # counted again. Through remaining_download_bytes() rather than worked out
    # here, so the figure part 5c quotes before the download is this one; it
    # re-stats a handful of files, under the lock, which is nothing.
    ensure_free_space(root, remaining_download_bytes(root, model))

    created_dir = not os.path.isdir(directory)
    owned_session = session is None
    # Through tls_trust, so a machine whose HTTPS is intercepted locally (an
    # antivirus, a proxy) can download the models at all — see that module.
    session = tls_trust.create_session() if owned_session else session
    try:
        try:
            os.makedirs(directory, exist_ok=True)
            for name, size in pending:
                _check_cancel(should_cancel)
                logging.info(
                    "[transcription] downloading %s/%s (%d bytes)", model.id, name, size
                )
                done_bytes = _download_file(
                    session, model, directory, name, size,
                    done_bytes, total, progress, should_cancel,
                )
        except Exception as exc:
            failure = _as_transcription_error(exc, model.id, errors.MODEL_DOWNLOAD_FAILED)
            # A prefix the server disowned is the one thing that must not be
            # resumed from; every other interruption — a cancellation, a
            # dropped connection — leaves a valid prefix, and keeping it is
            # what makes cancelling at 90% cost 10% next time.
            if failure.code == errors.MODEL_CORRUPTED:
                _remove_parts(directory, model)
            if created_dir:
                _remove_empty_dir(directory)
            if root_created:
                # Nothing of the app's may be left in a folder the user chose
                # and the app then failed to use.
                _remove_empty_dir(root)
            raise failure
    finally:
        if owned_session:
            session.close()

    logging.info("[transcription] model %s is installed at %s", model.id, directory)
    return directory


def _move_locked(old_root, new_root, root_created, progress, should_cancel):
    """move_models()'s body, with both roots' locks already held."""
    # Every model with anything of it under the old root, complete or not.
    plans = []
    # Both catalogues: the GGML files share the root, and one left behind
    # would sit where nothing lists it (see find_entry()).
    for model in all_entries():
        present = _present_files(old_root, model)
        if present:
            plans.append((model, present))

    total = sum(size for _model, present in plans for _name, size in present)
    done = 0
    _report(progress, done, total)

    # Filled in as each model lands, so that a failure part way through can
    # still say which ones crossed: every model is whole in exactly one root
    # either way, but "three of your five are in the new folder" is the only
    # thing part 5 can tell the user, and a bare error code loses it.
    moved = []
    try:
        _move_each(
            plans, old_root, new_root, done, total, progress, should_cancel, moved
        )
    except Exception as exc:
        if root_created:
            # Nothing of the app's may be left in a folder the user chose and
            # the app then failed to fill.
            _remove_empty_dir(new_root)
        failure = _as_transcription_error(exc, new_root, errors.MODEL_MOVE_FAILED)
        carried = errors.TranscriptionError(
            failure.code,
            f"{failure.detail}; moved before the failure: {', '.join(moved) or 'none'}",
        )
        # Also as data, not only as log text: `detail` is log-facing by design,
        # so a UI that had to parse it would be reading a string this module
        # promises nothing about.
        carried.moved = tuple(moved)
        raise carried from exc

    # The old root itself is left alone even when it ends up empty: it may be a
    # folder the user made and pointed WinZapp at, and moving files out of a
    # directory is not permission to delete it.
    return tuple(moved)


def _move_each(plans, old_root, new_root, done, total, progress, should_cancel, moved):
    """Move each planned model, in catalogue order, appending ids to `moved`.

    The caller owns that list so that it survives an exception raised half way
    through — a returned value would not.
    """
    for model, present in plans:
        _check_cancel(should_cancel)
        model_bytes = sum(size for _name, size in present)
        source = model_dir(old_root, model.id)
        destination = model_dir(new_root, model.id)

        if canonical_dir(source) == canonical_dir(destination):
            # One directory under two names: it is already where it is going,
            # and above all there is nothing here to delete.
            done += model_bytes
            _report(progress, done, total)
            moved.append(model.id)
            continue

        if is_installed(new_root, model):
            # A complete copy is already at the destination, so the source one
            # is pure duplication of up to 3 GB — which is what the user asked
            # to be rid of by moving the folder in the first place.
            remove_model_files(old_root, model)
            done += model_bytes
            _report(progress, done, total)
            moved.append(model.id)
            continue

        if not os.path.exists(destination) and _try_rename(source, destination):
            done += model_bytes
            _report(progress, done, total)
            moved.append(model.id)
            logging.info("[transcription] renamed model %s into %s", model.id, new_root)
            continue

        # Only the copy path needs room: the rename above moved nothing.
        ensure_free_space(new_root, model_bytes)
        done = _copy_model(
            model, present, old_root, new_root, done, total, progress, should_cancel
        )
        remove_model_files(old_root, model)
        moved.append(model.id)
        logging.info("[transcription] copied model %s to %s", model.id, new_root)


def _present_files(root, model):
    """[(name, size on disk)] for the catalogued files actually in `root`.

    Sizes come from the disk rather than from the catalogue because a
    half-downloaded file has to move too, and has to arrive as the same half —
    the destination must end in the state the source was in, or the move turns
    an incomplete model into a corrupted-looking one.
    """
    directory = model_dir(root, model.id)
    present = []
    for name, _size in model.files:
        try:
            present.append((name, os.path.getsize(os.path.join(directory, name))))
        except OSError:
            continue
    return present


def _try_rename(source, destination) -> bool:
    """Move a whole model directory with one rename, or say it is not possible.

    Same-volume is the common case (the user picks another folder on the same
    disk), and there this is atomic, instant and free — without it, moving 5 GB
    from D:\\a to D:\\b would be refused on a disk with 5.4 GB free for a
    transfer that needs no space at all. Any OSError means "not this way":
    EXDEV across volumes, but also a destination that will not take it.
    """
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        os.rename(source, destination)
        return True
    except OSError as exc:
        logging.info(
            "[transcription] cannot rename %s to %s (%s); copying instead",
            source, destination, errors.scrub_media_names(str(exc)),
        )
        return False


def _copy_model(model, present, old_root, new_root, done, total, progress, should_cancel):
    """Copy one model's files from one root to the other, verified by size."""
    source = model_dir(old_root, model.id)
    directory = model_dir(new_root, model.id)
    created_dir = not os.path.isdir(directory)
    try:
        os.makedirs(directory, exist_ok=True)
        for name, size in present:
            _check_cancel(should_cancel)
            if _file_complete(os.path.join(directory, name), size):
                done += size
                _report(progress, done, total)
                continue
            done = _write_part(
                directory,
                name,
                _read_chunks(os.path.join(source, name)),
                size,
                # No hashing on a local copy: the source was verified when it
                # was downloaded, and re-reading 3 GB to prove that a
                # filesystem copied it correctly would double the move.
                None,
                done,
                total,
                progress,
                should_cancel,
            )
    except Exception as exc:
        _remove_parts(directory, model)
        if created_dir:
            _remove_empty_dir(directory)
        raise _as_transcription_error(exc, model.id, errors.MODEL_MOVE_FAILED)
    return done


def _file_complete(path, expected_bytes) -> bool:
    """Whether `path` exists at exactly `expected_bytes`."""
    try:
        return os.path.getsize(path) == expected_bytes
    except OSError:
        return False


def _has_parts(directory, model) -> bool:
    """Whether an interrupted transfer left any `.part` of `model` here."""
    return any(
        os.path.exists(os.path.join(directory, name + _PART_SUFFIX))
        for name, _size in model.files
    )


def _download_plan(directory, model):
    """(bytes already complete, [(name, size) still to fetch]) for `model`."""
    done_bytes = 0
    pending = []
    for name, size in model.files:
        if _file_complete(os.path.join(directory, name), size):
            done_bytes += size
            continue
        pending.append((name, size))
    return done_bytes, pending


def _resumable_bytes(directory, model, pending) -> int:
    """Bytes of `pending` already on disk as a `.part` a resume will keep.

    Only the files _download_file() actually resumes — the ones with a digest
    (`sha256_of()`: model.bin, or a whisper.cpp file). An auxiliary file's
    `.part` is fetched again from byte 0 however long it is, so counting it
    here would quote the free-space gate less than the transfer is about to
    write.
    """
    return sum(
        _resume_offset(os.path.join(directory, name + _PART_SUFFIX), size)
        for name, size in pending
        if model.sha256_of(name)
    )


def _resume_offset(part_path, expected_bytes) -> int:
    """How much of `part_path` a byte range may pick up from.

    Only a strict prefix qualifies. A `.part` at or past the expected size is
    not a prefix of anything — a server that sent too much, or a leftover from
    a revision this catalogue never measured — and resuming from it would build
    a file of exactly the right length out of the wrong bytes, which nothing
    but the digest would catch, and only for model.bin.
    """
    try:
        size = os.path.getsize(part_path)
    except OSError:
        return 0
    return size if 0 < size < expected_bytes else 0


def _download_file(session, model, directory, name, expected_bytes,
                   done_bytes, total, progress, should_cancel) -> int:
    """One file of `model`, streamed into place. Returns the new byte count."""
    part_path = os.path.join(directory, name + _PART_SUFFIX)
    # The catalogue entry answers: for a faster-whisper model only model.bin
    # has a digest, for a whisper.cpp file its single .bin does.
    expected_sha256 = model.sha256_of(name)

    # Only a file with a digest may be resumed, and that is the whole rule.
    # For the auxiliary files the only check is `written == expected_bytes`,
    # which a `.part` that is NOT a prefix of the current file satisfies
    # exactly: splice a stale 1 KB head onto the rest and the result has the
    # catalogued size, so installation_state, is_installed, verify_model and
    # ensure_ready all call the model healthy — verify_model included, because
    # it only hashes model.bin — and CTranslate2 then dies on the tokenizer
    # with nothing but "internal error" to show the user. A user's own
    # cancellation always leaves a valid prefix, but a power cut or a kill does
    # not (fsync only runs at the end of _write_part, so NTFS can leave a
    # `.part` longer than the durable data, with a tail of zeros), and neither
    # does a catalogue revision that changed between the interruption and the
    # resume. model.bin is self-healing under the same accident because the
    # digest catches it and the failure sweeps the `.part`; the others have
    # nothing to catch it with. The cost of the rule is nil: on large-v3,
    # model.bin is 3,087,284,237 of 3,090,835,702 bytes, so this keeps 99.885%
    # of what resuming exists to keep and removes the class entirely.
    #
    # The prefix is hashed *before* the request, so the range asked for is the
    # range actually accounted for. Hashing it afterwards would leave the
    # digest and the offset free to disagree whenever the local read fell short.
    resume_from = _resume_offset(part_path, expected_bytes) if expected_sha256 else 0
    digest, resume_from = _seed_from_part(
        part_path, resume_from, expected_sha256, should_cancel
    )

    headers = {"Range": f"bytes={resume_from}-"} if resume_from else None
    response = session.get(
        file_url(model, name), stream=True, timeout=_HTTP_TIMEOUT, headers=headers
    )
    try:
        response.raise_for_status()
        if resume_from and not _range_honoured(response, resume_from):
            # The range was ignored and the whole file is coming: appending it
            # to what is already there would produce a file too long, or — with
            # a stale prefix — the right length and the wrong bytes.
            logging.info(
                "[transcription] %s/%s: range not honoured (status %s), starting over",
                model.id, name, getattr(response, "status_code", "?"),
            )
            digest, resume_from = _seed_from_part(part_path, 0, expected_sha256, None)
        return _write_part(
            directory,
            name,
            response.iter_content(chunk_size=_CHUNK_BYTES),
            expected_bytes,
            expected_sha256,
            done_bytes,
            total,
            progress,
            should_cancel,
            resume_from=resume_from,
            digest=digest,
        )
    finally:
        response.close()


def _range_honoured(response, resume_from) -> bool:
    """Whether the answer really begins where the request asked it to.

    Status 206 says *a* range is coming, not *which* one. Neither Hugging Face
    nor its CDN gets this wrong, and after the digest-only resume rule the one
    file that resumes would catch a wrong offset by its hash anyway — but the
    comparison is one line and closes the hole instead of arguing about it. A
    206 with no Content-Range at all (which is not legal) reads as "not
    honoured", so the file starts over: always safe.
    """
    if getattr(response, "status_code", None) != 206:
        return False
    headers = getattr(response, "headers", None) or {}
    value = str(headers.get("Content-Range", "")).strip()
    if value.lower().startswith("bytes"):
        value = value[len("bytes"):].strip()
    return value.startswith(f"{resume_from}-")


def _seed_from_part(part_path, resume_from, expected_sha256, should_cancel):
    """(digest, offset) for a transfer resuming from `resume_from` bytes.

    Reading the prefix back is a local read of the bytes deliberately *not*
    being fetched again — nothing against the gigabytes of network it saves —
    and it is the only way a digest computed from the stream can survive an
    interrupted transfer. Anything unexpected about the prefix answers (fresh
    digest, 0): starting the file over is always safe, resuming from a prefix
    that could not be read is not.
    """
    fresh = hashlib.sha256() if expected_sha256 else None
    if not resume_from:
        return fresh, 0

    digest = hashlib.sha256() if expected_sha256 else None
    read = 0
    try:
        with open(part_path, "rb") as fh:
            while read < resume_from:
                chunk = fh.read(min(_CHUNK_BYTES, resume_from - read))
                if not chunk:
                    break
                _check_cancel(should_cancel)
                if digest is not None:
                    digest.update(chunk)
                read += len(chunk)
    except OSError as exc:
        logging.info("[transcription] cannot resume %s: %s", part_path,
                     errors.scrub_media_names(str(exc)))
        return fresh, 0

    if read != resume_from:
        return fresh, 0
    logging.info("[transcription] resuming %s at %d bytes", part_path, read)
    return digest, read


def _write_part(directory, name, chunks, expected_bytes, expected_sha256,
                done_bytes, total, progress, should_cancel,
                resume_from=0, digest=None) -> int:
    """Stream `chunks` into `<name>.part`, verify them, and only then publish.

    The one place the "a file under its final name has been verified" invariant
    is enforced, which is why the download and the move both go through it
    rather than each writing their own files: two copies of this would drift,
    and the way that drift shows up is a model that looks installed and that
    CTranslate2 then refuses to load.
    """
    part_path = os.path.join(directory, name + _PART_SUFFIX)
    if digest is None and expected_sha256:
        digest = hashlib.sha256()
    written = resume_from
    if resume_from:
        _report(progress, done_bytes + written, total)

    # Appending only when there is a prefix this call has already accounted
    # for; otherwise the file starts again from nothing.
    with open(part_path, "ab" if resume_from else "wb") as fh:
        for chunk in chunks:
            _check_cancel(should_cancel)
            if not chunk:
                continue
            if written + len(chunk) > expected_bytes:
                # Stopped here rather than measured at the end: a server (or a
                # mirror) that keeps sending would otherwise fill the disk
                # before the size check ever ran. Nothing past the expected
                # size is written; the `.part` is swept as for any mismatch.
                raise errors.TranscriptionError(
                    errors.MODEL_CORRUPTED,
                    f"{name}: more than the expected {expected_bytes} bytes",
                )
            fh.write(chunk)
            if digest is not None:
                digest.update(chunk)
            written += len(chunk)
            _report(progress, done_bytes + written, total)
        fh.flush()
        # NTFS journals the rename, not the data behind it. Without this, a
        # power cut between the two can leave a file of exactly the right size
        # full of zeros under the final name — which the install check believes
        # forever, and which for the auxiliary files nothing would ever detect.
        os.fsync(fh.fileno())

    if written != expected_bytes:
        raise errors.TranscriptionError(
            errors.MODEL_CORRUPTED,
            f"{name}: expected {expected_bytes} bytes, got {written}",
        )
    if digest is not None:
        actual = digest.hexdigest()
        if actual != expected_sha256:
            raise errors.TranscriptionError(
                errors.MODEL_CORRUPTED,
                f"{name}: sha256 {actual}, expected {expected_sha256}",
            )

    os.replace(part_path, os.path.join(directory, name))
    return done_bytes + written


def _read_chunks(path):
    """`path` as a chunk iterator, shaped like requests' iter_content()."""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk


def _hash_file(path, total_bytes, progress, should_cancel, done_bytes=0) -> str:
    """sha256 of `path`, reported (after `done_bytes`) and cancellable."""
    digest = hashlib.sha256()
    read = 0
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
                _check_cancel(should_cancel)
                digest.update(chunk)
                read += len(chunk)
                _report(progress, done_bytes + read, total_bytes)
    except OSError as exc:
        # A file that cannot be read is not a file that can be transcribed
        # with, and the way out of it is the same as for a bad digest.
        raise errors.TranscriptionError(errors.MODEL_CORRUPTED, f"{path}: {exc}") from exc
    return digest.hexdigest()


def _as_transcription_error(exc, model_id, fallback):
    """Map whatever went wrong onto the closed set of codes.

    Nothing escapes unclassified: a code the UI cannot translate is a code
    I18n.t() renders as its own key name, which NVDA then reads out letter by
    letter instead of a sentence.
    """
    if isinstance(exc, errors.TranscriptionError):
        return exc
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        # The gate ran before the transfer, but a multi-gigabyte transfer takes
        # long enough for something else on the machine to fill the volume.
        return errors.TranscriptionError(errors.NO_DISK_SPACE, f"{model_id}: {exc}")
    return errors.TranscriptionError(fallback, f"{model_id}: {exc}")


def _remove_parts(directory, model) -> None:
    """Drop every `.part` this model could have left behind in `directory`."""
    for name, _size in model.files:
        _unlink(os.path.join(directory, name + _PART_SUFFIX))
