"""Action routes on chats and messages of the bridge: clear and delete a chat
(one or all), archive all, mark as unread, typing state, download media and
receipts of a sent message.

Clearing and deleting a chat use the app state notice (clearChat / deleteChat,
the same indexes and version that WhatsApp Web and Baileys use), as archiving
and pinning already do in server.py: the phone and the other devices follow.
The bridge's store only changes after WhatsApp accepts the notice.

Nothing here was tested against a real account: neonize does not install
outside Windows. The response formats of clear-chat and message-ack are the ones
the client reads (core/utils.py clear_chat_applied, core/message_ack.py); the
other routes' the client does not use.

The pure functions are at the top (tested in test_chat_actions.py);
`register_routes` wires the routes to the server.
"""
import base64
import logging
import time

import msgmap
import responses
from chat_listing import boolean, chat_jid

log = logging.getLogger("bridge")

#: receipt stage -> field of the message-ack response
RECEIPT_FIELDS = {"delivered": "deliveredAt", "read": "readAt", "played": "playedAt"}

#: WPPConnect's `chatstate` value -> neonize's (presence, media)
TYPING_STATES = {
    0: ("CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_TEXT"),    # typing
    1: ("CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_AUDIO"),   # recording audio
    2: ("CHAT_PRESENCE_PAUSED", "CHAT_PRESENCE_MEDIA_TEXT"),       # stopped
}


def keep_starred(body):
    """clear-chat: a missing `keepStarred` keeps the starred ones (as WhatsApp
    does by default)."""
    value = body.get("keepStarred")
    return value if isinstance(value, bool) else boolean(value, True)


def phones(body):
    """`phone` as a list (the client sends a list; others send text)."""
    phone = body.get("phone")
    items = phone if isinstance(phone, list) else [phone]
    items = [str(f) for f in items if f]
    if not items:
        raise ValueError("phone is required")
    return items


def clear_index(keep):
    """Rest of the clearChat notice's index: [delete starred, "0"]. WhatsApp
    Web (WAWebChatClearBridge.sendClear, read 2026-11-09) always sends "0" in
    the last place, and so does Baileys."""
    return ("0" if keep else "1", "0")


#: What WhatsApp Web lists in a range, at most (WAWebDBMessageRange).
RANGE_MESSAGES_LIMIT = 1000
#: How many of the chat's most recent stored messages the range is built from.
RANGE_WINDOW = 300


def range_plan(rows, index_jid, group, system_types=("gp2", "call_log")):
    """The message range of a clearChat/deleteChat notice, the way WhatsApp
    Web builds it (WAWebMessageRangeUtils.constructForwardMovingMessageRange,
    read 2026-11-09), from the chat's most recent stored messages (`rows`,
    WPPConnect-format dicts):

    * `last`: the newest message's time (what the phone clears up to);
    * `last_system`: the newest system message's, when newer than that;
    * `messages`: our own messages from the newest received one on, and the
      received ones at that very second, as (from_me, id, participant, t) —
      the phone clears those by key, not trusting our clock.

    A key the phone's check refuses (WAWebMessageRangeUtils.validateMessageRange)
    is never listed: one refused key makes the whole notice malformed and the
    phone drops it. The bridge used to list only the newest stored item, which
    in a group was often a notification with no author: a received group
    message without `participant`, exactly such a key. Keys name the chat as
    the index does (`index_jid`)."""
    def ts(m):
        try:
            return int(m.get("t") or 0)
        except (TypeError, ValueError):
            return 0

    timed = [m for m in rows or () if isinstance(m, dict) and ts(m) > 0
             and (m.get("id") or {}).get("id")]
    system = [m for m in timed if m.get("type") in system_types]
    chat = [m for m in timed if m.get("type") not in system_types]
    received = [m for m in chat if not m.get("fromMe")]
    last = max((ts(m) for m in chat), default=0)
    last_received = max((ts(m) for m in received), default=0)
    last_system = max((ts(m) for m in system), default=0)
    listed = []
    for m in chat:
        own = bool(m.get("fromMe"))
        if own and ts(m) < last_received:
            continue
        if not own and ts(m) != last_received:
            continue
        participant = ""
        if group and not own:
            # checked before converting: jid_whatsmeow("") is "@s.whatsapp.net"
            author = str(m.get("author") or "").strip()
            if "@" not in author or author.startswith("@"):
                continue
            participant = msgmap.jid_whatsmeow(author)
        listed.append((own, m["id"]["id"], participant, ts(m)))
    listed.sort(key=lambda item: -item[3])
    plan = {"last": last or (last_system or int(time.time())),
            "last_system": last_system if last_system > last else 0,
            "messages": listed[:RANGE_MESSAGES_LIMIT], "chat": index_jid}
    if not last and last_system:
        # only system messages: they are the range's end (WhatsApp Web's
        # firstSystemMessageTimestamp fallback), and not said twice
        plan["last_system"] = 0
    return plan


