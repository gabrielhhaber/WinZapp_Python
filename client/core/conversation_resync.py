"""Shift+F5: resync one conversation, like F5 does for all of them.

F5 wipes every local chat and message and syncs from scratch. For a single
conversation the same wipe would throw away history older than the one page
get-messages returns -- history WhatsApp Web may no longer hold -- and would
leave the conversation empty if the request failed. So the conversation is
fetched first, through the normal sync_chat_messages() path, and only then are
local rows the server no longer has removed, and only where the server spoke.

Which local rows the answer can speak for follows the rules the open-chat
deletion mirror learned the hard way (core/remote_deletions.py,
comparable_local_records()), reused rather than restated:

- only real content (is_countable_message): a reaction, a system event or an
  edit event is not something get-messages lists under that id;
- never a synthetic id (`_rxn_<target>...` is how WinZapp stores a reaction --
  a review caught the first version of this deleting reactions for good);
- only strictly newer than the oldest message returned: another message
  sharing that second is not proof of anything;
- never newer than the newest one returned (it arrived while the request was
  in flight), and never a local-only record (sending, failed, cancelled).
"""

from core.incremental_sync import timestamp_seconds
from core.remote_deletions import comparable_local_records

#: Flags that mark a record WinZapp created and the server cannot know about.
_LOCAL_ONLY_FLAGS = ("_local_pending", "_send_failed", "_cancelled_awaiting_id")


def _record_id(record) -> str:
    return ((record or {}).get("key") or {}).get("id") or ""


def _record_ts(record) -> int:
    record = record or {}
    return timestamp_seconds(
        record.get("messageTimestamp") or record.get("timestamp") or record.get("t") or 0)


def stale_ids_in_fetched_window(records, fetched_ids, is_content=lambda r: True) -> list:
    """Ids of *records* the server contradicts: inside its window, not returned.

    *records* is the conversation after the sync merged the fetched page into
    it; *fetched_ids* are the ids get-messages returned; *is_content* is the
    caller's is_countable_message. Empty when nothing was fetched -- no answer
    is never a reason to delete.
    """
    fetched_ids = {i for i in (fetched_ids or ()) if i}
    fetched_ts = [_record_ts(r) for r in records or () if _record_id(r) in fetched_ids]
    fetched_ts = [ts for ts in fetched_ts if ts]
    if not fetched_ts:
        return []
    oldest, newest = min(fetched_ts), max(fetched_ts)
    candidates = comparable_local_records(
        list(records or ()), 0, float("inf"), oldest, is_content)
    return [
        _record_id(r) for r in candidates
        if _record_id(r) not in fetched_ids
        and _record_ts(r) <= newest
        and not any(r.get(flag) for flag in _LOCAL_ONLY_FLAGS)
    ]
