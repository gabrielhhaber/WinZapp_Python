"""The Contacts panel of the main navigation (issue #770): the address book,
one row per person, and what Enter does on a row.

Pure functions, tested in tests/test_contacts_tab.py; the panel is
ui/contacts_panel.py. Names are resolved by the caller: a JID never reaches
a row.
"""
from core.call_invites import person_addresses, saved_contact_jids, saved_name_of

#: What Enter on a contact does, in the order the panel offers them.
ENTER_ACTIONS = ("open", "call", "video", "info")
ENTER_ACTION_KEYS = {
    "open": "contacts_tab_open_chat",
    "call": "contacts_tab_voice_call",
    "video": "contacts_tab_video_call",
    "info": "contacts_tab_info",
}
SETTINGS_SECTION = "contacts_tab"


def enter_action(settings) -> str:
    """The action Enter takes (Settings' "contacts_tab"/"enter_action"),
    opening the conversation when nothing valid is stored."""
    section = (settings or {}).get(SETTINGS_SECTION) or {}
    action = section.get("enter_action") if isinstance(section, dict) else None
    return action if action in ENTER_ACTIONS else "open"


def contact_rows(contacts, lid_to_phone, name_of, number_of):
    """[(jid, name, number)] of the saved contacts, one row per person,
    sorted by name.

    A person kept under their number and their @lid (and the Brazilian 8/9
    digit forms) is one row (core/call_invites.person_addresses). The row's
    address is the number when one is known: a conversation, a call and the
    contact's details all start from it; an @lid stays only for someone whose
    number WhatsApp never gave. *name_of(jid)* is the display name,
    *number_of(jid)* the formatted number ('' for an @lid).
    """
    saved = saved_contact_jids(contacts)
    saved_name = saved_name_of(contacts)
    addresses = person_addresses(saved, lid_to_phone or {}, saved_name)
    people = {}
    for jid in saved:
        jid = jid.replace("@c.us", "@s.whatsapp.net")
        people.setdefault(addresses[jid], []).append(jid)
    rows = []
    for address, forms in people.items():
        phones = [j for j in forms if not j.endswith("@lid")]
        mapped = [lid_to_phone.get(j) for j in forms if j.endswith("@lid") and lid_to_phone.get(j)]
        jid = (phones or mapped or [address])[0]
        name = next((n for n in (" ".join(str(saved_name(j) or "").split()) for j in forms) if n), "")
        name = name or " ".join(str(name_of(jid) or "").split())
        number = "" if jid.endswith("@lid") else number_of(jid)
        rows.append((jid, name, number))
    rows.sort(key=lambda row: (row[1].casefold(), row[2]))
    return rows


def row_label(name, number, unknown) -> str:
    """The text of a row: the name, then the number when there is one."""
    name = name or unknown
    return f"{name}, {number}" if number and number != name else name


def matching_rows(rows, query):
    """The rows whose name or number contain *query* (case and accents of
    the name as typed; digits of the number alone)."""
    query = " ".join(str(query or "").split()).casefold()
    if not query:
        return list(rows)
    # a query made of a number (spaces, +, -, parentheses allowed) is
    # looked for in the numbers' digits
    digits = "".join(ch for ch in query if ch.isdigit())
    is_number = bool(digits) and all(ch.isdigit() or ch in " +-()." for ch in query)
    out = []
    for row in rows:
        _jid, name, number = row
        if query in (name or "").casefold() or (
                is_number and digits in "".join(ch for ch in number if ch.isdigit())):
            out.append(row)
    return out
