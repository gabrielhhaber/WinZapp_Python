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
import tempfile
import zipfile
from dataclasses import dataclass

import requests

_CHUNK = 65536


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
    resp = requests.get(zip_url, stream=True, timeout=60)
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

    if cancelled():
        logging.info("Auto-updater: Download cancelled.")
        _remove(zip_path)
        return UpdatePackage(cancelled=True)

    logging.info("Auto-updater: Download completed successfully.")

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
    extract_dir = tempfile.mkdtemp(prefix="winzapp_ext_")
    logging.info("Auto-updater: Extracting update to %s", extract_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        updater._safe_extract_zip(zf, extract_dir)
    os.remove(zip_path)

    # If the ZIP placed all files inside a single top-level folder, point
    # extract_dir at that folder so xcopy copies the contents.
    entries = [e for e in os.listdir(extract_dir) if not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
        extract_dir = os.path.join(extract_dir, entries[0])

    if cancelled():
        logging.info("Auto-updater: Extraction cancelled.")
        return UpdatePackage(cancelled=True)
    return UpdatePackage(extract_dir=extract_dir)
