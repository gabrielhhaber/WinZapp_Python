"""Rules about messages that the bridge decides before calling neonize: when an
edit is refused and who signs a delete-for-everyone.

Edit: WhatsApp only accepts editing your own message for about 15 minutes.
Refusing here, before calling neonize, lets the client know for certain that
nothing went out and undo the optimistic text (main_window/message_actions.py);
a failure after the send stays "unknown" to it.

Pure functions, tested in test_message_rules.py.
"""

import msgmap

#: WhatsApp's edit window, in seconds
EDIT_WINDOW = 15 * 60


def is_channel_chat(jid):
    """True for a channel (newsletter, ids starting 120363) chat. The app has no
    channels tab, so what channels post must never become a conversation."""
    return str(jid or "").endswith("@newsletter")


def refusal_reason(from_me, original, now):
    """'not_mine', 'not_stored' or 'outside_window' if the edit cannot go
    out; '' if it can. `original` is the stored message (or None)."""
    if not from_me:
        return "not_mine"
    if original is None:
        return "not_stored"
    when = original.get("t") or 0
    if when and now - when > EDIT_WINDOW:
        return "outside_window"
    return ""


def revocation_author(from_me, group, me, id_author, original):
    """The JID (whatsmeow) that signs the 'delete for everyone' ('' if it
    cannot be deleted). My message: me. Someone else's can only be deleted in a
    group (the admin revokes it), and the revocation carries its author."""
    if from_me:
        return me
    if not group:
        return ""
    if id_author:
        return id_author
    return msgmap.jid_whatsmeow(original.get("author")) if original.get("author") else ""
