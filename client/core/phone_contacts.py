"""Contacts that WhatsApp syncs to the phone's address book.

A local contact (NewContactDialog's first tab) lives only inside WinZapp. A
phone-synced one is saved in WhatsApp through the server's ``save-contact``
route (WPP.contact.save with syncAddressBook), which is the action WhatsApp
Web's own "add contact" runs, and ends up in the phone's address book.

Plain functions over an injectable ``post``, no wx and no network of their
own, so the request shapes, the error mapping and the record rules are tested
directly. The HTTP client is imported only when a request is made: the
database reads SYNCED_KEY and is_phone_synced() from here and has no use for
it.
"""

import re
import time
from collections import namedtuple

#: Set on the record of a contact that lives in the phone's address book.
#: Persisted as contacts.synced_to_phone (core/database.py).
SYNCED_KEY = "syncedToPhone"

#: When WinZapp itself saved the contact to the phone (time.time()). In memory
#: only: it lets a contact list that was requested BEFORE the save, and so
#: still says "not a contact", be told apart from WhatsApp's real answer.
SYNCED_AT_KEY = "_syncedToPhoneAt"

#: Shortest number WhatsApp can have; below it nothing is sent.
MIN_DIGITS = 7

#: i18n keys for each way saving can fail.
ERR_INVALID = "create_contact_error"
ERR_NOT_ON_WHATSAPP = "new_contact_phone_not_on_whatsapp"
ERR_LID_WITHOUT_PHONE = "new_contact_phone_lid_unknown"
ERR_FAILED = "new_contact_phone_failed"

# Answers that mean the contact is already not in the address book: removing
# it has nothing left to do.
_ALREADY_GONE = {"contact_not_found", "number_is_not_your_contact"}

_PHONE_SUFFIXES = ("@s.whatsapp.net", "@c.us")

#: What save_contact() answers. *jid* is the JID WhatsApp filed the contact
#: under (it may differ from the typed number in the Brazilian 9th digit);
#: *synced* is whether WhatsApp confirmed the sync with the phone.
SaveResult = namedtuple("SaveResult", "ok error_key jid synced")


def now() -> float:
    return time.time()


def _api_post(url, **kwargs):
    from core.api_client import api_post
    return api_post(url, **kwargs)


