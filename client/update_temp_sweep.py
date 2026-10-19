"""What past updates left in %TEMP%, removed at the next start.

An update is downloaded into ``winzapp_upd_*.zip``, extracted into a
``winzapp_ext_*`` folder (a full copy of the release, hundreds of megabytes)
and installed by a ``winzapp_upd_*.bat``. The installer script now deletes the
folder when the copy has worked (updater.cleanup_lines()), but three things
still leave things behind: every update made before that line existed, an
install that failed (the script and its folder are kept as evidence), and a
process killed between download and install. This sweeps them.

An active package's lifetime lease keeps it off a download still waiting for
a quiet moment to install (update_background.may_interrupt_now()), regardless
of age. Abandoned packages get two days; scripts get a week, since a failed
install's script is evidence and the log it points at is elsewhere.

Never raises, runs off the main thread, and logs counts only.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time

from update_package import EXTRACT_PREFIX
from core.update_file_lease import package_busy

#: Prefix of the downloaded ZIP, the installer script and its relaunch markers.
_UPDATE_PREFIX = "winzapp_upd_"

PACKAGE_MAX_AGE_SECONDS = 2 * 24 * 3600
SCRIPT_MAX_AGE_SECONDS = 7 * 24 * 3600


def _max_age(name: str):
    """How old `name` must be before it goes, or None if it is not ours."""
    if name.startswith(EXTRACT_PREFIX):
        return PACKAGE_MAX_AGE_SECONDS
    if name.startswith(_UPDATE_PREFIX):
        # .zip is a download that never reached its extraction; the rest are
        # the script (.bat) and the markers beside it.
        return PACKAGE_MAX_AGE_SECONDS if name.endswith(".zip") else SCRIPT_MAX_AGE_SECONDS
    return None


def sweep_stale_update_temporaries(temp_dir=None, now=None) -> int:
    """Delete the update leftovers older than their grace period.

    Returns how many entries went. A folder partly locked by an antivirus is
    removed as far as it can be and tried again at the next start.
    """
    removed = 0
    try:
        root = temp_dir or tempfile.gettempdir()
        now = time.time() if now is None else now
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        limit = _max_age(name)
        if limit is None:
            continue
        path = os.path.join(root, name)
        try:
            if name.startswith(EXTRACT_PREFIX) and not name.endswith(".lease"):
                if package_busy(path):
                    continue
            if now - os.stat(path).st_mtime < limit:
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                failures = []
                shutil.rmtree(path, onexc=lambda _f, _p, exc: failures.append(exc))
                if failures:
                    continue
            else:
                os.remove(path)
            removed += 1
        except OSError as exc:
            logging.warning("Auto-updater: could not remove a stale update temporary (%s)",
                            type(exc).__name__)
    if removed:
        logging.info("Auto-updater: removed %d stale update temporary item(s)", removed)
    return removed


def start_background_sweep() -> threading.Thread:
    """Sweep on a thread of its own: a few hundred megabytes of small files can
    take seconds, and nobody should wait for that to see the window."""
    thread = threading.Thread(
        target=sweep_stale_update_temporaries, daemon=True, name="winzapp-update-sweep"
    )
    thread.start()
    return thread
