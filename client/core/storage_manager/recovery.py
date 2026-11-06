"""Review and undo held files without trusting a journal's stored paths."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import re
import time

from . import fileops
from .transactions import _write
from .types import Entry, Root, canonical, fingerprint, inside, unsafe


_TOKEN = re.compile(r"[0-9a-f]{32}")


class HoldingPage(list):
    """List-compatible bounded page with an explicit continuation/coverage flag."""
    next_cursor = None
    partial = False


@dataclass(frozen=True)
class Holding:
    journal: str
    index: int
    entry: Entry
    original: Entry
    new_block: bool
    created: float


def _decode(store, journal, index, item, roots, created):
    if item.get("state") not in ("held", "pending", "restoring", "purging"):
        return None
    if type(created) not in (int, float) or not math.isfinite(created) or not 0 <= created <= time.time() + 86400:
        return None
    root = next((r for r in roots if r.key == item.get("root") and r.managed
                 and canonical(r.path) == item.get("root_path")), None)
    if root is None or root.category not in ("media", "voice", "compile_cache"):
        return None
    relative, held = item.get("relative"), item.get("held")
    if not isinstance(relative, str) or not isinstance(held, str):
        return None
    if not held.endswith(".held") or not _TOKEN.fullmatch(held[:-5]):
        return None
    identity = item.get("identity")
    if not isinstance(identity, list) or len(identity) != 5 or not all(type(v) is int for v in identity):
        return None
    original = Entry(root, relative, identity[2], 0, tuple(identity), root.category,
                     media_id=item.get("media_id", ""), chat_key=item.get("chat_key", ""))
    if not inside(original.path, root.path) or canonical(original.path) == canonical(root.path):
        return None
    if root.category in ("media", "voice"):
        from ui.conversation_panel.media_paths import local_media_cache_paths
        if not isinstance(original.media_id, str) or not original.media_id:
            return None
        expected = local_media_cache_paths(root.path, root.path, original.media_id)[0 if root.category == "voice" else 1]
        if canonical(original.path) != canonical(expected):
            return None
    folder = os.path.join(store.directory, "storage_quarantine")
    path = os.path.join(folder, held)
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if unsafe(info) or fingerprint(info)[:4] != tuple(identity)[:4]:
        return None
    value = Entry(Root("quarantine", folder, "quarantine", managed=True), held,
                  info.st_size, info.st_mtime, fingerprint(info), "quarantine")
    return Holding(journal, index, value, original, item.get("new_block") is True, created)


def holdings(store, roots, *, limit=1000, after=None):
    """Bounded private manifests, including a crash between rename and commit.

    Pending rows qualify only when the held file proves the original identity.
    Unreadable/incompatible manifests are retained, not automatically repaired.
    """
    folder = os.path.join(store.directory, "storage_quarantine")
    result = HoldingPage()
    from .scan import _safe_ancestors
    if not _safe_ancestors(folder):
        return result
    try:
        names = []
        with os.scandir(folder) as children:
            for child in children:
                if len(names) >= 100000:
                    result.partial = True
                    break
                if not child.name.endswith(".json") or not _TOKEN.fullmatch(child.name[:-5]):
                    continue
                names.append(child.name)
        for name in sorted(names):
            if after and name < after[0]:
                continue
            path = os.path.join(folder, name)
            try:
                info = os.lstat(path)
                if unsafe(info) or info.st_size > 2_000_000:
                    result.partial = True
                    continue
                with open(path, encoding="utf-8") as stream:
                    raw = json.load(stream)
                if (not isinstance(raw, dict) or type(raw.get("version")) is not int
                        or raw.get("version") != 1 or raw.get("method") != "quarantine"):
                    continue
                items = raw.get("items")
                if not isinstance(items, list):
                    result.partial = True
                    continue
                for index, item in enumerate(items):
                    if after and (name, index) <= tuple(after):
                        continue
                    if isinstance(item, dict) and item.get("state") in ("held", "pending", "restoring", "purging"):
                        value = _decode(store, path, index, item, roots, raw.get("created", 0))
                        if value is not None:
                            result.append(value)
                            if len(result) >= limit:
                                result.next_cursor = (name, index)
                                return result
                        else:
                            result.partial = True
            except (OSError, ValueError, TypeError):
                result.partial = True
    except OSError:
        result.partial = True
    return result


def finish(holding, store, roots, media_index, preferences, *, restore,
           provider=fileops.pinned):
    """Freshly validate one selection. Native rename never overwrites a target."""
    folder = os.path.join(store.directory, "storage_quarantine")
    name = os.path.basename(holding.journal)
    if (canonical(os.path.dirname(holding.journal)) != canonical(folder)
            or not name.endswith(".json") or not _TOKEN.fullmatch(name[:-5])):
        raise ValueError("journal leaves the account scope")
    with store.lock(), fileops.pinned_directory(store.directory), fileops.pinned_directory(folder):
        with open(holding.journal, encoding="utf-8") as stream:
            raw = json.load(stream)
        item = raw["items"][holding.index]
        current = _decode(store, holding.journal, holding.index, item, roots, raw.get("created", 0))
        if current is None or current.entry.identity != holding.entry.identity:
            raise fileops.ChangedFile("held file changed")
        if (current.original.key in preferences.protected
                or current.original.chat_key in preferences.protected_chats
                or current.original.category in preferences.protected_categories):
            raise fileops.ChangedFile("original is user protected")
        if restore and os.path.lexists(current.original.path):
            raise FileExistsError("restore never overwrites a current file")
        item["state"] = "restoring" if restore else "purging"
        _write(holding.journal, raw)
        with provider(current.entry) as pinned:
            if restore:
                pinned.move(current.original.path)
            else:
                pinned.remove()
        if restore and current.new_block and current.original.media_id:
            media_index.update_durable([current.original.media_id], remove=True)
        item["state"] = "restored" if restore else "removed"
        if all(i.get("state") in ("restored", "removed", "failed") for i in raw["items"]):
            raw["items"] = []  # Retain the consumed token, not old private paths.
        _write(holding.journal, raw)
        return current.entry.size
