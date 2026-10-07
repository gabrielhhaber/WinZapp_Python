"""Presence snapshots shared by the button, dialog and instant announcement.

Times below measure receipt freshness, never the age of a person's last visit.
WPP's event ``t`` measures emission time and is deliberately a separate field.
"""
import math
import time

FRESH_SECONDS = 90
ONLINE = frozenset(("available", "composing", "recording"))


def timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        return int(number / 1000 if number > 1_000_000_000_000 else number) or None
    except (TypeError, ValueError, OverflowError):
        return None


def aliases(owner, jid):
    jid = owner._normalize_jid(jid)
    phone = getattr(owner, "_lid_to_phone", {}).get(jid, jid)
    lid = getattr(owner, "_phone_to_lid", {}).get(phone, "")
    return tuple(dict.fromkeys(x for x in (phone, jid, lid) if x))


def is_subscribed(owner, jid):
    """Whether a presence subscription for this contact (any alias) is live.

    subscribe_presence() drops an entry when the request fails and the cache is
    cleared on every new connection epoch, so a hit means "do not ask again".
    """
    subscribed = getattr(owner, "_subscribed_presence_cache", {})
    return any(key in subscribed for key in (jid, *aliases(owner, jid)))


def cached(owner, jid, *, now=None, fresh=True):
    cache = getattr(owner, "_presence_cache", {})
    entries = [cache[key] for key in aliases(owner, jid) if key in cache]
    entry = max(entries, key=lambda x: x.get("_received", 0), default={})
    now = time.monotonic() if now is None else now
    # Metadata-free entries support old providers/test fixtures; all new writes
    # go through merge() and carry freshness metadata.
    if fresh and "_received" in entry and now - entry["_received"] >= FRESH_SECONDS:
        return {}
    if fresh and entry.get("lastSeen") and now - entry.get("_seen_received", now) >= FRESH_SECONDS:
        return {**entry, "lastSeen": None}
    return entry


def merge(owner, jid, data, *, now=None):
    """Merge a verified snapshot/event; reject reordered WPP events."""
    now = time.monotonic() if now is None else now
    old = cached(owner, jid, now=now, fresh=False)
    raw_event = data.get("eventTimestamp")
    event = float(raw_event) if timestamp(raw_event) is not None else None
    if event is not None and event <= 1_000_000_000_000:
        event *= 1000  # Compare legacy seconds and current milliseconds alike.
    previous_event = old.get("_event")
    if event and previous_event and event < previous_event:
        return old
    state = data.get("lastKnownPresence", "")
    if state not in ONLINE | {"unavailable", "paused"}:
        state = ""
    if data.get("isOnline") is True and state not in ("composing", "recording"):
        state = "available"
    elif data.get("isOnline") is False:
        state = "unavailable"
    elif state == "paused":
        state = ""
    # A missing field is not an explicit privacy denial. Never retain a
    # last-seen value across an online transition or after its own expiry.
    last_seen = timestamp(data.get("lastSeen"))
    seen_at = now
    if "lastSeen" not in data and state not in ONLINE:
        if now - old.get("_seen_received", -FRESH_SECONDS) < FRESH_SECONDS:
            last_seen = old.get("lastSeen")
            seen_at = old.get("_seen_received", now)
    if state in ONLINE or data.get("restricted") is True:
        last_seen = None
    entry = {"lastKnownPresence": state, "lastSeen": last_seen,
             "_received": now, "_seen_received": seen_at,
             "_version": old.get("_version", 0) + 1,
             "_event": event or previous_event}
    if not hasattr(owner, "_presence_cache"):
        owner._presence_cache = {}
    for key in aliases(owner, jid):
        owner._presence_cache[key] = entry
    return entry


def response_snapshot(payload):
    """Parse the current API snapshot and older scalar last-seen responses.

    None means retryable/unknown; an explicit false/null means unavailable.
    """
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    presence = payload.get("presence")
    if isinstance(presence, dict):
        if (presence.get("status") != "ready"
                or not isinstance(presence.get("isOnline"), bool)
                or "lastSeen" not in presence
                or (presence["lastSeen"] is not None and timestamp(presence["lastSeen"]) is None)):
            return None
        return {"lastKnownPresence": presence.get("state", ""),
                "isOnline": presence.get("isOnline"),
                "lastSeen": timestamp(presence.get("lastSeen")),
                "restricted": presence.get("restricted") is True}
    if "response" not in payload:
        return None
    value = payload["response"]
    if isinstance(value, dict):
        if "lastSeen" not in value and "t" not in value:
            return None
        value = value.get("lastSeen", value.get("t"))
    if value is True or (value not in (None, False) and timestamp(value) is None):
        return None
    return {"lastKnownPresence": "unavailable", "lastSeen": timestamp(value)}
