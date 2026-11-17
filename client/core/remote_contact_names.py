"""Learn public profile names without adding people to an address book."""
from core.chat_contact_names import find_contact_key, learn_chat_names


def profile_name(contact, invalid):
    """WPPConnect and Neonize use different casing for the same nickname."""
    for field in ("pushName", "pushname"):
        value = contact.get(field)
        if isinstance(value, str) and value.strip() and not invalid(value.strip()):
            return value.strip()
    return ""


def learn_remote_profiles(contacts, rows, normalize, invalid, lid_to_phone, phone_to_lid):
    """Cache nicknames even for group-only participants, keeping saved names.

    The phonebook filter is deliberately separate: a public nickname does not
    make its owner a saved contact. Known number/LID aliases learn it together.
    """
    learned = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        nickname = profile_name(row, invalid)
        wid = row.get("id")
        raw = row.get("remoteJid") or (wid.get("_serialized") if isinstance(wid, dict) else wid)
        if not isinstance(raw, str):
            continue
        jid = normalize(raw or "")
        if not nickname or not jid.endswith(("@s.whatsapp.net", "@lid")):
            continue
        linked = lid_to_phone.get(jid) or phone_to_lid.get(jid)
        before = {target: contacts.get(find_contact_key(contacts, target), {}).get("pushName")
                  for target in (jid, linked) if target}
        learn_chat_names(contacts, jid, "", nickname, linked=(linked,))
        for target in (jid, linked):
            if target:
                key = find_contact_key(contacts, target)
                if key and before[target] != contacts[key].get("pushName"):
                    learned[key] = contacts[key]
    return learned


def lid_phone_pairs(rows, normalize):
    """[(lid, phone)] of the `all-contacts` rows that say an @lid is a number:
    `phoneNumber` on an @lid row ('<digits>@c.us'; both engines send it). Without
    it a contact saved on the phone under both addresses stayed two people in
    WinZapp (2026-11-17: 144 of 189 saved @lid contacts of a real account)."""
    pairs = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        wid, number = row.get("id"), row.get("phoneNumber")
        raw = wid.get("_serialized") if isinstance(wid, dict) else wid
        number = number.get("_serialized") if isinstance(number, dict) else number
        if not isinstance(raw, str) or not isinstance(number, str):
            continue
        lid, phone = normalize(raw), normalize(number)
        if (lid.endswith("@lid") and phone.endswith("@s.whatsapp.net")
                and phone.split("@", 1)[0].isdigit()):
            pairs.append((lid, phone))
    return pairs
