"""Address-book observations never delete history or overwrite local names."""
import logging
import time
import wx

from core.chat_contact_names import find_contact_key
from core.phone_contacts import SYNCED_KEY, SYNCED_AT_KEY, user_saved
from core.remote_message_state import chat_target, chat_aliases


def contact_state_target(data):
    if not isinstance(data, dict):
        return None
    jid = chat_target(data.get("contactId"))
    kind, name = data.get("kind"), data.get("name")
    if not jid or jid.endswith("@g.us") or not isinstance(name, str) or len(name) > 4096:
        return None
    if kind == "profile":
        return (jid, kind, name.strip(), None, None) if name.strip() else None
    saved, synced = data.get("saved"), data.get("syncedToPhone")
    if (kind != "address-book" or type(saved) is not bool or (saved and not name.strip())
            or ("syncedToPhone" in data and type(synced) is not bool)):
        return None
    return jid, kind, name.strip() if saved else "", saved, synced


def apply_contact_state(mw, jid, kind, name, saved, synced):
    if getattr(mw, "_shutting_down", False):
        return
    if kind == "profile" and mw._is_bad_contact_name(name):
        return
    contacts = mw.contacts
    aliases = chat_aliases(mw, jid)
    keys = {find_contact_key(contacts, alias) or alias for alias in aliases}
    protected = next((contacts[k] for k in keys if user_saved(contacts.get(k))), None)
    changed = {}
    for key in keys:
        current = contacts.get(key)
        if current is None and kind == "address-book" and not saved:
            continue
        record = dict(current or {"id": key, "remoteJid": key, "type": "contact"})
        if kind == "profile":
            record["pushName"] = name
        else:
            record["isMyContact"] = saved
            if not saved:
                record[SYNCED_KEY] = record["syncToAddressbook"] = False
                record.pop(SYNCED_AT_KEY, None)
            elif synced is not None:
                record[SYNCED_KEY] = record["syncToAddressbook"] = synced
                record[SYNCED_AT_KEY] = time.time()
            if protected is None:
                record["name"] = record["shortName"] = record["formattedName"] = name
            else:
                record["name"] = protected.get("name", "")
        if record != current:
            contacts[key] = changed[key] = record
    if not changed:
        return
    db = getattr(mw, "db", None)
    if db is not None:
        try:
            db.upsert_contacts_batch(changed)
        except Exception:
            logging.exception("[contact-state-changed] could not persist observation")
    phone = next((a for a in aliases if a.endswith("@s.whatsapp.net")), jid)
    lid = next((a for a in aliases if a.endswith("@lid")), "")
    mw._refresh_views_after_contact_change(phone, lid)


def on_contact_state(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    target = contact_state_target(data)
    if target is not None:
        wx.CallAfter(apply_contact_state, client.main_window, *target)
