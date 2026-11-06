"""The index of media removed by the cleanup, so it does not come back.

Cleaning frees disk space only if what was deleted stays deleted. The
automatic download passes every message whose file is not on disk (that is
what it is for), so without a memory of what the cleanup removed the next
sync would fetch it all again. This is that memory: the cache ids (see
ui.conversation_panel.media_paths.media_cache_id) of the files the cleanup
deleted, kept in `media_cleaned.json` of the account's data folder.

What it blocks: every automatic download and every explicit one — the
message offers no download and says why. What clears it: a logout or a new
pairing (`MainWindow._forget_cleaned_media()`, from the wipe that empties the
account's local data) and nothing else. A full resync (F5) does not: it
refetches the messages of the same account, and what the user chose to delete
must stay deleted.

No wx, no MainWindow; the file path is resolved at each use because the
account's data folder changes with the account.
"""

from __future__ import annotations

import json
import os
import threading
import uuid


class CleanedMediaIndex:
    def __init__(self, path_getter):
        self._path_getter = path_getter
        self._lock = threading.Lock()
        self._loaded_from = None
        self._ids: set = set()

    # The cache is keyed by the file it was read from, so it follows the
    # account without anyone having to remember to reset it.
    def _load(self):
        path = self._path_getter()
        if self._loaded_from == path:
            return
        ids = set()
        try:
            with open(path, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
            values = raw.get("ids") if isinstance(raw, dict) else raw
            if isinstance(values, list):
                ids = {value for value in values if isinstance(value, str) and value}
        except (OSError, ValueError):
            ids = set()
        self._ids = ids
        self._loaded_from = path

    def contains(self, cache_id: str) -> bool:
        if not cache_id:
            return False
        with self._lock:
            self._load()
            return cache_id in self._ids

    def __len__(self) -> int:
        with self._lock:
            self._load()
            return len(self._ids)

    def add_many(self, cache_ids) -> int:
        """Remember `cache_ids`; the number of new ones. Written at once and
        atomically: a crash must not leave half an index that forgets."""
        fresh = {value for value in cache_ids if isinstance(value, str) and value}
        if not fresh:
            return 0
        with self._lock:
            self._load()
            added = fresh - self._ids
            if not added:
                return 0
            self._ids |= added
            self._save()
            return len(added)

    def clear(self) -> None:
        with self._lock:
            path = self._path_getter()
            self._ids = set()
            self._loaded_from = path
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass

    def update_durable(self, cache_ids, *, remove=False):
        """An explicit storage transaction must persist its block BEFORE acting.

        Unlike legacy best-effort add_many, failures propagate. Only requested
        ids change; malformed existing data must never become an empty index.
        Returns newly changed ids so a failed transaction can roll back its own
        additions without clearing anybody else's cleanup decisions.
        """
        requested = {v for v in cache_ids if isinstance(v, str) and v}
        with self._lock:
            path = self._path_getter()
            try:
                with open(path, encoding="utf-8") as stream:
                    raw = json.load(stream)
                values = raw.get("ids") if isinstance(raw, dict) else raw
                if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                    raise ValueError("invalid cleaned-media index")
                previous = set(values)
            except FileNotFoundError:
                previous = set()
            changed = previous & requested if remove else requested - previous
            self._loaded_from = path
            self._ids = previous - requested if remove else previous | requested
            if not self._save():
                self._ids = previous
                raise OSError("could not persist cleaned-media index")
            return changed

    def _save(self):
        path = self._loaded_from
        temporary = path + "." + uuid.uuid4().hex + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump({"ids": sorted(self._ids)}, stream)
            os.replace(temporary, path)
            return True
        except OSError:
            # Still blocked for this run; it is the file that failed.
            try:
                os.remove(temporary)
            except OSError:
                pass
            return False