def digits_of(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def wire_id(jid: str) -> str:
    """What identifies *jid* to the server routes, or "" when it cannot.

    A phone number travels as bare digits. An @lid travels whole: its digits
    are not a phone number, and sending them as one would save or remove a
    stranger whose number happens to be those digits.
    """
    jid = (jid or "").strip()
    local, at, domain = jid.partition("@")
    local = local.split(":")[0]
    if at and domain == "lid":
        return f"{local}@lid" if local.isdigit() else ""
    if at and f"@{domain}" not in _PHONE_SUFFIXES:
        return ""       # a group, a broadcast, a newsletter: never a contact
    digits = digits_of(local)
    return digits if len(digits) >= MIN_DIGITS else ""


def jid_from_server(server_id, fallback: str) -> str:
    """The JID form WinZapp keys contacts by, out of the id a route answers."""
    if not isinstance(server_id, str) or "@" not in server_id:
        return fallback
    local, _, domain = server_id.partition("@")
    if domain == "c.us":
        return f"{local}@s.whatsapp.net"
    return server_id if domain in ("s.whatsapp.net", "lid") else fallback


def is_phone_synced(contact) -> bool:
    """Whether this record is a contact that lives in the phone's address book:
    one marked so (SYNCED_KEY: saved through WinZapp's synced tab, or restored
    from the database), or one WhatsApp itself reports as saved and synced
    (isMyContact + syncToAddressbook), e.g. added on the phone."""
    if not contact:
        return False
    return bool(contact.get(SYNCED_KEY)) or (
        bool(contact.get("isMyContact")) and bool(contact.get("syncToAddressbook")))


def user_saved(contact) -> bool:
    """Whether the USER made this record, on either tab of the new-contact
    dialog. Such a record keeps the name the user gave it, and follows the
    person from an @lid to their phone JID once that is learned.

    isSaved alone says so: both tabs set it and the database keeps it. The
    synced mark does NOT: the database restores it for every contact in the
    phone's address book (is_phone_synced() counts WhatsApp's own flags when
    it is written), and those records are WhatsApp's, not the user's.
    """
    return bool(contact) and bool(contact.get("isSaved"))


def other_digit_form(jid: str) -> str:
    """The same Brazilian mobile number with or without its 9th digit
    (5511999999999 <-> 551199999999), as a phone JID; "" when *jid* has no
    such twin. A contact can sit under either, depending on how it was added."""
    local, at, domain = (jid or "").partition("@")
    if not at or domain != "s.whatsapp.net" or not local.startswith("55"):
        return ""
    if len(local) == 13 and local[4] == "9":
        return f"{local[:4]}{local[5:]}@{domain}"
    if len(local) == 12:
        return f"{local[:4]}9{local[4:]}@{domain}"
    return ""


def _user_saved_for_phone(contacts: dict, phone_jid: str) -> bool:
    """Whether the user saved a contact for this phone number, under either of
    its 8/9-digit forms."""
    return any(user_saved(contacts.get(jid))
               for jid in (phone_jid, other_digit_form(phone_jid)) if jid)


def orphaned_saved_lid_copies(contacts: dict, lid_to_phone: dict) -> list:
    """The @lids whose record still says "saved by the user" although the user
    has no saved contact for the phone number behind them.

    Up to 1.1.1.x, deleting a local contact removed the record under the phone
    JID and left its copy under the @lid, isSaved and all. That only made the
    leftover count as a contact of the user's in the contact pickers — until a
    contact saved under an @lid began to follow the person to their phone JID
    (followed_contact()): those leftovers would follow too, and bring back
    contacts the user had deleted.

    Meant to be asked ONCE, before any contact can have been saved under an
    @lid on purpose (the first start of the version that allows it): from then
    on such a record is exactly what the user wants to keep.
    """
    return [lid_jid for lid_jid, phone_jid in list(lid_to_phone.items())
            if user_saved(contacts.get(lid_jid))
            and not _user_saved_for_phone(contacts, phone_jid)]


def followed_contact(contacts: dict, lid_jid: str, phone_jid: str):
    """The record to put under *phone_jid* so that a contact the user saved
    under *lid_jid* follows the person there, or None when nothing is to move.

    The user saves a contact under an @lid when that is all WinZapp knows of
    the person (the new-contact dialog, for a chat with no phone number yet).
    Once the phone is learned every lookup goes to the phone JID, and left
    under the @lid alone the contact could no longer be edited or deleted.

    Nothing moves when the @lid record is not the user's (a pushname cache
    entry, a contact WhatsApp reports), nor over a record the user saved for
    the phone number itself, under either of its 8/9-digit forms. What
    WhatsApp already knew of the phone JID is kept underneath, and so is the
    "in the phone's address book" mark of either record: the merged one is
    that contact, whatever name the user gave it.
    """
    lid_record = contacts.get(lid_jid)
    if not user_saved(lid_record) or _user_saved_for_phone(contacts, phone_jid):
        return None
    phone_record = contacts.get(phone_jid) or {}
    return {**phone_record, **lid_record, "id": phone_jid, "remoteJid": phone_jid,
            SYNCED_KEY: is_phone_synced(lid_record) or is_phone_synced(phone_record)}


def existing_contact(main_window, jid: str):
    """The record main_window.contacts holds for this phone JID, tolerant of the
    Brazilian 8/9-digit forms; None when there is none."""
    lookup = getattr(main_window, "_get_contact_tolerant", None)
    if lookup is not None:
        return lookup(jid)
    return (getattr(main_window, "contacts", None) or {}).get(jid)


def key_of(main_window, record, default: str) -> str:
    """The JID *record* is stored under in main_window.contacts.

    existing_contact() finds a record under the other 8/9-digit form of the
    JID it was asked for; whoever then deletes "the contact" has to delete
    that key, or the record stays behind with its buttons and its name.
    """
    # A copy: the contact sync adds keys to this dict from its own thread.
    for jid, candidate in list((getattr(main_window, "contacts", None) or {}).items()):
        if candidate is record:
            return jid
    return default


def available_modes(contact, modes: tuple, synced_mode: str) -> tuple:
    """The tabs a contact may be saved under. A number that is already a synced
    contact has only the synced tab: a local copy next to it would hide the
    real one behind a name only this WinZapp knows."""
    return (synced_mode,) if is_phone_synced(contact) else tuple(modes)


def local_entry(jid: str, full_name: str) -> dict:
    return {"remoteJid": jid, "name": full_name, "pushName": full_name, "isSaved": True}


def synced_entry(jid: str, full_name: str, confirmed: bool = True) -> dict:
    """The contact record a successful save to the phone leaves in WinZapp.

    It is marked as synced whether or not WhatsApp's answer already showed the
    sync (*confirmed*): the save was asked for with the sync and accepted, and
    the contact is in WhatsApp either way, so it must be edited and deleted
    through WhatsApp. If the sync really did not happen, the next contact list
    says so and clear_stale_marks() takes the mark away.
    """
    return {**local_entry(jid, full_name), "isMyContact": True,
            "syncToAddressbook": bool(confirmed), SYNCED_KEY: True, SYNCED_AT_KEY: now()}


def unmark(record: dict, is_my_contact: bool, sync_to_addressbook: bool) -> None:
    """Take the "in the phone's address book" mark off *record*, with the two
    WhatsApp flags that would otherwise still read as it (is_phone_synced())."""
    record[SYNCED_KEY] = False
    record["isMyContact"] = bool(is_my_contact)
    record["syncToAddressbook"] = bool(sync_to_addressbook)


def clear_stale_marks(contacts: dict, server_contacts, requested_at: float) -> list:
    """Apply WhatsApp's answer to the records still marked as synced.

    A contact removed on the phone keeps its mark here (nothing else clears
    it), so the list WhatsApp just sent is the truth: a marked record it
    reports as not saved, or saved without the sync, loses the mark and those
    two flags. Records saved to the phone at or after *requested_at* are left
    alone: that list was asked for before WhatsApp knew about them. Entries
    that carry no answer (neither flag present) change nothing.

    A number that is simply ABSENT from the list keeps its mark, on purpose.
    The server lists saved contacts and numbers with an open chat, so a
    contact removed on the phone with no chat left does drop out of it; but so
    does everyone while WhatsApp Web is still loading its store, and reading
    absence as "removed" would then unmark the whole address book. The mark
    that stays behind costs little: deleting such a contact still works (the
    server answers that it is not one, which counts as removed), and editing
    it offers only the synced tab, which saves it to the phone again.

    Returns the JIDs whose mark was cleared.
    """
    cleared = []
    for server in server_contacts or ():
        if not isinstance(server, dict):
            continue
        if "isMyContact" not in server or "syncToAddressbook" not in server:
            continue
        record = contacts.get(server.get("remoteJid") or "")
        if not record or not record.get(SYNCED_KEY):
            continue
        if server.get("isMyContact") and server.get("syncToAddressbook"):
            continue
        if record.get(SYNCED_AT_KEY, 0) >= requested_at:
            continue
        unmark(record, server.get("isMyContact"), server.get("syncToAddressbook"))
        cleared.append(server["remoteJid"])
    return cleared


def _url(base: str, token: str, route: str) -> str:
    return f"{base}/api/{token}/{route}"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _body(resp) -> dict:
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _error_of(resp) -> str:
    """The i18n key for a refused save."""
    code = _body(resp).get("code", "")
    if code == "contact_invalid":
        return ERR_INVALID
    if code == "contact_lid_without_phone":
        # An @lid whose phone number WhatsApp Web does not know yet: nothing
        # to retry now, the local tab is the way.
        return ERR_LID_WITHOUT_PHONE
    if resp.status_code == 400 and not code:
        # The server's contact validation: "the number does not exist".
        return ERR_NOT_ON_WHATSAPP
    return ERR_FAILED


def save_contact(base: str, token: str, jid: str, first: str, last: str = "",
                 post=_api_post) -> SaveResult:
    """Save the contact in WhatsApp, synced to the phone.

    Blocks for the length of the request: call it off the main thread.
    """
    target = wire_id(jid)
    if not target:
        return SaveResult(False, ERR_INVALID, jid, False)
    payload = {"phone": target, "name": first, "lastName": last, "syncAddressBook": True}
    try:
        resp = post(_url(base, token, "save-contact"), json=payload,
                    headers=_headers(token), timeout=20)
    except Exception:
        return SaveResult(False, ERR_FAILED, jid, False)
    if not resp.ok:
        return SaveResult(False, _error_of(resp), jid, False)
    answer = _body(resp).get("response")
    answer = answer if isinstance(answer, dict) else {}
    return SaveResult(True, "", jid_from_server(answer.get("id"), jid),
                      bool(answer.get("syncToAddressbook")))


def remove_contact(base: str, token: str, jid: str, post=_api_post) -> bool:
    """Remove the contact from WhatsApp and the phone. True when it is gone,
    including when it was already not a contact."""
    target = wire_id(jid)
    if not target:
        return False
    try:
        resp = post(_url(base, token, "remove-contact"), json={"phone": target},
                    headers=_headers(token), timeout=20)
    except Exception:
        return False
    return bool(resp.ok) or _body(resp).get("code", "") in _ALREADY_GONE
