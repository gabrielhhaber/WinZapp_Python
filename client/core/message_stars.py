"""WhatsApp star observations and the explicit transition from local stars.

An old local star survives a remote False until the user explicitly syncs or
removes it. Missing remote metadata is unknown, never an unstar instruction.
"""

import time

STAR_FIELDS = ("starred", "_star_remote", "_star_local", "_star_observed_at")


def remote_star_state(raw, observed_at=None):
    value = raw.get("star") if isinstance(raw, dict) else None
    if not isinstance(value, bool):
        return {}
    return {"starred": value, "_star_remote": value,
            "_star_observed_at": observed_at or time.time_ns()}


def is_local_star(message):
    return bool(message.get("_star_local") or (
        message.get("starred") and "_star_remote" not in message))


def merge_star_state(incoming, stored):
    """Merge only star metadata; leave the incoming content/identity intact."""
    result = dict(incoming)
    local = incoming.get("_star_local", is_local_star(stored))
    source = incoming
    if ("_star_remote" not in incoming or
            stored.get("_star_observed_at", 0) > incoming.get("_star_observed_at", 0)):
        source = stored
        local = is_local_star(stored)
    if "_star_remote" in source:
        result.update({key: source[key] for key in STAR_FIELDS if key in source})
        result["_star_local"] = bool(local)
        result["starred"] = bool(local or source["_star_remote"])
    elif is_local_star(stored):
        result["starred"] = True
    return result


def carry_over_stars(messages, stored):
    by_id = {(m.get("key") or {}).get("id"): m for m in stored}
    for message in messages:
        previous = by_id.get((message.get("key") or {}).get("id"))
        if previous:
            merged = merge_star_state(message, previous)
            message.update({k: merged[k] for k in STAR_FIELDS if k in merged})


def confirmed_star_state(star):
    return {**remote_star_state({"star": star}), "_star_local": False}


def stamp_star_snapshot(messages, started):
    """A read started before a confirmation cannot undo that confirmation."""
    for message in messages:
        if "_star_observed_at" in message:
            message["_star_observed_at"] = min(message["_star_observed_at"], started)


def apply_remote_star(existing, incoming):
    if "_star_remote" not in incoming:
        return False
    merged = merge_star_state(incoming, existing)
    changed = any(existing.get(k) != merged.get(k) for k in STAR_FIELDS if k != "_star_observed_at")
    existing.update({k: merged[k] for k in STAR_FIELDS if k in merged})
    return changed
