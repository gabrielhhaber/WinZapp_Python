"""Which "deleted" marks a conversation coming back lifts.

Deleting a conversation marks its JID (and the @lid/number counterpart known
then) in MainWindow._deleted_chats; the list hides every marked JID. The mark
was lifted only for the exact JID a new message came under, so a conversation
deleted under one form and reopened under another (the @lid learned later,
the Brazilian number with or without its 9th digit, a "Nova conversa" opened
under the typed number) stayed hidden while it was being used (reported
2026-11-17). Pure; tested in tests/test_deleted_chats.py.
"""
import logging

from core.phone_variants import other_brazilian_mobile_jid
from core.pii_redaction import redact_phone
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


def restore(window, jid):
    """Lift every "deleted" mark of *jid*'s person on *window* (the main
    window, or a test's stub with ``_deleted_chats``) and save the set. True
    when a mark was lifted. A plain function so the message pipeline reaches
    it without the ChatActionsMixin method on every stub it is tested with."""
    deleted = getattr(window, "_deleted_chats", None)
    if not deleted:
        return False
    lifted = marks_to_lift(deleted, jid, getattr(window, "_lid_to_phone", None) or {},
                           getattr(window, "_phone_to_lid", None) or {})
    if not lifted:
        return False
    deleted.difference_update(lifted)
    if getattr(window, "db", None) is not None:
        window.db.set_metadata_json("deleted_chats", list(deleted))
    logging.info("[chats] %s was deleted locally - restored (%d mark(s) lifted).",
                 redact_phone(str(jid)), len(lifted))
    return True