def typing_state(value):
    """chatstate (0, 1 or 2, also as text) -> (presence, media), or ValueError."""
    try:
        return TYPING_STATES[int(value)]
    except (KeyError, TypeError, ValueError):
        raise ValueError("chatstate must be 0 (typing), 1 (recording) or 2 (paused)") from None


def typing_chatstate(active, is_recording):
    """typing/recording (value, which of the two) -> the chatstate of TYPING_STATES."""
    if not active:
        return 2
    return 1 if is_recording else 0


def receipt_participants(rows):
    """[(who, stage, ts)] -> the `participants` of message-ack: one per person,
    with deliveredAt/readAt/playedAt (seconds) of the stages they reached.
    Whoever read or played also received: without a delivery receipt, the
    delivery carries the time of the next stage."""
    by_person = {}
    for who, stage, ts in rows:
        field = RECEIPT_FIELDS.get(stage)
        if field is None:
            continue
        person = by_person.setdefault(who, {"id": who})
        if not person.get(field) or (ts and ts < person[field]):
            person[field] = int(ts or 0) or 1
    for person in by_person.values():
        if "deliveredAt" not in person:
            following = person.get("readAt") or person.get("playedAt")
            if following:
                person["deliveredAt"] = following
    return list(by_person.values())


def chat_range(s, conversation):
    """The range of messages of the state notice (`range_plan`), from the
    chat's stored messages under both its addresses. WhatsApp only applies
    clear/delete up to that range."""
    from neonize.proto.waCommon.WACommon_pb2 import MessageKey
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
        SyncActionMessage, SyncActionMessageRange)
    keys = s.chat_keys(conversation) or [conversation]
    plan = range_plan(s.db.messages(keys, RANGE_WINDOW), s.mutation_jid(conversation),
                      conversation.endswith("@g.us"))
    range_ = SyncActionMessageRange(lastMessageTimestamp=plan["last"])
    if plan["last_system"]:
        range_.lastSystemMessageTimestamp = plan["last_system"]
    for own, mid, participant, when in plan["messages"]:
        key = MessageKey(remoteJID=plan["chat"], fromMe=own, ID=mid)
        if participant:
            key.participant = participant
        range_.messages.append(SyncActionMessage(key=key, timestamp=when))
    return range_


def clear_on_phone(s, conversation, keep):
    """Sends the clearChat notice; clears the bridge's store if WhatsApp accepted."""
    from neonize.proto.Neonize_pb2 import PatchInfo
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
        ClearChatAction, SyncActionValue)
    accepted = s.send_state(
        "clearChat", 6, PatchInfo.REGULAR_HIGH,
        SyncActionValue(clearChatAction=ClearChatAction(messageRange=chat_range(s, conversation))),
        conversation, extra=clear_index(keep))
    if accepted:
        s.db.clear_chat(conversation, keep_starred=keep)
    return accepted


def delete_on_phone(s, conversation):
    """Sends the deleteChat notice; removes the conversation from the store if WhatsApp accepted."""
    from neonize.proto.Neonize_pb2 import PatchInfo
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
        DeleteChatAction, SyncActionValue)
    accepted = s.send_state(
        "deleteChat", 6, PatchInfo.REGULAR_HIGH,
        SyncActionValue(deleteChatAction=DeleteChatAction(messageRange=chat_range(s, conversation))),
        conversation, extra=("1",))
    if accepted:
        s.db.delete_chat(conversation)
    return accepted


