"""WhatsApp events on the Neonize engine: the same routes and socket event the
WPPConnect server answers (client/api_patches controller/eventController.ts,
read by client/core/events.py).

* An ``eventMessage`` becomes WhatsApp Web's ``event_creation`` (event_fields);
  its ``messageContextInfo.messageSecret`` is kept (store.event_secret), with
  the addresses its creator may be known by.
* An answer is an ``encEventResponseMessage`` and an edit (or a cancel) a
  ``secretEncryptedMessage`` of type EVENT_EDIT, both encrypted with that
  secret. whatsmeow keeps the secret but exports no decrypt or encrypt for
  either, so the algorithm is done here, as whatsmeow's msgsecret.go does it
  (generateMsgSecretKey, decryptMsgSecret): key = HKDF-SHA256(secret, info =
  original id + original sender + modification sender + use case), AES-GCM,
  with ``id\\0sender`` as additional data for an answer. LID and phone
  addresses are both tried, like whatsmeow's own "orig sender hack".
* An answer or an edit received is stored and announced as ``event-changed``.

What cannot be done here: an event with a call link (WhatsApp Web creates the
link with a server query neonize does not send); the route refuses it with
``event_call_unsupported``.

Pure functions first (test_events.py).
"""
import asyncio
import logging
import os
import time

from aiohttp import web

import msgmap

log = logging.getLogger("bridge")

USE_RESPONSE = "Event Response"
USE_EDIT = "Event Edit"
EVENT_EDIT = 1               # SecretEncryptedMessage.SecretEncType
MESSAGE_EDIT = 14            # ProtocolMessage.Type


def non_ad(jid) -> str:
    """'user:device@server' -> 'user@server' (whatsmeow's ToNonAD)."""
    jid = str(jid or "")
    user, _, server = jid.partition("@")
    return f"{user.split(':')[0]}@{server}" if server and user else ""


def secret_key(use_case, mod_sender, orig_id, orig_sender, secret):
    """(AES key, additional data) of whatsmeow's generateMsgSecretKey."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    info = (str(orig_id) + non_ad(orig_sender) + non_ad(mod_sender) + use_case).encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(bytes(secret))
    aad = f"{orig_id}\x00{non_ad(mod_sender)}".encode() if use_case == USE_RESPONSE else None
    return key, aad


def decrypt(secret, use_case, orig_id, orig_senders, mod_senders, iv, payload):
    """The plaintext, trying every (original sender, modification sender)
    pair, or None when none authenticates."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    for orig in dict.fromkeys(a for a in orig_senders if a):
        for mod in dict.fromkeys(a for a in mod_senders if a):
            key, aad = secret_key(use_case, mod, orig_id, orig, secret)
            try:
                return AESGCM(key).decrypt(bytes(iv), bytes(payload), aad)
            except InvalidTag:
                continue
    return None


def encrypt(secret, use_case, orig_id, orig_sender, mod_sender, plaintext, iv=None):
    """(ciphertext, iv)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = iv or os.urandom(12)
    key, aad = secret_key(use_case, mod_sender, orig_id, orig_sender, secret)
    return AESGCM(key).encrypt(iv, bytes(plaintext), aad), iv


def event_fields(event) -> dict:
    """An EventMessage as WhatsApp Web's event_creation fields."""
    location = ""
    if event.HasField("location"):
        location = ", ".join(p for p in (event.location.name, event.location.address) if p)
    return {
        "type": "event_creation", "body": event.name,
        "eventName": event.name, "eventDescription": event.description,
        "eventLocation": location, "eventJoinLink": event.joinLink,
        "eventStartTime": int(event.startTime or 0), "eventEndTime": int(event.endTime or 0),
        "isEventCanceled": bool(event.isCanceled),
        "eventExtraGuestsAllowed": bool(event.extraGuestsAllowed),
        "eventIsScheduledCall": bool(event.isScheduleCall),
        "eventHasReminder": bool(event.hasReminder),
        "eventReminderOffsetSec": int(event.reminderOffsetSec or 0),
    }


