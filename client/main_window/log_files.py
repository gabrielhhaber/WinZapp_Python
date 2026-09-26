"""Log-file plumbing that has to exist before MainWindow does.

Moved verbatim out of main.py; main.py re-exports every name.
"""

import io
import logging
import os
import threading
from app_paths import data_path


class _LazyLogFile:
    """A write-only stdout/stderr replacement that creates its file on the
    first byte actually written, not on startup.

    stdout.log and stderr.log used to be created empty on every launch and then
    opened in append mode, so a healthy install accumulated two permanently
    empty files in logs/ that look like they should contain something. They are
    crash-and-traceback sinks: most runs legitimately write nothing to them, and
    a zero-byte file is worse than no file — it invites the user to open it
    looking for the problem, and it makes "is there anything in the logs?"
    unanswerable without checking sizes.

    Deliberately not a subclass of io.TextIOBase or a wrapper around an already
    open handle: the point is to hold nothing but a path until something writes.
    Append mode, so a file that does get created keeps whatever an earlier run
    of the same session put there.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, path: str):
        self._path = path
        self._fh = None
        self._lock = threading.Lock()

    def _ensure_open(self):
        if self._fh is None:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._fh = open(
                self._path, "a", encoding=self.encoding, errors=self.errors
            )
        return self._fh

    def write(self, text):
        if not text:
            # Nothing to record, and nothing worth creating a file for. print()
            # alone emits a separate "" and newline write pair, so treating an
            # empty write as a real one would defeat the whole point.
            return 0
        with self._lock:
            try:
                handle = self._ensure_open()
                written = handle.write(text)
                handle.flush()
                return written
            except Exception:
                # A logging sink that raises on write turns any stray print()
                # into a crash. Losing the line is the lesser failure.
                return 0

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except Exception:
                    pass

    def close(self):
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None

    def isatty(self):
        return False

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    def fileno(self):
        # Same answer io.StringIO gives. Anything asking for a real descriptor
        # (a subprocess wanting to inherit it) must not silently get one that
        # does not exist — and callers already handle this exception.
        raise io.UnsupportedOperation("fileno")

    @property
    def closed(self):
        return False

    @property
    def name(self):
        return self._path


def _consolidate_legacy_log_dir() -> None:
    """Fold a legacy data/log/ into data/logs/, once, without losing content.

    stdout.log and stderr.log used to be written to data/log/ while every other
    log file went to data/logs/. Nothing distinguished the two — it was just a
    hardcoded path that predated log_path() — but the names differ by a single
    letter, which is exactly the wrong kind of ambiguity for directories a user
    is asked to zip up and send when something breaks.

    Content is appended, never overwritten: a file may already exist on the new
    side (the app has run since this change) and the old side may still hold
    output from before it. Anything that cannot be moved is left where it is —
    a stale handle on Windows must not stop the app from starting — and the old
    directory is removed only when it actually ends up empty.
    """
    from app_paths import data_path, log_path
    legacy_dir = data_path("log")
    if not os.path.isdir(legacy_dir):
        return
    target_dir = log_path()
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError:
        return
    if os.path.abspath(legacy_dir) == os.path.abspath(target_dir):
        return

    for name in sorted(os.listdir(legacy_dir)):
        source = os.path.join(legacy_dir, name)
        if not os.path.isfile(source):
            continue
        try:
            with open(source, "rb") as src:
                payload = src.read()
            if payload:
                with open(os.path.join(target_dir, name), "ab") as dst:
                    dst.write(payload)
            os.remove(source)
        except OSError as exc:
            logging.info(
                "[logs] could not fold %s into %s: %s", source, target_dir, exc
            )
    try:
        os.rmdir(legacy_dir)
        logging.info("[logs] folded legacy %s into %s.", legacy_dir, target_dir)
    except OSError:
        # Not empty (something we could not move) — leave it alone.
        pass