def register_routes(r, *, session, ok, error, in_thread):
    """Wire the action routes on chats to the aiohttp router `r`."""

    def connected(s):
        return s.client is not None and s.status == "CONNECTED"

    async def body_of(req):
        try:
            body = await req.json()
            return body if isinstance(body, dict) else {}, None
        except ValueError:
            return None, error("invalid JSON body", 400)

    async def by_phone(req, name, action, echo=lambda body: {}, mirror=None):
        """`action(s, jid, body)` -> accepted?, for each phone in the body. Answers
        {data: {phone: True}} like WPPConnect's clear-chat, plus whatever
        `echo(body)` returns. A chat stored under both addresses (number and
        LID) gets the action on the first and `mirror(s, other_key, body)` on the
        others, after WhatsApp accepted."""
        s = session(req)
        body, failure = await body_of(req)
        if failure is not None:
            return failure
        try:
            phone_list = phones(body)
        except ValueError as e:
            return error(e, 400)
        if not connected(s):
            return responses.disconnected()
        group = boolean(body.get("isGroup"), False)
        result = {}
        for phone in phone_list:
            try:
                jid = chat_jid(phone, group)
                keys = await in_thread(s.chat_keys, jid)
                target = keys[0] if keys else jid
                result[phone] = bool(await in_thread(action, s, target, body))
                if result[phone] and mirror is not None:
                    for other in keys[1:]:
                        mirror(s, other, body)
            except Exception:
                log.exception(name)
                result[phone] = False
        if not all(result.values()):
            return error("not accepted", 500)
        return ok(status="success", response={"data": result, **echo(body)})

    async def clear_chat(req):
        # the client checks the `keepStarred` the server applied
        return await by_phone(
            req, "clear-chat",
            lambda s, jid, body: clear_on_phone(s, jid, keep_starred(body)),
            echo=lambda body: {"keepStarred": keep_starred(body)},
            mirror=lambda s, other, body: s.db.clear_chat(
                other, keep_starred=keep_starred(body)))

    async def delete_chat(req):
        return await by_phone(
            req, "delete-chat", lambda s, jid, body: delete_on_phone(s, jid),
            mirror=lambda s, other, body: s.db.delete_chat(other))

    async def on_all(req, name, action, only_if=lambda c, body: True):
        """Applies `action(s, jid, body)` to each chat in the store; answers how
        many existed and how many WhatsApp accepted."""
        s = session(req)
        body, failure = await body_of(req)
        if failure is not None:
            return failure
        if not connected(s):
            return responses.disconnected()
        targets = [c["jid"] for c in s.db.chats()
                   if c["jid"] != "status@broadcast" and only_if(c, body)]
        done = 0
        for jid in targets:
            try:
                done += bool(await in_thread(action, s, jid, body))
            except Exception:
                log.exception(name)
        return ok(status="success", response={"total": len(targets), "done": done})

    async def clear_all_chats(req):
        return await on_all(
            req, "clear-all-chats",
            lambda s, jid, body: clear_on_phone(s, jid, keep_starred(body)))

    async def delete_all_chats(req):
        return await on_all(
            req, "delete-all-chats", lambda s, jid, body: delete_on_phone(s, jid))

    async def archive_all_chats(req):
        return await on_all(
            req, "archive-all-chats",
            lambda s, jid, body: s.archive(jid, boolean(body.get("value"), True)),
            only_if=lambda c, body: bool(c["archived"]) != boolean(body.get("value"), True))

    async def mark_unseen(req):
        s = session(req)
        body, failure = await body_of(req)
        if failure is not None:
            return failure
        try:
            phone_list = phones(body)
        except ValueError as e:
            return error(e, 400)
        if not connected(s):
            return responses.disconnected()
        group = boolean(body.get("isGroup"), False)
        for phone in phone_list:
            jid = chat_jid(phone, group)
            await in_thread(s.mark_unread, await in_thread(s.existing_chat, jid) or jid)
        return ok(status="success", response={"unseen": True})

    async def send_presence_state(s, body, phone_list, state, name):
        """Sends the presence (typing, recording, stopped) of `state` to each phone."""
        from neonize.utils import build_jid
        from neonize.utils.enum import ChatPresence, ChatPresenceMedia
        presence, media = typing_state(state)
        group = boolean(body.get("isGroup"), False)
        for phone in phone_list:
            u, _, srv = chat_jid(phone, group).partition("@")
            try:
                await in_thread(s.client.send_chat_presence, build_jid(u, srv),
                                ChatPresence[presence], ChatPresenceMedia[media])
            except Exception as e:
                log.exception(name)
                return error(e)
        return None

    async def chat_state(req):
        s = session(req)
        body, failure = await body_of(req)
        if failure is not None:
            return failure
        try:
            phone_list = phones(body)
            typing_state(body.get("chatstate"))
        except ValueError as e:
            return error(e, 400)
        if not connected(s):
            return responses.disconnected()
        failed = await send_presence_state(s, body, phone_list, body.get("chatstate"), "chat-state")
        if failed is not None:
            return failed
        return ok(status="success", response={"chatstate": int(body["chatstate"])})

    async def typing_common(req, name, is_recording):
        """typing and recording: the client sends {phone, value, isGroup}; value
        true says 'typing' (or 'recording audio'), false 'stopped'."""
        s = session(req)
        body, failure = await body_of(req)
        if failure is not None:
            return failure
        try:
            phone_list = phones(body)
        except ValueError as e:
            return error(e, 400)
        if not connected(s):
            return responses.disconnected()
        failed = await send_presence_state(
            s, body, phone_list,
            typing_chatstate(boolean(body.get("value"), True), is_recording), name)
        if failed is not None:
            return failed
        return ok(status="success", response={})

    async def typing(req):
        return await typing_common(req, "typing", False)

    async def recording(req):
        return await typing_common(req, "recording", True)

    async def download_media(req):
        """Downloads and decrypts the media of a stored message; base64 in the
        response body, like get-media-by-message."""
        s = session(req)
        body, failure = await body_of(req)
        if failure is not None:
            return failure
        serial = str(body.get("messageId") or "")
        if not serial:
            return error("messageId is required", 400)
        _, _, mid, _ = s.id_parts(serial)
        record = s.db.media_record(mid)
        if record is None:
            return error("media not found", 404)
        if not connected(s):
            return responses.disconnected()
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
        try:
            data = await in_thread(s.client.download_any, Message.FromString(record))
        except Exception as e:
            log.exception("download-media")
            text = str(e).lower()
            # media that left WhatsApp's server: the app treats 410 as "expired"
            return error(e, 410 if ("404" in text or "410" in text or "not found" in text) else 500)
        stored_message = s.db.wpp_message(mid) or {}
        encoded = base64.b64encode(data).decode()
        return ok(status="success", response={
            "base64": encoded, "mimetype": stored_message.get("mimetype", "")})

    async def message_ack(req):
        """Who received, read or played a message of ours. Answers 404 with no
        receipts stored: the client then uses what it recorded itself."""
        s = session(req)
        body, failure = await body_of(req)
        if failure is not None:
            return failure
        from_me, chat, mid, _ = s.id_parts(str(body.get("messageId") or ""))
        rows, expected = (s.db.receipts_of(mid) if from_me else []), 0
        if from_me and chat.endswith("@g.us"):
            # group: only the receipts of whoever is a recipient, one row per person
            summary = await in_thread(s.receipt_summary, chat, mid)
            if summary is not None:
                rows, expected = summary
        participants = receipt_participants(rows)
        if not participants:
            return error("no receipts recorded for this message", 404)
        return ok(status="success", response={
            "participants": participants,
            "deliveryRemaining": max(0, expected - len(participants))})

    p = "/api/{tok}/"
    r.add_post(p + "clear-chat", clear_chat)
    r.add_post(p + "delete-chat", delete_chat)
    r.add_post(p + "clear-all-chats", clear_all_chats)
    r.add_post(p + "delete-all-chats", delete_all_chats)
    r.add_post(p + "archive-all-chats", archive_all_chats)
    r.add_post(p + "mark-unseen", mark_unseen)
    r.add_post(p + "chat-state", chat_state)
    r.add_post(p + "typing", typing)
    r.add_post(p + "recording", recording)
    r.add_post(p + "download-media", download_media)
    r.add_post(p + "message-ack", message_ack)
