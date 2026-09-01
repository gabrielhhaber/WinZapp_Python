"""Pure helpers for deciding and validating warm-cache message refreshes."""


def message_timestamp(message: dict) -> int:
    if not isinstance(message, dict):
        return 0
    try:
        return int(
            message.get("messageTimestamp")
            or message.get("timestamp")
            or message.get("t")
            or 0
        )
    except (TypeError, ValueError):
        return 0


def message_id(message: dict) -> str:
    if not isinstance(message, dict):
        return ""
    key = message.get("key") or {}
    return str(key.get("id") or "") if isinstance(key, dict) else ""


def chat_message_records(chat: dict) -> list:
    if not isinstance(chat, dict):
        return []
    outer = chat.get("messages") or {}
    if not isinstance(outer, dict):
        return []
    inner = outer.get("messages") or {}
    records = inner.get("records") if isinstance(inner, dict) else None
    return records if isinstance(records, list) else []


def chat_last_received_id(chat: dict) -> str:
    if not isinstance(chat, dict):
        return ""
    key = chat.get("lastReceivedKey")
    if not isinstance(key, dict):
        return ""
    direct = key.get("id") or key.get("_id")
    if direct:
        return str(direct)
    serialized = key.get("_serialized") or ""
    if serialized:
        parts = str(serialized).split("_")
        if len(parts) >= 3:
            return parts[-1]
    return ""


def chat_last_message_id(chat: dict) -> str:
    if not isinstance(chat, dict):
        return ""
    last = chat.get("lastMessage")
    return message_id(last) if isinstance(last, dict) else ""


