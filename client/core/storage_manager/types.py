"""Immutable metadata exchanged by storage scanning, policy and UI."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import stat
import time
import uuid


CATEGORIES = (
    "program", "media", "voice", "models", "backups", "sessions",
    "database", "logs", "compile_cache", "quarantine", "other",
)
# Only this cache is reproducible without contacting WhatsApp. Models, media,
# logs and backups must never silently join it just because of their age.
AUTOMATIC_CATEGORIES = frozenset({"compile_cache"})
BLOCKED_ATTRIBUTES = 0x400 | 0x1000 | 0x40000 | 0x400000
_IDENTITY_TIME_FIELD = "st_birthtime_ns" if os.name == "nt" else "st_ctime_ns"


def canonical(path):
    """Lexical identity: do not follow a reparse point to discover a scope."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def inside(path, root):
    try:
        return os.path.commonpath((canonical(path), canonical(root))) == canonical(root)
    except (ValueError, TypeError, OSError):
        return False


def unsafe(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & BLOCKED_ATTRIBUTES)


def fingerprint(info):
    # Windows path stat and fstat can give ctime different meanings. Use
    # their explicit, consistent creation timestamp without dropping any
    # preview component. Elsewhere retain metadata-change time.
    # https://github.com/python/cpython/issues/157671
    return (info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, getattr(info, _IDENTITY_TIME_FIELD, info.st_ctime_ns))


@dataclass(frozen=True)
class Root:
    key: str
    path: str
    category: str = "other"
    scope: str = "account"
    # Managed permits only the category's declared operations, never arbitrary
    # cleanup of a directory supplied by the user or a shared external model.
    managed: bool = False
    exclude: tuple[str, ...] = ()
    file_only: bool = False
    required: bool = False
    error: str = ""


@dataclass(frozen=True)
class Entry:
    root: Root
    relative: str
    size: int
    modified: float
    identity: tuple
    category: str
    reason: str = "protected"
    media_id: str = ""
    chat_key: str = ""
    display_name: str = ""
    media_type: str = ""

    @property
    def path(self):
        return self.root.path if self.relative == "." else os.path.join(self.root.path, self.relative)

    @property
    def key(self):
        return canonical(self.path)


@dataclass
class Snapshot:
    entries: list[Entry] = field(default_factory=list)
    issues: dict[str, int] = field(default_factory=dict)
    cancelled: bool = False
    created: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    duplicates: int = 0
    named_chats: set[str] = field(default_factory=set)

    @property
    def complete(self):
        return not self.cancelled and not self.issues

    @property
    def total_bytes(self):
        return sum(entry.size for entry in self.entries)

    def totals(self):
        result = {}
        for entry in self.entries:
            count, size = result.get(entry.category, (0, 0))
            result[entry.category] = count + 1, size + entry.size
        return result

    def issue(self, reason):
        self.issues[reason] = self.issues.get(reason, 0) + 1
