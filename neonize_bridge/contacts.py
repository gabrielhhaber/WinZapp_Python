"""Contact routes of the bridge: block/unblock, check whether a number is on
WhatsApp, save to the address book and send a contact card. The response formats
are the ones the WinZapp client reads (core/phone_contacts.py,
main_window/chat_actions.py, main_window/sending.py) and the ones the reference
server builds (api_patches/src/controller/contactSaveController.ts,
deviceController.ts, middleware/statusConnection.ts).

The pure functions are at the top (tested in test_contacts.py);
`register_routes` wires the routes to the server. save-contact tells the
server first with a usync "delta" query (contact_sync.py), then writes the
app state entry ["contact", "<digits>@s.whatsapp.net"], as WhatsApp Web does.
`remove-contact` is in contact_removal.py (it needs WinZapp's neonize build).
"""
import asyncio
import logging
import re
import time

from aiohttp import web

import contact_sync
import msgmap
from groups import number_forms, user_of

log = logging.getLogger("bridge")

# Error codes the client understands (core/phone_contacts.py::_error_of).
KNOWN_CODES = (
    "contact_not_available", "contact_invalid", "contact_name_required",
    "contact_lid_without_phone", "contact_operation_failed",
)

# A contact only has these two id forms (the reference server blocks the
# rest: group, broadcast list, channel, free text).
_WID = re.compile(r"^\d{7,20}@(c\.us|lid)$")

_MIN_DIGITS = 7
_NAME_LIMIT = 100

# App state collection where WhatsApp keeps the address book.
CONTACTS_COLLECTION = "critical_unblock_low"
CONTACT_INDEX = "contact"
CONTACT_VERSION = 2


def contact_target(text) -> str:
    """What the client sent in `phone` -> whatsmeow's 'user@server', or '' if it
    is not a person (group, broadcast list, free text).

    Numbers become '@s.whatsapp.net'; a '@lid' stays '@lid' (the digits of an
    LID are not a phone)."""
    user, at_sign, server = str(text or "").strip().partition("@")
    user = user.split(":")[0]
    if not user.isdigit():
        return ""
    if server == "lid":
        return f"{user}@lid"
    if at_sign and server not in ("c.us", "s.whatsapp.net"):
        return ""
    return f"{user}@s.whatsapp.net" if len(user) >= _MIN_DIGITS else ""


_GROUP = re.compile(r"^[\d-]+@g\.us$")


def subscription_target(text) -> str:
    """What `subscribe-presence` received -> whatsmeow's 'user@server': a person
    or a group; '' for the rest (broadcast list, free text). Whether WhatsApp
    accepts subscribing to a group is for its answer to say, not the bridge."""
    text = str(text or "").strip()
    return text if _GROUP.match(text) else contact_target(text)


def first_destination(value):
    """The `phone` field arrives as text or as a list with one destination."""
    return value[0] if isinstance(value, list) and value else value


def equivalent_jids(jid: str) -> list:
    """The JID and, for a Brazilian mobile (`@s.whatsapp.net`), its other form
    (with or without the 9th digit). The requested form comes last, because
    `Store.contact_of` lets the last one it finds win. An LID never gets an
    alternative form: its digits are not a phone."""
    user, _, server = str(jid or "").partition("@")
    user = user.split(":")[0]
    if not user:
        return []
    if server not in ("s.whatsapp.net", "c.us") or not user.isdigit():
        return [jid]
    forms = number_forms(user)
    return [f"{u}@s.whatsapp.net" for u in reversed(forms)]


def book_identity(rows: dict, jid: str, phone: str = "") -> tuple:
    """(address-book name, public nickname) of the person behind a chat.

    `rows` maps whatsmeow's 'user@server' to the stored contact. A chat kept
    under an LID reads the LID's row (where the nickname usually is) and the
    number's rows (where the address book's name is, in either form of a
    Brazilian number); the exact number comes first. The name is empty when the
    person is not in the address book: a nickname is not a name the user gave."""
    candidates = [jid]
    if phone:
        candidates += [phone] + [form for form in equivalent_jids(phone) if form != phone]
    name = nickname = ""
    for candidate in candidates:
        found = rows.get(candidate) or {}
        name = name or str(found.get("name") or "").strip()
        nickname = nickname or str(found.get("pushname") or "").strip()
    return name, nickname


