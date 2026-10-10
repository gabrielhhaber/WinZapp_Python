"""Describe the fetched page before cached messages are merged into it."""

from datetime import datetime, timezone

from core.incremental_sync import message_id, message_timestamp, timestamp_seconds


def newest_message_seconds(messages):
    return max((timestamp_seconds(message_timestamp(m)) for m in messages), default=0)


def _utc_timestamp(seconds):
    if seconds <= 0:
        return "none"
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return "invalid"


def fetched_page_summary(messages, known_ids, newest_cached):
    """Counts/timestamps only; no names, numbers, message IDs or text.

    New-to-cache IDs can be *older* history. Count newer messages separately
    so backfill progress is never mistaken for recovery of missed live events.
    """
    fetched = {message_id(m): m for m in messages if message_id(m)}
    new_ids = set(fetched) - known_ids
    newer = sum(timestamp_seconds(message_timestamp(fetched[mid])) > newest_cached
                for mid in new_ids)
    return (len(fetched), len(new_ids), newer,
            _utc_timestamp(newest_message_seconds(fetched.values())),
            _utc_timestamp(newest_cached))
