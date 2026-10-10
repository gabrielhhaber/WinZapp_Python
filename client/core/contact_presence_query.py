"""Bounded snapshot reads through an already-known private-contact identity.

Only explicit modern pending permits the paired identity. A ready response,
including a withheld timestamp, is authoritative; failures are not pending.
"""
import re

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


def read_snapshot(targets, request, clock):
    """At most two reads; the second shares the original timeout allowance.

    request(target, timeout) is supplied by the caller, so this module has no
    HTTP client, wx or account dependencies. HTTP timeout is not a hard total
    wall-clock deadline; skip the second read if the first spent the allowance.
    """
    started = clock()
    for index, target in enumerate(targets[:2]):
        timeout = 10 if index == 0 else 10 - (clock() - started)
        if timeout <= 0:
            return None
        try:
            response = request(target, timeout)
            if response.status_code not in (200, 201):
                return None
            payload = response.json()
            snapshot = response_snapshot(payload)
            if snapshot is not None:
                return snapshot
            presence = payload.get("presence") if isinstance(payload, dict) else None
            if (not isinstance(presence, dict) or payload.get("status") != "success"
                    or presence.get("status") != "pending"
                    or presence.get("restricted") is True):
                return None
        except Exception:
            return None
    return None