def blocklist_entry(jid: str, phone_jid: str = "") -> dict:
    """A row of `blocklist`. The client treats `phone` as PHONE digits
    (main_window/chat_actions.py::is_contact_blocked), so a block stored by LID
    comes out with the number's digits (`phone_jid`, from `get_pn_from_lid`); the
    LID stays in `id`. Without the number (LID not yet mapped) the LID's digits
    come out, which at least match the LID's own chat."""
    user, _, server = str(jid or "").partition("@")
    digits = user.split(":")[0]
    if server == "lid" and phone_jid and not phone_jid.endswith("@lid"):
        digits = user_of(phone_jid)
    return {"phone": digits, "id": {"_serialized": msgmap.jid_wpp(jid)}}


def pn_lid_response(phone: str, lid: str, found: dict) -> dict:
    """The body of `contact/pn-lid/:x`. `phoneNumber` and `contact` are the fields
    the client reads (main_window/identity.py::resolve_lid_jids_via_api);
    `phone` and `id` are those of `resolve_self_lid`. `name` is only the name
    saved in the address book, never the public nickname (which goes in `pushname`)."""
    number = {"_serialized": msgmap.jid_wpp(phone)}
    return {"lid": {"_serialized": lid}, "phone": number, "id": number,
            "phoneNumber": number,
            "contact": {"name": found.get("name") or None,
                        "pushname": found.get("pushname") or None}}


def contact_body(jid: str, phone: str, found: dict, verified_name: str = "") -> dict:
    """The `response` of `contact/:phone`. `verifiedName`/`isBusiness` feed the
    client's "business account" and "verified name" rows
    (core/contact_identity.py); `isBusiness` is always a boolean."""
    name = found.get("name") or ""
    nickname = found.get("pushname") or ""
    body = {"id": {"_serialized": msgmap.jid_wpp(phone or jid)},
            "name": name or nickname or None, "pushname": nickname or None,
            "formattedName": name or nickname or None,
            "isMyContact": bool(name), "isBusiness": bool(verified_name)}
    if verified_name:
        body["verifiedName"] = verified_name
    if phone:
        body["phoneNumber"] = {"_serialized": msgmap.jid_wpp(phone)}
    return body


def address_book_rows(entries, jid_text):
    """neonize's `contact.get_all_contacts()` -> (jid, saved name, nickname) rows
    for `Store.contacts_in_batch`. The saved name is FullName, else FirstName;
    an entry with neither name nor nickname says nothing and is left out."""
    rows = []
    for entry in entries or ():
        info = entry.Info
        name = (info.FullName or info.FirstName or "").strip()
        nickname = (info.PushName or "").strip()
        jid = jid_text(entry.JID)
        if jid and (name or nickname):
            rows.append((jid, name, nickname))
    return rows


def address_book_record(contact: dict, wpp_id: dict, my_jids) -> dict:
    """A row of `all-contacts`. The client ignores rows without `isMyContact` and
    `syncToAddressbook` (core/phone_contacts.py): a name in the address book is
    what says the contact is still saved on the phone. `my_jids`: the account's
    own ids (number and LID) in WhatsApp Web's format."""
    name = contact["name"] or None
    return {"id": wpp_id, "name": name, "pushname": contact["pushname"] or None,
            "shortName": name, "isMyContact": bool(name),
            "syncToAddressbook": bool(name),
            "isMe": wpp_id["_serialized"] in my_jids}


def address_book_records(contact_rows, wpp_id, my_jids, phone_of, lid_of) -> list:
    """The rows of `all-contacts`. A saved person who writes in a group under
    their LID read as not saved: the LID's own row has no address-book name, or
    there is no row at all. The name comes from the number's row (`phone_of(lid)`),
    and a saved number with no LID row gets one (`lid_of(phone)`); both give ''
    when whatsmeow does not know the other id."""
    rows = {c["jid"]: c for c in contact_rows}
    records = []
    for c in contact_rows:
        if c["jid"].endswith("@lid") and not c["name"]:
            name, _ = book_identity(rows, c["jid"], phone_of(c["jid"]))
            if name:
                c = {**c, "name": name}
        records.append(address_book_record(c, wpp_id(c["jid"]), my_jids))
    for c in contact_rows:
        if not (c["jid"].endswith("@s.whatsapp.net") and c["name"]):
            continue
        lid = lid_of(c["jid"])
        if lid.endswith("@lid") and lid not in rows:
            rows[lid] = c
            records.append(address_book_record(
                {"name": c["name"], "pushname": c["pushname"]}, wpp_id(lid), my_jids))
    return records


