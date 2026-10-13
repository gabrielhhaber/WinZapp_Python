"""Plaintext a crashed run left in %TEMP%, removed at the next start.

A transcription writes the decrypted voice note, its converted WAV and
whisper.cpp's output into the system temp folder under random names, and
deletes them when it ends. A process killed in the middle (a crash, a power
cut, Task Manager) never gets to, and %TEMP% is not a private place. The day
of grace is what keeps this from touching a run another WinZapp account on
the same machine is in the middle of: a transcription of a long recording on a
slow machine outlasts an hour, and what a live run still holds open is left by
the delete's own refusal, not by guesswork about its age.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time

#: What message_audio, audio_prep and whisper_cpp_backend name their temporaries.
PREFIXES = ("winzapp-audio-", "winzapp-transcribe-", "winzapp-whisper-",
            # The download of a status being transcribed (statuses.TEMP_PREFIX).
            "winzapp-status-")

MAX_AGE_SECONDS = 24 * 3600


def _remove_tree(path) -> bool:
    """rmtree that goes on past a locked file; False if anything stayed.

    Whatever could be deleted is, so a run that holds one file open does not
    keep its siblings' plaintext alive; the entry is not counted as removed and
    the next start tries it again.
    """
    failures = []

    def _skip(_function, _path, exc):
        failures.append(type(exc).__name__)

    shutil.rmtree(path, onexc=_skip)
    if failures:
        logging.warning(
            "[transcription] a stale temporary folder is partly locked (%s)", failures[0]
        )
    return not failures


def sweep_stale_temporaries(temp_dir=None, now=None, max_age_seconds=MAX_AGE_SECONDS) -> int:
    """Delete this package's temporaries older than `max_age_seconds`.

    Returns how many entries went. Never raises, and logs counts only: a
    temporary's name is random, but nothing else about it is worth saying.
    """
    removed = 0
    try:
        root = temp_dir or tempfile.gettempdir()
        now = time.time() if now is None else now
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(PREFIXES):
            continue
        path = os.path.join(root, name)
        try:
            info = os.stat(path)
            # A file only read lately (atime) is as recent as one written.
            if now - max(info.st_mtime, info.st_atime) < max_age_seconds:
                continue
            if os.path.isdir(path) and not os.path.islink(path):
                if not _remove_tree(path):
                    continue
            else:
                os.remove(path)
            removed += 1
        except OSError as exc:
            logging.warning(
                "[transcription] could not remove a stale temporary (%s)",
                type(exc).__name__,
            )
    if removed:
        logging.info("[transcription] removed %d stale temporary file(s)", removed)
    return removed
