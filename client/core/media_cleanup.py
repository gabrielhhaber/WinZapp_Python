"""Media cleanup: free disk space by deleting downloaded media files.

Deletes the files WinZapp cached for received media of chosen conversations
and media types — never the messages. The cleaned-media index prevents the
next sync from refilling the cache. A later explicit Download request can
ask to retrieve that one file without clearing the automatic-download block;
WhatsApp may no longer have the media.

Two steps, on purpose: `scan()` only reads (it stats files and deletes
nothing) and returns a `Preview`; `execute()` takes that same preview back
and deletes exactly the files in it, never scanning again. What is deleted is
always what the user was shown and confirmed.

No wx and no MainWindow: the caller reads each chat's messages (the database
holds the whole history, the list in memory only a page of it) and hands the
records in. Where a message's file lives is `cached_media_path()`'s answer
(media/<id>.wzmedia, voice_messages/<id>.msv); this module keeps no second
copy of that rule.

What is never touched:

* a file outside the two cache folders (checked again at delete time);
* the media of a message that is not sent yet or whose send failed — the file
  is what will be (re)sent;
* anything the user saved with Save As: that is a copy of their own, in a
  folder of their choice, and not in the cache.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from core.transcription.stored import is_unsent
from core.utils import group_media_category

#: Same categories as the group Media tab (core.utils.group_media_category).
MEDIA_CATEGORIES = ("photos", "videos", "audios", "voice_messages", "documents")


@dataclass
class CleanupCandidate:
    chat_jid: str
    message_id: str
    category: str
    size_bytes: int
    path: str


@dataclass
class CleanupPreview:
    candidates: list = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return len(self.candidates)

    @property
    def total_bytes(self) -> int:
        return sum(c.size_bytes for c in self.candidates)

    @property
    def chats_affected(self) -> int:
        return len({c.chat_jid for c in self.candidates})

    def by_category(self) -> dict:
        """{category: (count, bytes)}, only for categories that matched."""
        out: dict = {}
        for c in self.candidates:
            count, total = out.get(c.category, (0, 0))
            out[c.category] = (count + 1, total + c.size_bytes)
        return out


@dataclass
class CleanupStats:
    files_deleted: int = 0
    bytes_freed: int = 0
    files_missing: int = 0   # already gone by the time it ran — not an error
    files_failed: int = 0    # in use or refused by the system
    cancelled: bool = False
    #: The ids of the messages whose file was deleted here, for the index of
    #: cleaned media (core/media_cleaned.py).
    deleted_ids: list = field(default_factory=list)


def _default_path_for(msg_type: str, msg_id: str) -> str:
    # Imported here: ui.conversation_panel pulls wx in, and the engine is
    # tested without it by passing `path_for`.
    from ui.conversation_panel.media_paths import cached_media_path
    return cached_media_path(msg_type, msg_id)


def scan(records_by_chat: dict, categories, min_size_bytes: int = 0, path_for=None,
         isfile=os.path.isfile, getsize=os.path.getsize) -> CleanupPreview:
    """Read-only: the cached media files of `records_by_chat` ({jid: [message,
    ...]}) whose category is in `categories` and whose size is at least
    `min_size_bytes`. A file shared by several messages is listed once."""
    wanted = set(categories)
    path_for = path_for or _default_path_for
    preview = CleanupPreview()
    seen = set()
    for jid, records in records_by_chat.items():
        for msg in records or ():
            if not isinstance(msg, dict) or is_unsent(msg) or msg.get("_send_failed"):
                continue
            category = group_media_category(msg)
            if not category or category not in wanted:
                continue
            msg_id = (msg.get("key") or {}).get("id", "")
            if not msg_id:
                continue
            path = path_for(msg.get("messageType") or msg.get("type") or "", msg_id)
            if not path or path in seen or not isfile(path):
                continue
            try:
                size = getsize(path)
            except OSError:
                continue
            if size < min_size_bytes:
                continue
            seen.add(path)
            preview.candidates.append(CleanupCandidate(
                chat_jid=jid, message_id=msg_id, category=category,
                size_bytes=size, path=path))
    return preview


def _inside(path: str, roots) -> bool:
    real = os.path.normcase(os.path.realpath(path))
    for root in roots:
        base = os.path.normcase(os.path.realpath(root))
        if real.startswith(base + os.sep):
            return True
    return False


def execute(preview: CleanupPreview, allowed_roots, progress_cb=None,
            should_cancel=None) -> CleanupStats:
    """Delete exactly the files `scan()` found, and only those inside
    `allowed_roots` (the two cache folders). Never re-scans."""
    stats = CleanupStats()
    total = len(preview.candidates)
    for index, candidate in enumerate(preview.candidates, start=1):
        if should_cancel is not None and should_cancel():
            stats.cancelled = True
            break
        if not _inside(candidate.path, allowed_roots):
            stats.files_failed += 1
        else:
            try:
                size = os.path.getsize(candidate.path)
                os.remove(candidate.path)
                stats.files_deleted += 1
                stats.bytes_freed += size
                stats.deleted_ids.append(candidate.message_id)
            except FileNotFoundError:
                stats.files_missing += 1
            except OSError:
                stats.files_failed += 1
        if progress_cb is not None:
            progress_cb(index, total)
    return stats


def chat_options(panels, is_locked=lambda jid: False) -> list:
    """(jid, name, is_group) for the chat picker, from the conversation lists
    already in memory (`panels`: [(chats, names), ...], main list first, then
    the archived one). Locked chats are left out: their media is not offered
    to a screen that lists them. Sorted by name."""
    options = []
    seen = set()
    for chats, names in panels:
        for chat, name in zip(chats or (), names or ()):
            jid = (chat or {}).get("remoteJid") or ""
            if not jid or jid in seen or is_locked(jid):
                continue
            seen.add(jid)
            options.append((jid, str(name or jid), jid.endswith("@g.us")))
    options.sort(key=lambda option: option[1].lower())
    return options
