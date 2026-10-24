"""Bounded snapshot reads through an already-known private-contact identity.

Only explicit modern pending permits the paired identity. A ready response,
including a withheld timestamp, is authoritative; failures are not pending.
An explicit denial is authoritative even before the model becomes ready.
"""
import re
from dataclasses import dataclass

from core.contact_presence import response_snapshot

_PRIVATE_ID = re.compile(r"[0-9]+@(?:s\.whatsapp\.net|lid)\Z")


def query_targets(jid, lid_to_phone, phone_to_lid):
    """Keep the phone-first order; add only a reciprocal known PN/LID pair."""
    if not _PRIVATE_ID.fullmatch(jid):
        return ()
    phone = lid_to_phone.get(jid, jid)
    if not isinstance(phone, str) or not _PRIVATE_ID.fullmatch(phone):
        return ()
    lid = phone_to_lid.get(phone, "")
    if (phone.endswith("@s.whatsapp.net") and isinstance(lid, str) and lid.endswith("@lid")
            and _PRIVATE_ID.fullmatch(lid) and lid_to_phone.get(lid) == phone
            and jid in (phone, lid)):
        return phone, lid
    return (phone,)


@dataclass(frozen=True)
class PresenceRead:
    """Content-free outcome plus an optional validated cache snapshot."""
    outcome: str
    snapshot: dict | None = None


def read_presence(targets, request, clock):
    """At most two reads; the second shares the original timeout allowance.

    request(target, timeout) is supplied by the caller, so this module has no
    HTTP client, wx or account dependencies. HTTP timeout is not a hard total
    wall-clock deadline; skip the second read if the first spent the allowance.
    """
    started = clock()
    if not targets:
        return PresenceRead("unsupported")
    for index, target in enumerate(targets[:2]):
        timeout = 10 if index == 0 else 10 - (clock() - started)
        if timeout <= 0:
            return PresenceRead("error")
        try:
            response = request(target, timeout)
            if response.status_code not in (200, 201):
                return PresenceRead("error")
            payload = response.json()
            if not isinstance(payload, dict):
                return PresenceRead("malformed")
            if payload.get("status") != "success":
                return PresenceRead("error")
            presence = payload.get("presence")
            if "presence" in payload and not isinstance(presence, dict):
                return PresenceRead("malformed")
            if (isinstance(presence, dict) and presence.get("state") is not None
                    and not isinstance(presence["state"], str)):
                return PresenceRead("malformed")
            snapshot = response_snapshot(payload)
            if snapshot is not None:
                outcome = ("restricted" if snapshot.get("restricted") is True else
                           "withheld" if snapshot.get("lastSeen") is None
                           and snapshot.get("isOnline") is not True
                           and snapshot.get("lastKnownPresence") not in ("composing", "recording") else "ready")
                return PresenceRead(outcome, snapshot)
            if not isinstance(presence, dict):
                return PresenceRead("malformed")
            if presence.get("status") == "unsupported":
                return PresenceRead("unsupported")
            if presence.get("status") == "error":
                return PresenceRead("error")
            if presence.get("status") != "pending":
                return PresenceRead("malformed")
            if "restricted" in presence:
                if not isinstance(presence["restricted"], bool):
                    return PresenceRead("malformed")
                if presence["restricted"]:
                    # Permission is known although online/state are not. Carry
                    # the denial through both guarded cache-application paths;
                    # None would preserve old visible data in the profile path.
                    return PresenceRead("restricted", {"restricted": True, "lastSeen": None})
            # A pending model cannot also assert usable online/last-seen data.
            if ((presence.get("isOnline") is not None and presence["isOnline"] is not False)
                    or presence.get("lastSeen") is not None
                    or presence.get("state") not in (None, "")):
                return PresenceRead("malformed")
        except Exception:
            return PresenceRead("error")
    return PresenceRead("pending")


def read_snapshot(targets, request, clock):
    """Compatibility API for profile reads: unknown outcomes remain None."""
    return read_presence(targets, request, clock).snapshot
