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


def _id_changed(current_id: str, previous_id: str, newest_local_id: str) -> bool:
    if not current_id:
        return False
    if previous_id:
        return current_id != previous_id
    return bool(newest_local_id and current_id != newest_local_id)


def chat_sync_marker_changed(chat: dict, baseline: dict) -> bool:
    if not baseline:
        return True
    current = chat_sync_marker(chat)
    try:
        previous_activity = int(baseline.get("activity", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        previous_activity = 0
    if current["activity"] > previous_activity:
        return True

    current_unread = int(current.get("unread_count", 0) or 0)
    previous_unread = int(baseline.get("unread_count", 0) or 0)
    if current_unread > 0 and (current_unread != previous_unread or current["activity"] != previous_activity):
        return True
    if current_unread > previous_unread:
        return True

    newest_local_id = str(baseline.get("newest_local_id") or "")
    if _id_changed(
        current["last_received_id"],
        str(baseline.get("last_received_id") or ""),
        newest_local_id,
    ):
        return True
    if _id_changed(
        current["last_message_id"],
        str(baseline.get("last_message_id") or ""),
        newest_local_id,
    ):
        return True
    return False


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
    if chat_sync_marker_changed(chat, baseline):
        return "incremental", "activity-changed"
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
