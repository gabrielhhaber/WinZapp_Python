"""Shift+F5: resync one conversation, like F5 does for all of them.

F5 wipes every local chat and message and syncs from scratch. For a single
conversation the same wipe would throw away history older than the one page
get-messages returns -- history WhatsApp Web may no longer hold -- and would
leave the conversation empty if the request failed. So the conversation is
fetched first, through the normal sync_chat_messages() path, and only then are
local rows the server no longer has removed, and only where the server spoke:
between the oldest and the newest message it returned.

Kept regardless:
- anything older than the fetched window (older history, not contradicted);
- anything newer than it (arrived live while the request was in flight);
- local-only records: a message still sending, one that failed to send, or one
  cancelled while sending -- none of them is on the server yet, by definition.
"""

from core.incremental_sync import timestamp_seconds

#: Flags that mark a record WinZapp created and the server cannot know about.
_LOCAL_ONLY_FLAGS = ("_local_pending", "_send_failed", "_cancelled_awaiting_id")


def _record_id(record) -> str:
    return ((record or {}).get("key") or {}).get("id") or ""


def _record_ts(record) -> int:
    record = record or {}
    return timestamp_seconds(
        record.get("messageTimestamp") or record.get("timestamp") or record.get("t") or 0)


def stale_ids_in_fetched_window(records, fetched_ids) -> list:
    """Ids of *records* the server contradicts: inside its window, not returned.

    *records* is the conversation after the sync merged the fetched page into
    it; *fetched_ids* are the ids get-messages returned. Empty when nothing
    was fetched -- no answer is never a reason to delete.
    """
    fetched_ids = {i for i in (fetched_ids or ()) if i}
    fetched_ts = [_record_ts(r) for r in records or () if _record_id(r) in fetched_ids]
    fetched_ts = [ts for ts in fetched_ts if ts]
    if not fetched_ts:
        return []
    oldest, newest = min(fetched_ts), max(fetched_ts)
    stale = []
    for record in records or ():
        record_id = _record_id(record)
        if not record_id or record_id in fetched_ids:
            continue
        if any(record.get(flag) for flag in _LOCAL_ONLY_FLAGS):
            continue
        if oldest <= _record_ts(record) <= newest:
            stale.append(record_id)
    return stale
