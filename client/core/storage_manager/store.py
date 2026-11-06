"""Account-local private preferences/history, independent of exported settings."""
from __future__ import annotations

import json
import math
import os
import uuid

from coord_locks import NamedLock
from .policy import Preferences
from .types import canonical


class StorageStore:
    def __init__(self, directory):
        self.directory = os.path.abspath(directory)
        self.path = os.path.join(self.directory, "storage_manager.json")

    def lock(self):
        import hashlib
        tag = hashlib.sha256(canonical(self.directory).encode()).hexdigest()[:24]
        return NamedLock("WinZappStorage_" + tag,
                         os.path.join(self.directory, "storage_manager.lock"), timeout=2)

    def _read(self, *, strict=False):
        try:
            with open(self.path, encoding="utf-8") as stream:
                text = stream.read(2_000_001)
                if len(text) > 2_000_000:
                    raise ValueError("storage preferences too large")
                raw = json.loads(text)
            if not isinstance(raw, dict) or type(raw.get("version", 1)) is not int or raw.get("version", 1) != 1:
                if strict:
                    raise ValueError("unsupported storage preferences")
                return {}
            return raw
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if strict:
                raise
            return {}

    def preferences(self, *, strict=False):
        # Reading preferences never creates a directory or a mutex lock file.
        raw = self._read(strict=strict).get("preferences", {})
        if strict:
            if (not isinstance(raw, dict) or type(raw.get("version", 1)) is not int
                    or raw.get("version", 1) != 1):
                raise ValueError("unsupported private storage preferences")
            for key in ("protected", "protected_chats", "protected_categories"):
                values = raw.get(key, [])
                if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                    raise ValueError("unreadable protection list")
        return Preferences.read(raw)

    def save_preferences(self, preferences):
        with self.lock():
            raw = self._read(strict=True)
            raw.update(version=1, preferences=preferences.as_dict())
            self._write(raw)

    def history(self, now, days):
        if not days:
            return []
        rows = self._read().get("history", [])
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict) and type(r.get("time")) in (int, float)
                and math.isfinite(r["time"]) and 0 <= now - r["time"] <= days * 86400
                and all(type(r.get(k, 0)) is int and 0 <= r.get(k, 0) <= 10**18
                        for k in ("completed", "skipped", "failed", "bytes_removed"))][-100:]

    def record(self, now, days, *, completed=0, skipped=0, failed=0, bytes_removed=0):
        with self.lock():
            raw = self._read(strict=True)
            rows = self.history(now, days)
            if days:
                rows.append(dict(time=now, completed=completed, skipped=skipped,
                                 failed=failed, bytes_removed=bytes_removed))
            raw.update(version=1, history=rows[-100:])
            self._write(raw)

    def clear_history(self):
        with self.lock():
            raw = self._read(strict=True)
            raw.update(version=1, history=[])
            self._write(raw)

    def reset_preferences(self):
        """Explicit UI reset may replace bad preferences, never held files."""
        with self.lock():
            raw = self._read()
            raw.update(version=1, preferences=Preferences().as_dict())
            self._write(raw)

    def _write(self, raw):
        os.makedirs(self.directory, exist_ok=True)
        temporary = self.path + "." + uuid.uuid4().hex + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(raw, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