def found_in_query(responses):
    """neonize IsOnWhatsAppResponse -> {'jid': 'x@s.whatsapp.net', 'business': bool}
    for the first number WhatsApp knows, or None if none."""
    for item in responses or ():
        if item.IsIn and item.JID.User:
            return {"jid": f"{item.JID.User}@{item.JID.Server or 's.whatsapp.net'}",
                    "business": item.HasField("VerifiedName")}
    return None


def known_phone_jid(client, user: str):
    """The address WhatsApp itself gives the phone number `user` (the typed
    digits or, for a Brazilian mobile, the form with/without the 9th digit that
    is the registered one), as ('digits', 'server'), or None when it does not
    know the number or the query fails. Blocks on a neonize call."""
    digits = re.sub(r"\D", "", str(user or ""))
    if len(digits) < _MIN_DIGITS:
        return None
    try:
        responses = client.is_on_whatsapp(*["+" + f for f in number_forms(digits)])
    except Exception:
        log.exception("send-message: number lookup")
        return None
    found = found_in_query(responses)
    if not found:
        return None
    found_user, _, server = found["jid"].partition("@")
    return found_user.split(":")[0], server


def _wpp_id(user: str, server: str = "c.us") -> dict:
    return {"server": server, "user": user, "_serialized": f"{user}@{server}"}


def number_response(digits: str, found) -> dict:
    """The body of `check-number-status`. `numberExists` and `id._serialized` are
    the fields the reference server reads (middleware/statusConnection.ts); the
    id is the canonical one, which may differ from the typed one (9th digit)."""
    if found is None:
        return {"id": _wpp_id(digits), "status": 404, "isBusiness": False,
                "canReceiveMessage": False, "numberExists": False}
    user, _, server = found["jid"].partition("@")
    server = "c.us" if server == "s.whatsapp.net" else server
    return {"id": _wpp_id(user.split(":")[0], server), "status": 200,
            "isBusiness": bool(found.get("business")), "canReceiveMessage": True,
            "numberExists": True}


def to_wid(phone_number) -> str:
    """The contact's id in `save-contact`: only digits become 'digits@c.us'; a
    whole id ('...@lid') stays as it came. '' if it is not a contact."""
    raw = str(first_destination(phone_number) or "").strip()
    wid = raw if "@" in raw else re.sub(r"\D", "", raw) + "@c.us"
    wid = wid.replace("@s.whatsapp.net", "@c.us")
    return wid if _WID.match(wid) else ""


def validate_save(body) -> tuple:
    """Body of `save-contact` -> (fields, error_code). Fields: wid, first, last,
    sync. The order of the checks is the reference server's."""
    body = body if isinstance(body, dict) else {}
    wid = to_wid(body.get("phone"))
    if not wid:
        return None, "contact_invalid"
    first = str(body.get("name") or "").strip()[:_NAME_LIMIT]
    if not first:
        return None, "contact_name_required"
    return {"wid": wid, "first": first,
            "last": str(body.get("lastName") or "").strip()[:_NAME_LIMIT],
            # synced with the phone, unless the caller says not to
            "sync": body.get("syncAddressBook") is not False}, ""


def full_name(first: str, last: str) -> str:
    return f"{first} {last}".strip()


def address_book_fields(fields: dict, phone_jid: str, lid_jid: str = "") -> dict:
    """The fields of the ContactAction (app state notice) that saves the contact."""
    action = {"fullName": full_name(fields["first"], fields["last"]),
              "firstName": fields["first"],
              "saveOnPrimaryAddressbook": bool(fields["sync"]),
              "pnJID": phone_jid}
    if lid_jid:
        action["lidJID"] = lid_jid
    return action


def contact_error(code: str) -> tuple:
    """(HTTP status, body) of a contact error; an unknown code never carries an
    exception text (it may have a name and number)."""
    code = code if code in KNOWN_CODES else "contact_operation_failed"
    status = {"contact_not_available": 503, "contact_operation_failed": 502}.get(code, 400)
    return status, {"status": "error", "code": code}


def save_response(phone_jid: str, sync: bool) -> dict:
    """The `response` of `save-contact`: the id under which the contact was saved
    (the client uses it in place of the typed digits)."""
    user, _, server = phone_jid.partition("@")
    server = "c.us" if server == "s.whatsapp.net" else server
    return {"id": f"{user.split(':')[0]}@{server}", "isMyContact": True,
            "syncToAddressbook": bool(sync)}


