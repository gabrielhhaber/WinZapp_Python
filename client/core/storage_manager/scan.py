"""Cancellable metadata-only scan. Never reads, decrypts or opens file content."""
from __future__ import annotations

import os
import stat

from .types import Entry, Snapshot, canonical, fingerprint, inside, unsafe


def _safe_ancestors(path):
    """A lexical root must not enter through an intermediate junction either."""
    current = canonical(path)
    while True:
        try:
            if unsafe(os.lstat(current)):
                return False
        except FileNotFoundError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return True
        current = parent


def scan(roots, *, cancel=lambda: False, progress=None, max_entries=100_000,
         max_nodes=300_000, classify=None):
    """Count a physical file identity once; more-specific roots own subtrees.

    Counts logical lengths of unique files, not allocated disk bytes. Entries
    skipped for offline/reparse attributes remain explicit incomplete coverage.
    `max_nodes` bounds directory-only trees as well as regular files.
    """
    result = Snapshot()
    unique = {}
    for root in roots:
        if root.error:
            result.issue(root.error)
            continue
        unique.setdefault(canonical(root.path), root)
    roots = sorted(unique.values(), key=lambda r: len(canonical(r.path)), reverse=True)
    claimed, identities, nodes = [], set(), 0
    for root in roots:
        base = canonical(root.path)
        excluded = [canonical(p) for p in root.exclude] + claimed
        claimed.append(base)
        try:
            if not _safe_ancestors(base):
                result.issue("unfollowed")
                continue
            root_info = os.lstat(base)
        except FileNotFoundError:
            if root.required:
                result.issue("missing")
            continue  # An optional cache not created yet is not missing data.
        except OSError:
            result.issue("unreadable")
            continue
        pending = [(base, root_info)]
        if root.file_only and not stat.S_ISREG(root_info.st_mode):
            result.issue("unsupported")
            continue
        while pending:
            if cancel():
                result.cancelled = True
                return result
            path, info = pending.pop()
            nodes += 1
            if nodes > max_nodes or len(result.entries) >= max_entries:
                result.issue("limit")
                return result
            if any(inside(path, item) for item in excluded):
                continue
            if unsafe(info):
                result.issue("unfollowed")
                continue
            if stat.S_ISDIR(info.st_mode):
                # Recheck the ancestors immediately before opening a folder.
                # Metadata is still a changing snapshot, never deletion proof.
                try:
                    if not _safe_ancestors(path):
                        result.issue("unfollowed")
                        continue
                    with os.scandir(path) as children:
                        for child in children:
                            if cancel():
                                result.cancelled = True
                                return result
                            if len(pending) + nodes >= max_nodes:
                                result.issue("limit")
                                return result
                            try:
                                pending.append((child.path, child.stat(follow_symlinks=False)))
                            except OSError:
                                result.issue("changed")
                except OSError:
                    result.issue("unreadable")
                continue
            if not stat.S_ISREG(info.st_mode):
                result.issue("unsupported")
                continue
            # Windows DirEntry.stat() can omit st_dev/st_ino. A fresh lstat
            # supplies the identity a later preview must actually revalidate.
            try:
                info = os.lstat(path)
            except OSError:
                result.issue("changed")
                continue
            if unsafe(info) or not stat.S_ISREG(info.st_mode):
                result.issue("changed")
                continue
            identity = (info.st_dev, info.st_ino) if info.st_ino else canonical(path)
            if identity in identities:
                result.duplicates += 1
                continue
            identities.add(identity)
            entry = Entry(root, os.path.relpath(path, base), max(0, info.st_size),
                          info.st_mtime, fingerprint(info), root.category)
            if classify is not None:
                entry = classify(entry)
            result.entries.append(entry)
            if entry.display_name and entry.chat_key:
                result.named_chats.add(entry.chat_key)
            if progress is not None and len(result.entries) % 256 == 0:
                progress(len(result.entries))
    return result
