"""Explicit retirement of expired, empty replay tokens; never of held files."""
import json
import os
import re
import time

from . import fileops
from .types import Entry, Root, fingerprint, unsafe


def retire(store, *, cancel=lambda: False, provider=fileops.pinned, now=None):
    now = time.time() if now is None else now
    folder = os.path.join(store.directory, "storage_quarantine")
    if not os.path.isdir(folder):
        return 0, False
    count, partial = 0, False
    with store.lock(), fileops.pinned_directory(folder), os.scandir(folder) as children:
        for number, child in enumerate(children):
            if cancel() or number >= 10000:
                partial = True
                break
            if not re.fullmatch(r"[0-9a-f]{32}\.json", child.name):
                continue
            try:
                info = os.lstat(child.path)
                if unsafe(info) or info.st_size > 2_000_000:
                    continue
                with open(child.path, encoding="utf-8") as stream:
                    raw = json.load(stream)
                created = raw.get("created")
                if (raw.get("version") != 1 or raw.get("items") != []
                        or type(created) not in (int, float) or not 172800 < now - created):
                    continue
                entry = Entry(Root("tokens", folder, "quarantine", managed=True),
                              child.name, info.st_size, info.st_mtime, fingerprint(info), "quarantine")
                with provider(entry) as pinned:
                    pinned.remove()
                count += 1
            except (OSError, ValueError, AttributeError, NotImplementedError):
                partial = True
    return count, partial
