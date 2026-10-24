"""Presence snapshots shared by the button, dialog and instant announcement.

Times below measure receipt freshness, never the age of a person's last visit.
WPP's event ``t`` measures emission time and is deliberately a separate field.
"""
import math
import time

FRESH_SECONDS = 90
ACTIVITY_SECONDS = 10
ACTIVITY = frozenset(("composing", "recording"))
ONLINE = frozenset(("available", "composing", "recording"))


def visible_online(entry):
    """Activity does not establish permission to display online presence."""
    return (not entry.get("restricted", False)
            and entry.get("isOnline", entry.get("lastKnownPresence") == "available") is True)


def activity(entry):
    state = entry.get("lastKnownPresence", "")
    return state if state in ACTIVITY else ""


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


def cached(owner, jid, *, now=None, fresh=True, chat=None):
    cache = getattr(owner, "_presence_cache", {})
    keys = [(chat, key) if chat else key for key in aliases(owner, jid)]
    entries = [cache[key] for key in keys if key in cache]
    entry = max(entries, key=lambda x: x.get("_received", 0), default={})
    now = time.monotonic() if now is None else now
    # Metadata-free entries support old providers/test fixtures; all new writes
    # go through merge() and carry freshness metadata.
    if fresh and "_received" in entry and now - entry["_received"] >= FRESH_SECONDS:
        return {}
    if fresh and "_online_received" in entry and now - entry["_online_received"] >= FRESH_SECONDS:
        entry = {**entry, "isOnline": None}
        if not activity(entry):
            entry["lastKnownPresence"] = ""
    if fresh and activity(entry) and "_activity_received" in entry and now - entry["_activity_received"] >= ACTIVITY_SECONDS:
        entry = {**entry, "lastKnownPresence": "available" if visible_online(entry) else
                 "unavailable" if entry.get("isOnline") is False else ""}
    if fresh and entry.get("lastSeen") and now - entry.get("_seen_received", now) >= FRESH_SECONDS:
        return {**entry, "lastSeen": None}
    return entry


def merge(owner, jid, data, *, now=None, chat=None):
    """Merge a verified snapshot/event; reject reordered WPP events.

    Group chatstate belongs to (chat, participant), never the private contact.
    """
    now = time.monotonic() if now is None else now
    old = cached(owner, jid, now=now, fresh=False, chat=chat)
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
    current = cached(owner, jid, now=now, chat=chat)
    online = data.get("isOnline")
    online_received = now
    if not isinstance(online, bool):
        if state in ("available", "unavailable"):
            online = state == "available"
        else:
            online = current.get("isOnline")
            online_received = old.get("_online_received", now)
    restriction = data.get("restricted")
    restricted = (restriction if isinstance(restriction, bool)
                  else old.get("restricted") is True)
    # Legacy providers have no permission field. An actual returned timestamp
    # is authoritative; a missing/empty or malformed field is not permission.
    if "restricted" not in data and timestamp(data.get("lastSeen")) is not None:
        restricted = False
    if restricted:
        online = None
    if data.get("invalidated") is True:
        online, state = None, ""
    # A real activity event may coexist with a hidden/offline online flag.
    if state not in ACTIVITY:
        state = "available" if online is True else "unavailable" if online is False else ""
    # A missing field is not an explicit privacy denial. Never retain a
    # last-seen value across an online transition or after its own expiry.
    last_seen = timestamp(data.get("lastSeen"))
    seen_at = now
    if "lastSeen" not in data and state not in ONLINE:
        if now - old.get("_seen_received", -FRESH_SECONDS) < FRESH_SECONDS:
            last_seen = old.get("lastSeen")
            seen_at = old.get("_seen_received", now)
    if state in ACTIVITY or online is True or restricted or data.get("invalidated") is True:
        last_seen = None
    entry = {"lastKnownPresence": state, "lastSeen": last_seen,
             "isOnline": online, "restricted": restricted,
             "_online_received": online_received, "_activity_received": now,
             "_received": now, "_seen_received": seen_at,
             "_seen_version": (old.get("_seen_version", old.get("_version", 0))
                               if "lastSeen" not in data else old.get("_version", 0) + 1),
             "_version": old.get("_version", 0) + 1,
             "_event": event or previous_event}
    if not hasattr(owner, "_presence_cache"):
        owner._presence_cache = {}
    for key in aliases(owner, jid):
        owner._presence_cache[(chat, key) if chat else key] = entry
    return entry


def response_snapshot(payload):
    """Parse the current API snapshot and older scalar last-seen responses.

    None means unknown; it does not authorize a retry or identity fallback.
    An explicit valid false/null response means unavailable.
    """
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    presence = payload.get("presence")
    if isinstance(presence, dict):
        if (presence.get("status") != "ready"
                or not isinstance(presence.get("isOnline"), bool)
                or ("restricted" in presence and not isinstance(presence["restricted"], bool))
                or "lastSeen" not in presence
                or (presence["lastSeen"] is not None and timestamp(presence["lastSeen"]) is None)):
            return None
        snapshot = {"lastKnownPresence": presence.get("state", ""),
                    "isOnline": presence.get("isOnline"),
                    "lastSeen": timestamp(presence.get("lastSeen"))}
        if "restricted" in presence:
            snapshot["restricted"] = presence["restricted"]
        return snapshot
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
