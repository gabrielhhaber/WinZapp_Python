"""The "~" WhatsApp puts before the name of a contact that is not saved.

Settings > User interface has an opt-out (on by default). The mark is applied
only where a name is shown (conversation rows and titles, message sender
labels), never to the stored name, to search text or to mentions.
"""
from core.phone_contacts import is_phone_synced, user_saved

MARK = "~"

#: Not people: never marked, whatever the contact record says.
_NOT_A_CONTACT_SUFFIXES = ("@g.us", "@broadcast", "@newsletter")


def unsaved_marker_enabled(settings) -> bool:
    ui = settings.get("user_interface") if isinstance(settings, dict) else None
    return not isinstance(ui, dict) or bool(ui.get("mark_unsaved_contacts", True))


def contact_is_saved(contact) -> bool:
    """Whether this record is in an address book: WhatsApp's own flag, a
    contact the user made in WinZapp, or one restored as phone-synced."""
    if not contact:
        return False
    return bool(contact.get("isMyContact")) or user_saved(contact) or is_phone_synced(contact)


def _candidate_jids(main_window, jid: str) -> list:
    jids = [jid]
    local, _, domain = jid.partition("@")
    if domain == "lid":
        phone = getattr(main_window, "_lid_to_phone", {}).get(jid, "")
        if phone:
            jids += [phone, phone.rsplit("@", 1)[0] + "@c.us"]
    else:
        jids.append(local + "@c.us")
        lid = getattr(main_window, "_phone_to_lid", {}).get(f"{local}@s.whatsapp.net", "")
        if lid:
            jids.append(lid)
    return jids


def is_unsaved_contact(main_window, jid: str) -> bool:
    jid = (jid or "").strip()
    if not jid or "@" not in jid or jid.endswith(_NOT_A_CONTACT_SUFFIXES):
        return False
    if jid.split("@", 1)[0].split(":")[0] == "0":
        return False  # WhatsApp's own system account
    is_self = getattr(main_window, "_is_self_jid", None)
    if is_self is not None and is_self(jid):
        return False
    lookup = getattr(main_window, "_get_contact_tolerant", None)
    if lookup is None:
        return False
    return not any(contact_is_saved(lookup(candidate))
                   for candidate in _candidate_jids(main_window, jid))


def marked_chat_name(main_window, chat: dict, name: str) -> str:
    """*name* as a conversation row shows it: a group's name is never marked."""
    jid = chat.get("remoteJid", "")
    return name if jid.endswith("@g.us") else mark_unsaved_name(main_window, jid, name)


def mark_unsaved_name(main_window, jid: str, name: str) -> str:
    """*name* with "~" in front when *jid* is a contact that is not saved.

    Placeholders and number fallbacks stay as they are: only a name WhatsApp
    itself would show as a profile name gets the mark.
    """
    if not name or not unsaved_marker_enabled(getattr(main_window, "settings", None)):
        return name
    if name.startswith(MARK):
        return name
    if getattr(main_window, "_is_bad_contact_name", lambda _n: False)(name):
        return name
    i18n = getattr(main_window, "i18n", None)
    if i18n is not None and name in {i18n.t(key) for key in (
            "unknown_contact", "unnamed_participant", "unknown_group", "self_chat_name")}:
        return name
    return f"{MARK}{name}" if is_unsaved_contact(main_window, jid) else name
