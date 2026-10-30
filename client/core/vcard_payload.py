"""The ``contactMessage`` payload built from a server contact-card message.

Both engines hand the card over as text: WPPConnect puts the vCard in the
message ``body`` (a ``multi_vcard`` carries a ``vcardList``), and the Neonize
bridge puts it in ``body`` with the card name in ``displayName``. The client
used to keep only the name, so the card arrived with no number to show, copy
or chat with.
"""


def _is_card(text) -> bool:
    return isinstance(text, str) and "BEGIN:VCARD" in text.upper()


def contact_message_payload(wpp_msg: dict) -> dict:
    """``contactMessage`` content for a vcard / multi_vcard server message."""
    body = wpp_msg.get("body")
    cards = []
    names = []
    for item in wpp_msg.get("vcardList") or []:
        if isinstance(item, dict):
            if _is_card(item.get("vcard")):
                cards.append(item["vcard"])
            if item.get("displayName"):
                names.append(item["displayName"])
        elif _is_card(item):
            cards.append(item)
    if not cards and _is_card(body):
        cards.append(body)
    display = (
        wpp_msg.get("displayName")
        or wpp_msg.get("vcardFormattedName")
        or (names[0] if names else "")
    )
    if _is_card(display):
        # some engines stuff the whole card into the name field
        cards.append(display)
        display = ""
    if not display and body and not _is_card(body):
        display = body
    # An empty name is deliberate: the UI then reads FN from the card, or shows
    # its own translated "unknown contact".
    payload = {"displayName": display}
    if cards:
        payload["vcard"] = "\n".join(cards)
    return payload