def own_addresses(s) -> list:
    return [a for a in (non_ad(getattr(s, "lid", "")), non_ad(getattr(s, "me", ""))) if a]


def sender_addresses(source, jid_text) -> list:
    """The addresses of who sent a message: its sender and the other address."""
    return [a for a in (non_ad(jid_text(source.Sender)),
                        non_ad(jid_text(getattr(source, "SenderAlt", None)))) if a]


def own_address_for(s, chat) -> str:
    """The address this account answers as in *chat*: its LID in a LID chat
    or a group (WhatsApp Web's getMeUserLidOrJidForChat), else its number."""
    lid, me = non_ad(getattr(s, "lid", "")), non_ad(getattr(s, "me", ""))
    if chat.endswith(("@lid", "@g.us")) and lid:
        return lid
    return me or lid


# ------------------------------------------------------------ receiving
def remember(s, chat, msg_id, from_me, source, message, jid_text):
    """An event arrived or was sent: keep its secret and creator's addresses."""
    inner = msgmap.unwrap(message)
    if not inner.HasField("eventMessage"):
        return
    secret = bytes(message.messageContextInfo.messageSecret or b"")
    if not secret:
        return
    senders = own_addresses(s) if from_me else sender_addresses(source, jid_text)
    s.db.event_secret(msg_id, chat=chat, senders=senders, secret=secret)


def received(s, chat, source, e, jid_text) -> bool:
    """True when the message was an answer to an event or an event edit
    (decrypted, stored and announced). Runs on neonize's thread."""
    inner = msgmap.unwrap(e.Message)
    if inner.HasField("encEventResponseMessage"):
        enc = inner.encEventResponseMessage
        target = enc.eventCreationMessageKey.ID
        kept = s.db.event_secret(target)
        if kept is None:
            log.info("event answer for an event whose secret is unknown")
            return True
        _chat, creators, secret = kept
        mine = bool(source.IsFromMe)
        senders = own_addresses(s) if mine else sender_addresses(source, jid_text)
        plain = decrypt(secret, USE_RESPONSE, target, creators, senders, enc.encIV, enc.encPayload)
        if plain is None:
            log.info("event answer not decrypted")
            return True
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import EventResponseMessage
        answer = EventResponseMessage.FromString(plain)
        who = "me" if mine else msgmap.jid_wpp(senders[0])
        s.db.event_response(target, who, int(answer.response), int(answer.extraGuestCount),
                            msgmap.seconds(e.Info.Timestamp))
        s.emit("event-changed", {"session": s.name, "kind": "responses", "id": target})
        return True
    if inner.HasField("secretEncryptedMessage") and \
            inner.secretEncryptedMessage.secretEncType == EVENT_EDIT:
        enc = inner.secretEncryptedMessage
        target = enc.targetMessageKey.ID
        kept = s.db.event_secret(target)
        if kept is None:
            return True
        _chat, creators, secret = kept
        senders = own_addresses(s) if source.IsFromMe else sender_addresses(source, jid_text)
        plain = decrypt(secret, USE_EDIT, target, creators, senders, enc.encIV, enc.encPayload)
        if plain is None:
            log.info("event edit not decrypted")
            return True
        apply_edit(s, target, plain)
        return True
    return False