def _escape_vcard(text: str) -> str:
    text = " ".join(str(text).split())
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def vcard_of(name: str, number: str) -> str:
    """The vCard 3.0 card that WhatsApp shows as a contact; `waid` is what ties
    it to the number on WhatsApp."""
    name = _escape_vcard(name)
    return ("BEGIN:VCARD\nVERSION:3.0\n"
            f"N:;{name};;;\nFN:{name}\n"
            f"TEL;type=CELL;waid={number}:+{number}\n"
            "END:VCARD")


def card_name(found: dict, number: str, requested="") -> str:
    """Name of the card: what the caller asked for, else the address book's, else
    the public nickname, else the number itself."""
    return (str(requested or "").strip() or found.get("name") or found.get("pushname")
            or number)


def set_name(names) -> str:
    """displayName of a card with several contacts."""
    return ", ".join(names)


def register_routes(r, *, session, ok, jid_from_text, jid_text, destination_of, store_sent,
                    quoted_context):
    """Wire the contact routes to the aiohttp router `r`."""

    async def execute(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def error(message, status=500):
        return web.json_response({"status": "error", "message": str(message)}, status=status)

    def error_code(code):
        status, body = contact_error(code)
        return web.json_response(body, status=status)

    def connected(s):
        return s.client is not None and s.status == "CONNECTED"

    async def query_number(s, digits):
        """What WhatsApp found for the number (or for its other form), or None."""
        responses = await execute(
            s.client.is_on_whatsapp, *["+" + f for f in number_forms(digits)])
        return found_in_query(responses)

    async def change_block(req, block):
        from neonize.utils.enum import BlocklistAction
        s = session(req)
        body = await req.json()
        if not connected(s):
            return error("Disconnected", 400)
        target = contact_target(first_destination(body.get("phone")))
        if not target:
            return error("invalid contact", 400)
        action = BlocklistAction.BLOCK if block else BlocklistAction.UNBLOCK
        jid = jid_from_text(target)
        try:
            try:
                await execute(s.client.update_blocklist, jid, action)
            except Exception:
                if not target.endswith("@s.whatsapp.net"):
                    raise
                # a number that WhatsApp only knows by the LID: try through it
                lid = await execute(s.client.get_lid_from_pn, jid)
                if not getattr(lid, "User", ""):
                    raise
                await execute(s.client.update_blocklist, lid, action)
        except Exception:
            log.exception("block-contact" if block else "unblock-contact")
            return error("Error on block contact" if block else "Error on unblock contact")
        return ok(status="success", response={
            "message": "Contact blocked" if block else "Contact UnBlocked"})

    async def block_contact(req):
        return await change_block(req, True)

    async def unblock_contact(req):
        return await change_block(req, False)

    async def check_number_status(req):
        s = session(req)
        if not connected(s):
            return error("Disconnected", 400)
        digits = user_of(req.match_info["phone"])
        if not digits.isdigit() or len(digits) < _MIN_DIGITS:
            return ok(status="success", response=number_response(digits, None))
        try:
            found = await query_number(s, digits)
        except Exception:
            log.exception("check-number-status")
            return error("Error on check number status")
        return ok(status="success", response=number_response(digits, found))

    def send_to_address_book(s, phone_jid, action):
        """Writes the contact to WhatsApp's address book (and, with
        `saveOnPrimaryAddressbook`, the phone's) through an app state notice. Runs
        on a thread. Does not use Session.send_state: it only repairs the
        'regular' collections."""
        from neonize.proto.Neonize_pb2 import MutationInfo, PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            ContactAction, SyncActionValue)
        patch = PatchInfo(
            Timestamp=time.time_ns(), Type=PatchInfo.CRITICAL_UNBLOCK_LOW,
            Mutations=[MutationInfo(
                Index=[CONTACT_INDEX, phone_jid], Version=CONTACT_VERSION,
                Value=SyncActionValue(contactAction=ContactAction(**action)))])
        try:
            s.client.send_app_state(patch)
        except Exception as e:
            if "LTHash" not in str(e) and "409" not in str(e):
                raise
            # collection out of sync: download it again and try once more
            if not s.repair_collection(CONTACTS_COLLECTION):
                raise
            s.client.send_app_state(patch)

    async def save_contact(req):
        s = session(req)
        fields, code = validate_save(await req.json())
        if code:
            return error_code(code)
        if not connected(s):
            return error_code("contact_not_available")
        wid = fields["wid"]
        lid = ""
        if wid.endswith("@lid"):
            # the digits of an LID are not a phone: needs the number behind it
            lid = wid
            try:
                digits = (await execute(s.client.get_pn_from_lid, jid_from_text(wid))).User
            except Exception:
                digits = ""
            if not digits:
                return error_code("contact_lid_without_phone")
        else:
            digits = user_of(wid)
        try:
            found = await query_number(s, digits)
        except Exception:
            log.exception("save-contact: number query")
            # a query that failed does not prove the number does not exist; 408 is
            # what the client retries (the reference server answers the same).
            # The message is the reference server's own text, in Portuguese.
            return web.json_response(
                {"response": None, "status": "Connected",
                 "message": "Não foi possível consultar o número."}, status=408)
        if found is None:
            return web.json_response(
                {"response": None, "status": "Connected",
                 "message": f"O número {digits} não existe."}, status=400)
        phone_jid = found["jid"]
        if not lid:
            try:
                found_lid = await execute(s.client.get_lid_from_pn, jid_from_text(phone_jid))
                lid = jid_text(found_lid)
            except Exception:
                lid = ""        # the LID is only a complement; the notice works without it
        action = address_book_fields(fields, phone_jid, lid)
        # the server learns of the new contact first, as WhatsApp Web does
        await execute(contact_sync.notify, s.client,
                      contact_sync.add_query(user_of(phone_jid)), "save")
        try:
            await execute(send_to_address_book, s, phone_jid, action)
        except Exception:
            log.exception("save-contact")
            return error_code("contact_operation_failed")
        # the bridge's local address book (all-contacts) and neonize's store now have the name
        s.db.contact(phone_jid, name=action["fullName"])
        try:
            await execute(s.client.contact.put_contact_name, jid_from_text(phone_jid),
                          action["fullName"], action["firstName"])
        except Exception:
            log.warning("save-contact: neonize's contact store not updated")
        return ok(status="success",
                  response=save_response(phone_jid, fields["sync"]))

    async def contact_vcard(req):
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
            ContactMessage, ContactsArrayMessage, Message)
        s = session(req)
        body = await req.json()
        if not connected(s):
            return error("Disconnected", 400)
        phone = first_destination(body.get("phone"))
        if not phone:
            return error("phone is required", 400)
        if body.get("isGroup") and "@" not in phone:
            phone += "@g.us"
        destination = destination_of(phone)
        ids = [i for i in body.get("contactsId") or [] if i]
        if not ids:
            return error("contactsId is required", 400)
        cards = []
        for contact in ids:
            target = contact_target(contact)
            if not target:
                return error("invalid contact", 400)
            number = user_of(target)
            if target.endswith("@lid"):
                try:
                    number = (await execute(s.client.get_pn_from_lid,
                                            jid_from_text(target))).User
                except Exception:
                    number = ""
                if not number:
                    return error("contact has no phone number yet", 400)
            found = s.db.contact_of(f"{number}@s.whatsapp.net")
            name = card_name(found, number, body.get("name") if len(ids) == 1 else "")
            cards.append(ContactMessage(displayName=name, vcard=vcard_of(name, number)))
        quoted = body.get("quotedMessageId")
        ctx = None
        if quoted:
            ctx = quoted_context(s, quoted)
            if ctx is None:
                return error("quoted message not found", 404)
        if len(cards) == 1:
            message = Message(contactMessage=cards[0])
            if ctx is not None:
                message.contactMessage.contextInfo.CopyFrom(ctx)
        else:
            message = Message(contactsArrayMessage=ContactsArrayMessage(
                displayName=set_name([c.displayName for c in cards]),
                contacts=cards))
            if ctx is not None:
                message.contactsArrayMessage.contextInfo.CopyFrom(ctx)
        try:
            resp = await execute(s.client.send_message, destination, message)
        except Exception:
            log.exception("contact-vcard")
            return error("Error on send contact vcard")
        return store_sent(s, destination, resp)

    r.add_post("/api/{tok}/block-contact", block_contact)
    r.add_post("/api/{tok}/unblock-contact", unblock_contact)
    r.add_get("/api/{tok}/check-number-status/{phone}", check_number_status)
    r.add_post("/api/{tok}/save-contact", save_contact)
    r.add_post("/api/{tok}/contact-vcard", contact_vcard)
