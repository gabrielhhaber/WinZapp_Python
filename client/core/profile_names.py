"""One profile name per person, kept in step on every address of theirs.

A person is known under up to three addresses (phone JID, its ``@c.us`` twin,
``@lid``) and the name WhatsApp shows for them (their profile name, "pushName")
was filed in several places: the contact record of each address (``name`` and
``pushName``), the presence-learned map and the private chat. A rename reached
only some of those, and ``name`` (copied from the old profile name when the
contact was first listed) kept shadowing the new ``pushName``. Messages of the
same person then showed whichever name their own address resolved to first,
and the old and new names alternated from row to row.

``apply_profile_name`` is the single writer: it files the name under every
address, moves ``name`` along unless an address book names the person, and
refuses a name older than the one already filed, so replaying an old message
(a history sync, an older page) never brings a previous name back.
"""
from core.chat_contact_names import find_contact_key
from core.unsaved_contact_marker import contact_is_saved


def apply_profile_name(contacts: dict, ppm: dict, stamps: dict, targets, name: str,
                       ts: float, *, authoritative: bool, chats: dict | None = None) -> bool:
    """File *name* for every address in *targets* (the same person).

    *ts* is when WhatsApp said the person had that name (a message's
    timestamp; "now" for a live ``contacts.update``). *stamps* remembers, per
    address, the newest *ts* applied, so an older name is dropped.

    *authoritative* is False for a replay of history: it then only fills an
    address that has no name yet, because the name already stored came from
    something newer than what the replay cannot tell. Returns True when
    anything changed."""
    name = (name or "").strip()
    if not name:
        return False
    changed = False
    for target in dict.fromkeys(t for t in targets if t):
        seen = stamps.get(target, 0)
        if ts < seen:
            continue
        key = find_contact_key(contacts, target)
        record = contacts.get(key) if key else None
        previous = (ppm.get(target) or (record or {}).get("pushName") or "").strip()
        if not authoritative and previous and seen == 0:
            continue
        if ts > seen:
            stamps[target] = ts
        stale = {previous, ((record or {}).get("name") or "").strip()} - {""}
        if ppm.get(target) != name:
            ppm[target] = name
            changed = True
        if record is not None:
            old_push = (record.get("pushName") or "").strip()
            if old_push != name:
                record["pushName"] = name
                changed = True
            # `name` of a record nobody saved is only the profile name it was
            # listed with; it moves with it. An address-book name stays.
            if not contact_is_saved(record):
                old_name = (record.get("name") or "").strip()
                if old_name != name:
                    record["name"] = name
                    changed = True
        chat = (chats or {}).get(target)
        if chat is not None and not str(target).endswith("@g.us"):
            for field in ("name", "pushName"):
                old = (chat.get(field) or "").strip()
                if old and old != name and old in stale:
                    chat[field] = name
                    changed = True
    return changed