def chat_sync_marker(chat: dict) -> dict:
    records = chat_message_records(chat)
    newest = max(records, key=message_timestamp, default={})
    try:
        activity = int((chat or {}).get("t", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        activity = 0
    try:
        unread = int((chat or {}).get("unreadCount", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        unread = 0
    return {
        "activity": activity,
        "unread_count": unread,
        "last_received_id": chat_last_received_id(chat),
        "last_message_id": chat_last_message_id(chat),
        "newest_local_id": message_id(newest),
        "newest_local_ts": message_timestamp(newest),
        "record_count": len(records),
    }


def _seconds(value) -> int:
    """A timestamp in seconds, whatever unit it arrived in.

    `t` comes from WhatsApp Web in seconds while a stored message's
    messageTimestamp is occasionally a millisecond value; comparing the two raw
    makes the local side look impossibly newer, which is the direction that
    silently skips a chat.
    """
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return ts // 1000 if ts > 1_000_000_000_000 else ts


def _id_changed(current_id: str, previous_id: str, newest_local_id: str) -> bool:
    if not current_id:
        return False
    if previous_id:
        return current_id != previous_id
    if newest_local_id:
        return current_id != newest_local_id
    # The server names a message and we have nothing to compare it against.
    # Answering False here (which this did) is a guess that we already hold it;
    # the cost of guessing wrong is a chat that stays broken until F5, against
    # one extra get-messages for guessing right.
    return True


def local_history_behind_server(chat: dict, verified_activity: int = 0) -> bool:
    """The chat list claims activity newer than any message we actually stored.

    Every other signal here compares the current snapshot against the previous
    *snapshot*, and the baseline is a copy of that previous snapshot — so a
    round that merged a newer `t` without ever storing the message it belongs
    to poisons the next baseline with its own claim: the marker matches from
    then on, the chat is skipped every round, and only a forced full sync
    repairs it. That state is reachable in normal operation (see
    sync_chat_messages' _MAX_EMPTY_DELTA_RETRIES, which deliberately commits
    the activity marker of a delta that never produced a message), and it is
    the "the conversation stays stale until I press F5" report.

    This signal compares the server's claim against the content on disk
    instead, so no previously written marker can talk it out of a refresh.

    *verified_activity* is the activity value a get-messages for this chat
    already completed on. Without it the chats whose newest server-side event
    is one that never becomes a stored message (a filtered protocol row) would
    be re-fetched on every single round, forever.
    """
    marker = chat_sync_marker(chat)
    if marker["record_count"] <= 0:
        # No local page at all is classify_chat_sync()'s own case
        # (missing-local-history / new-chat); answering here only relabels it.
        return False
    floor = max(_seconds(marker["newest_local_ts"]), _seconds(verified_activity))
    return _seconds(marker["activity"]) > floor


def chat_sync_change_reason(chat: dict, baseline: dict,
                            verified_activity: int = 0) -> str:
    """Which signal says this chat changed, or "" when none does.

    Named per signal rather than returning a bare bool so the sync plan's log
    line says *why* a chat was queried — it used to report every incremental
    target as "activity-changed" whatever had actually moved, which made a
    planner bug impossible to tell apart from a genuinely quiet account.
    """
    if not baseline:
        return "no-baseline"
    current = chat_sync_marker(chat)
    try:
        previous_activity = int(baseline.get("activity", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        previous_activity = 0
    # Strictly forward, deliberately. A *lower* current activity than the
    # baseline is not worth a get-messages: sync_chat_messages() never removes
    # anything, so a fetch cannot act on a revoke, and the baseline is not
    # "what the server last said" — several paths raise chat["t"] locally
    # above the server's own marker (sync_chat_messages() itself, whenever the
    # newest displayable message is newer than `t`; on_historical_message(),
    # which writes an un-normalised millisecond timestamp straight into it).
    # Treating "different" as changed pins every such chat into a re-fetch on
    # every 60s poll round for the rest of the session, because the fetch it
    # triggers raises `t` back above the server value and re-arms the next
    # round — the exact outcome the long comment above sync_chat_messages()'
    # `if last_ts > current_t` exists to prevent. The state a forward-only
    # comparison misses, an activity marker committed without its message, is
    # what local_history_behind_server() below covers from the content side.
    if current["activity"] > previous_activity:
        return "activity-changed"

    current_unread = int(current.get("unread_count", 0) or 0)
    previous_unread = int(baseline.get("unread_count", 0) or 0)
    # One rule, not the two this used to carry: "the count rose" is fully
    # contained in "the count moved and there are unread messages now", since
    # a chat sitting at 0 cannot be above whatever it was before.
    if current_unread > 0 and current_unread != previous_unread:
        return "unread-changed"

    newest_local_id = str(baseline.get("newest_local_id") or "")
    if _id_changed(
        current["last_received_id"],
        str(baseline.get("last_received_id") or ""),
        newest_local_id,
    ):
        return "last-received-changed"
    if _id_changed(
        current["last_message_id"],
        str(baseline.get("last_message_id") or ""),
        newest_local_id,
    ):
        return "last-message-changed"
    if local_history_behind_server(chat, verified_activity):
        return "local-behind-server"
    return ""


def chat_sync_marker_changed(chat: dict, baseline: dict,
                             verified_activity: int = 0) -> bool:
    return bool(chat_sync_change_reason(chat, baseline, verified_activity))


def messages_overlap(fetched: list, local_records: list) -> bool:
    local_ids = {message_id(message) for message in (local_records or [])}
    local_ids.discard("")
    if not local_ids:
        return False
    return any(message_id(message) in local_ids for message in (fetched or []))


def classify_chat_sync(
    chat: dict,
    baseline: dict,
    *,
    force_full: bool = False,
    repair_needed: bool = False,
    server_claims_content: bool = False,
    verified_activity: int = 0,
) -> tuple[str, str]:
    """Return (mode, reason): mode is full, incremental, or skip."""
    if force_full:
        return "full", "forced-full"
    if not baseline:
        return "full", "new-chat"
    if repair_needed:
        return "full", "history-repair"
    if int(baseline.get("record_count", 0) or 0) == 0 and server_claims_content:
        return "full", "missing-local-history"
    reason = chat_sync_change_reason(chat, baseline, verified_activity)
    if reason:
        return "incremental", reason
    return "skip", "unchanged"


def next_incremental_limit(
    current_limit: int,
    page_size: int,
    response_count: int,
    has_overlap: bool,
) -> int:
    """Grow a saturated disjoint warm-cache window geometrically."""
    current = max(1, int(current_limit or 1))
    target = max(1, int(page_size or 1))
    if has_overlap or response_count < current or current >= target:
        return current
    return min(target, max(current + 1, current * 2))
