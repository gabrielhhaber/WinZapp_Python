"""Explicit address-book and public-name changes; historical sync is silent."""
import msgmap


def address_book(s, e, name, jid):
    action = e.Action
    if not (action.HasField("fullName") or action.HasField("firstName")):
        return False
    s.db.set_contact_name(jid, name)
    if not getattr(e, "FromFullSync", False):
        payload = {"session": s.name, "contactId": msgmap.jid_wpp(jid),
                   "kind": "address-book", "name": name, "saved": bool(name)}
        if not name:
            payload["syncedToPhone"] = False
        elif action.HasField("saveOnPrimaryAddressbook"):
            payload["syncedToPhone"] = bool(action.saveOnPrimaryAddressbook)
        s.emit("contact-state-changed", payload)
    return True


def profile(s, jid, name):
    if not name:
        return
    s.db.contact(jid, pushname=name)
    s.emit("contact-state-changed", {"session": s.name, "contactId": msgmap.jid_wpp(jid),
                                   "kind": "profile", "name": name})
