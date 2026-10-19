"""Fetch a WinZapp update: download the release ZIP, verify it, extract it.

The first half of an update, with no window attached. It used to live inside
UpdateProgressDialog._worker(), which made a download impossible without the
modal progress dialog; the dialog still runs it (with its gauge and Cancel),
and a background download runs the very same function with neither
(update_background.py). The second half — the other accounts, the install slot,
the batch installer — stays in the dialog: it is the part that takes the app
down.

Nothing here is trusted until _verify_sha256sums() has passed: the ZIP is
handed elevated write access to the install directory right after
(docs/traps/release-integrity.md).
"""

import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass

from core import tls_trust
from core.update_file_lease import hold_package, release_package, package_busy

_CHUNK = 65536
#: Prefix of the temporary directory an update is extracted into.
EXTRACT_PREFIX = "winzapp_ext_"


@dataclass
class UpdatePackage:
    """How a fetch ended. Exactly one of the three is set."""
    extract_dir: str = ""     # ready to install
    error: str = ""           # refused: a message for the user
    cancelled: bool = False


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def download_update_package(zip_url: str, sha256sums_url: str, signature_url: str,
                            new_version: str, is_alpha: bool, i18n,
                            on_progress=None, is_cancelled=None) -> UpdatePackage:
    """Download, verify and extract; returns the directory to install from.

    on_progress(percent 0..99) is called as the download advances and
    is_cancelled() is polled between chunks and between steps. A network or
    disk fault raises, as it always did; the caller decides what to tell the
    user. A cancel or a refused checksum leaves no ZIP behind.
    """
    # The verification helpers live in updater.py, which imports this module.
    import updater

    cancelled = is_cancelled or (lambda: False)
    zip_fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="winzapp_upd_")
    os.close(zip_fd)

    logging.info("Auto-updater: Downloading ZIP from %s to %s", zip_url, zip_path)
    try:
        resp = tls_trust.get(zip_url, stream=True, timeout=60)
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if cancelled():
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total and on_progress is not None:
                    on_progress(min(int(downloaded * 100 / total), 99))
    except BaseException:
        # A connection that dropped half-way: the partial ZIP is of no use.
        _remove(zip_path)
        raise

    if cancelled():
        logging.info("Auto-updater: Download cancelled.")
        _remove(zip_path)
        return UpdatePackage(cancelled=True)

    logging.info("Auto-updater: Download completed successfully.")

    try:
        return _prepare_downloaded_package(
            zip_path, zip_url, sha256sums_url, signature_url, new_version,
            is_alpha, i18n, cancelled, updater)
    finally:
        _remove(zip_path)


def _prepare_downloaded_package(zip_path, zip_url, sha256sums_url, signature_url,
                                new_version, is_alpha, i18n, cancelled, updater):
    # ── Verify integrity ──────────────────────────────────────────────────
    # Before this ZIP is trusted with elevated write access to the install
    # directory, confirm it's byte-for-byte what CI actually built — not a
    # MITM'd download or a tampered/hijacked release edit. See
    # _verify_sha256sums()'s docstring for the fail-open/fail-closed policy.
    filename = os.path.basename(zip_url.split("?")[0])
    ok, detail = updater._verify_sha256sums(
        zip_path, filename, sha256sums_url,
        signature_url=signature_url,
        expected_version=new_version,
        is_alpha=is_alpha,
    )
    if not ok:
        logging.error("Auto-updater: Checksum verification failed for %s: %s", filename, detail)
        _remove(zip_path)
        return UpdatePackage(error=i18n.t("update_checksum_mismatch").format(detail=detail))

    # ── Extract ───────────────────────────────────────────────────────────
    extract_dir = extract_root = tempfile.mkdtemp(prefix=EXTRACT_PREFIX)
    logging.info("Auto-updater: Extracting update to %s", extract_dir)
    try:
        hold_package(extract_root)
        with zipfile.ZipFile(zip_path, "r") as zf:
            updater._safe_extract_zip(zf, extract_dir)
    except BaseException:
        release_package(extract_root)
        shutil.rmtree(extract_root, ignore_errors=True)
        raise
    finally:
        _remove(zip_path)

    # If the ZIP placed all files inside a single top-level folder, point
    # extract_dir at that folder so xcopy copies the contents.
    try:
        entries = [e for e in os.listdir(extract_dir) if not e.startswith(".")]
        if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
            extract_dir = os.path.join(extract_dir, entries[0])
        if not os.path.isfile(os.path.join(extract_dir, "WinZapp.exe")):
            raise ValueError("update package has no WinZapp.exe")
    except BaseException:
        release_package(extract_root)
        shutil.rmtree(extract_root, ignore_errors=True)
        raise

    if cancelled():
        logging.info("Auto-updater: Extraction cancelled.")
        release_package(extract_root)
        shutil.rmtree(extract_root, ignore_errors=True)
        return UpdatePackage(cancelled=True)
    return UpdatePackage(extract_dir=extract_dir)


def extraction_root(extract_dir: str) -> str:
    """The temporary directory an update was extracted into, or "".

    `extract_dir` is that directory, or the single folder inside it when the
    ZIP had one (see download_update_package()). Anything whose directory is
    not named like one of ours answers "": this is what decides what gets
    deleted, and a path that merely looks like a package must never be removed
    on the strength of its position.
    """
    if not extract_dir:
        return ""
    target = os.path.normpath(extract_dir)
    if os.path.basename(target).startswith(EXTRACT_PREFIX):
        return target
    parent = os.path.dirname(target)
    if os.path.basename(parent).startswith(EXTRACT_PREFIX):
        return parent
    return ""


def discard_package(extract_dir: str) -> None:
    """Drop a fetched update that will not be installed after all: the whole
    temporary directory, also when extract_dir is the single folder inside."""
    if not extract_dir:
        return
    root = extraction_root(extract_dir)
    target = root or os.path.normpath(extract_dir)
    if root:
        release_package(root)
        if package_busy(root):
            return  # The guardian has taken over; this caller no longer owns it.
    shutil.rmtree(target, ignore_errors=True)
