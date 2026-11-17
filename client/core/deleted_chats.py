"""Which "deleted" marks a conversation coming back lifts.

Deleting a conversation marks its JID (and the @lid/number counterpart known
then) in MainWindow._deleted_chats; the list hides every marked JID. The mark
was lifted only for the exact JID a new message came under, so a conversation
deleted under one form and reopened under another (the @lid learned later,
the Brazilian number with or without its 9th digit, a "Nova conversa" opened
under the typed number) stayed hidden while it was being used (reported
2026-11-17). Pure; tested in tests/test_deleted_chats.py.
"""
from core.phone_variants import other_brazilian_mobile_jid
from core.utils import person_dedup_key


def marks_to_lift(deleted, jid, lid_to_phone, phone_to_lid):
    """The JIDs of *deleted* that are the person (or group) *jid* is."""
    jid = str(jid or "")
    if not jid or not deleted:
        return set()
    if jid.endswith("@g.us"):
        return {jid} & set(deleted)
    forms = {jid, lid_to_phone.get(jid, ""), phone_to_lid.get(jid, "")}
    forms |= {other_brazilian_mobile_jid(form) for form in list(forms)}
    forms |= {phone_to_lid.get(form, "") for form in list(forms)}
    forms.discard("")
    key = person_dedup_key(lid_to_phone.get(jid, jid), lid_to_phone)
    return {mark for mark in deleted
            if mark in forms
            or (not mark.endswith("@g.us") and person_dedup_key(mark, lid_to_phone) == key)}