def edited_event(plain):
    """The EventMessage inside a decrypted event edit, or None."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    msg = Message.FromString(plain)
    if msg.HasField("eventMessage"):
        return msg.eventMessage
    edited = msg.protocolMessage.editedMessage
    return edited.eventMessage if edited.HasField("eventMessage") else None


def apply_edit(s, target, plain):
    event = edited_event(plain)
    if event is None:
        return False
    fields = {k: v for k, v in event_fields(event).items() if k != "type"}
    if s.db.change_message(target, **fields):
        s.emit("event-changed", {"session": s.name, "kind": "edited", "id": target})
        return True
    return False


def responses_wpp(s, msg_id):
    """Entry of /event-responses, or None when the message is no event."""
    original = s.db.wpp_message(msg_id)
    if not original or original.get("type") != "event_creation":
        return None
    me = msgmap.jid_wpp(getattr(s, "me", "") or "")
    responses, mine = [], 0
    for who, response, guests in s.db.event_responses_of(msg_id):
        is_me = who == "me"
        if is_me:
            mine = response
        responses.append({"sender": me if is_me else who, "response": response,
                          "extraGuestCount": guests, "isMe": is_me})
    return {"responses": responses, "mine": mine}


# ------------------------------------------------------------ sending
def event_message(fields, secret):
    """The Message that creates an event."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
        EventMessage, LocationMessage, Message, MessageContextInfo)
    event = EventMessage(name=fields["name"].strip(), startTime=int(fields["startTime"]),
                         isCanceled=False,
                         extraGuestsAllowed=bool(fields.get("extraGuestsAllowed")))
    if fields.get("description"):
        event.description = fields["description"].strip()
    if fields.get("location"):
        event.location.CopyFrom(LocationMessage(name=fields["location"].strip()))
    if fields.get("endTime"):
        event.endTime = int(fields["endTime"])
    return Message(eventMessage=event, messageContextInfo=MessageContextInfo(messageSecret=secret))


def response_message(target_key, secret, creator, me, response, now_ms, chat):
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
        EncEventResponseMessage, EventResponseMessage, Message)
    plain = EventResponseMessage(response=int(response), timestampMS=int(now_ms)).SerializeToString()
    payload, iv = encrypt(secret, USE_RESPONSE, target_key.ID, creator, me, plain)
    return Message(encEventResponseMessage=EncEventResponseMessage(
        eventCreationMessageKey=target_key, encPayload=payload, encIV=iv))


def edit_message(target_key, secret, creator, me, event, now_ms):
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, SecretEncryptedMessage
    edit = Message()
    edit.protocolMessage.key.CopyFrom(target_key)
    edit.protocolMessage.type = MESSAGE_EDIT
    edit.protocolMessage.timestampMS = int(now_ms)
    edit.protocolMessage.editedMessage.eventMessage.CopyFrom(event)
    payload, iv = encrypt(secret, USE_EDIT, target_key.ID, creator, me, edit.SerializeToString())
    return Message(secretEncryptedMessage=SecretEncryptedMessage(
        targetMessageKey=target_key, encPayload=payload, encIV=iv, secretEncType=EVENT_EDIT))


def merged_event(original, fields, cancel):
    """The EventMessage of an edit: the stored event with what changes."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import EventMessage, LocationMessage
    pick = lambda key, current: fields[key] if key in fields else current  # noqa: E731
    event = EventMessage(
        name=str(pick("name", original.get("eventName") or "")).strip(),
        startTime=int(pick("startTime", original.get("eventStartTime") or 0)),
        isCanceled=bool(cancel))
    description = pick("description", original.get("eventDescription") or "")
    if description:
        event.description = str(description)
    location = pick("location", original.get("eventLocation") or "")
    if location:
        event.location.CopyFrom(LocationMessage(name=str(location)))
    end = pick("endTime", original.get("eventEndTime") or 0)
    if end:
        event.endTime = int(end)
    if original.get("eventJoinLink") and "callType" not in fields:
        event.joinLink = original["eventJoinLink"]
    return event


def register_routes(r, *, session, ok, destination_of, store_sent, in_thread):
    def error(code, status=400):
        return web.json_response({"status": "error", "code": code}, status=status)

    def connected(s):
        return s.client is not None and s.status == "CONNECTED"

    async def body_of(req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    def target_of(s, serial):
        """(MessageKey, chat, creator address, secret, stored message) of an event id."""
        from neonize.proto.waCommon.WACommon_pb2 import MessageKey
        from_me, conversation, mid, author = s.id_parts(serial)
        original = s.db.wpp_message(mid)
        kept = s.db.event_secret(mid)
        if not original or original.get("type") != "event_creation" or kept is None:
            return None
        chat, creators, secret = kept
        key = MessageKey(remoteJID=conversation, fromMe=from_me, ID=mid)
        if conversation.endswith("@g.us") and not from_me and author:
            key.participant = author
        creator = creators[0] if creators else non_ad(author or conversation)
        return key, chat or conversation, creator, secret, original

    async def event_responses(req):
        s = session(req)
        body = await body_of(req)
        out = {}
        for serial in (body.get("messageIds") or [])[:50]:
            _, _, mid, _ = s.id_parts(str(serial))
            entry = responses_wpp(s, mid)
            if entry is not None:
                out[serial] = entry
        return ok(status="success", response=out)

    async def event_respond(req):
        s = session(req)
        body = await body_of(req)
        response = body.get("response")
        if response not in (1, 2, 3):
            return error("event_response_invalid")
        if not connected(s):
            return error("event_response_unavailable", 503)
        found = target_of(s, str(body.get("messageId") or ""))
        if found is None:
            return error("event_not_found")
        key, chat, creator, secret, original = found
        if original.get("isEventCanceled"):
            return error("event_canceled")
        me = own_address_for(s, chat)
        message = response_message(key, secret, creator, me, response, time.time() * 1000, chat)
        try:
            await in_thread(lambda: s.client.send_message(destination_of(chat), message))
        except Exception as e:
            log.info("event answer refused (%s)", type(e).__name__)
            return error("event_response_failed", 503)
        s.db.event_response(key.ID, "me", response, 0, int(time.time()))
        s.emit("event-changed", {"session": s.name, "kind": "responses", "id": key.ID})
        return ok(status="success")

    async def send_event(req):
        s = session(req)
        body = await body_of(req)
        name, start = body.get("name"), body.get("startTime")
        if not isinstance(name, str) or not name.strip() or not isinstance(start, int) or start <= 0:
            return error("event_invalid")
        if body.get("callType") in ("voice", "video"):
            return error("event_call_unsupported")
        if not connected(s):
            return error("event_create_unavailable", 503)
        phone = str(body.get("phone") or "")
        destination = destination_of(phone)
        secret = os.urandom(32)
        message = event_message(body, secret)
        try:
            resp = await in_thread(lambda: s.client.send_message(destination, message))
        except Exception as e:
            log.info("event refused (%s)", type(e).__name__)
            return error("event_create_failed", 503)
        chat = msgmap.jid_whatsmeow(phone) if "@" in phone else phone
        msg_id = getattr(resp, "ID", "") or ""
        if msg_id:
            s.db.event_secret(msg_id, chat=chat, senders=own_addresses(s), secret=secret)
        answer = store_sent(s, destination, resp)
        return answer

    async def edit_event(req):
        s = session(req)
        body = await body_of(req)
        cancel = body.get("cancel") is True
        if body.get("callType") in ("voice", "video"):
            return error("event_call_unsupported")
        if not connected(s):
            return error("event_edit_unavailable", 503)
        found = target_of(s, str(body.get("messageId") or ""))
        if found is None:
            return error("event_not_found")
        key, chat, creator, secret, original = found
        if not key.fromMe:
            return error("event_not_mine")
        if original.get("isEventCanceled"):
            return error("event_canceled")
        fields = {} if cancel else {k: body[k] for k in (
            "name", "description", "location", "startTime", "endTime", "callType") if k in body}
        event = merged_event(original, fields, cancel)
        if not event.name or not event.startTime:
            return error("event_invalid")
        me = own_address_for(s, chat)
        message = edit_message(key, secret, creator, me, event, time.time() * 1000)
        try:
            await in_thread(lambda: s.client.send_message(destination_of(chat), message))
        except Exception as e:
            log.info("event edit refused (%s)", type(e).__name__)
            return error("event_edit_failed", 503)
        changed = {k: v for k, v in event_fields(event).items() if k != "type"}
        if s.db.change_message(key.ID, **changed):
            s.emit("event-changed", {"session": s.name, "kind": "edited", "id": key.ID})
        return ok(status="success")

    r.add_post("/api/{tok}/event-responses", event_responses)
    r.add_post("/api/{tok}/event-respond", event_respond)
    r.add_post("/api/{tok}/send-event", send_event)
    r.add_post("/api/{tok}/edit-event", edit_event)


def run_in_thread(func):
    return asyncio.get_running_loop().run_in_executor(None, func)
