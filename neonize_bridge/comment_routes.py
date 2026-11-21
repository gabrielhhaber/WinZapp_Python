"""Comment routes of community announcements: GET and POST of
/message-comments/:messageId. The formats are the ones the WinZapp client reads
(core/community_comments.py; the reference server is
communityCommentsController.ts). The cipher, the secret and the receiving are
in comments.py.

    GET  -> 200 {status:'success', response:[{id, parentMsgId, chatId, author,
            authorName, fromMe, timestamp, type, body}]}
            400 not_community_announcement | 503 comments_unavailable
    POST {text} -> 201 {status:'success', response:{messageSendResult:'OK'}};
            any other response the client treats as "not confirmed"
            (502 comment_unconfirmed when the send may have gone out).

Only groups that WhatsApp says are a community's announcements group
(`IsDefaultSubGroup`) have comments; the others answer 400.

SEND NOT VALIDATED: the `EncCommentMessage` goes out through neonize's
`send_message` and whatsmeow classifies the stanza as `type="text"`
(getTypeFromMessage, with no case for an encrypted comment); WhatsApp Web uses
another type. If the server refuses or discards the stanza, nothing happens
besides the comment not arriving. This can only be proven with a real account.
"""
import asyncio
import logging
import os
import time

from aiohttp import web

import comments as com
import enc_secrets
import msgmap

log = logging.getLogger("bridge")

#: how long the bridge trusts what WhatsApp said about a group being an announcements one
GROUP_TTL = 600


def serial_parts(serial: str):
    """'fromMe_group_ID[_author]' -> (from_me, group, ID, whatsmeow author or '') or
    None if it is not the id of a group message."""
    parts = str(serial or "").split("_")
    if len(parts) < 3 or parts[0] not in ("true", "false") or not parts[2] \
            or not parts[1].endswith("@g.us"):
        return None
    author = msgmap.jid_whatsmeow(parts[3]) if len(parts) > 3 and parts[3] else ""
    return parts[0] == "true", parts[1], parts[2], author


def from_other_chat(serial: str) -> bool:
    """The id is of a message (fromMe_chat_ID...), but of a chat that is not a
    group."""
    parts = str(serial or "").split("_")
    return (len(parts) >= 3 and parts[0] in ("true", "false") and bool(parts[2])
            and "@" in parts[1] and not parts[1].endswith("@g.us"))


def is_community_announcement(info) -> bool:
    """neonize `GroupInfo` -> the group is a community's announcements group."""
    return bool(info.GroupIsDefaultSub.IsDefaultSubGroup)


def register_routes(r, *, session, ok, jid_from_text):
    """Wire the comment routes to the aiohttp router `r`."""

    async def execute(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def error(code, status, **extra):
        return web.json_response({"status": "error", "code": code, **extra}, status=status)

    def unavailable():
        return error("comments_unavailable", 503)

    async def known_announcement(s, group):
        """True/False if WhatsApp said the group is (or is not) a community's
        announcements one; None if it could not be known. Keeps the answer a while."""
        cache = s.__dict__.setdefault("_community_announcements", {})
        stored = cache.get(group)
        if stored and stored[1] > time.monotonic():
            return stored[0]
        try:
            info = await execute(s.client.get_group_info, jid_from_text(group))
            value = is_community_announcement(info)
        except Exception as e:
            log.warning("message-comments: group data unavailable (%s)", type(e).__name__)
            return None
        cache[group] = (value, time.monotonic() + GROUP_TTL)
        return value

    async def check(s, group, parent_id):
        """None if the group can have comments; otherwise the error response."""
        if s.db.comments_of(group, parent_id):      # a comment already arrived: it is an announcement
            return None
        known = await known_announcement(s, group)
        if known is None:
            return unavailable()
        if not known:
            return error("not_community_announcement", 400)
        return None

    def ready(s):
        return s.client is not None and s.status == "CONNECTED"

    def name_of(s, author_wpp):
        found = s.db.contact_of(msgmap.jid_whatsmeow(author_wpp)) if author_wpp else {}
        return found.get("name") or found.get("pushname") or ""

    async def get_comments(req):
        s = session(req)
        serial = req.match_info["messageId"]
        parts = serial_parts(serial)
        if parts is None:
            # the id of a message of a chat that is not a group never has comments
            return error("not_community_announcement" if from_other_chat(serial)
                         else "invalid_comment_request", 400)
        _, group, parent_id, _ = parts
        if not ready(s):
            return unavailable()
        refusal = await check(s, group, parent_id)
        if refusal is not None:
            return refusal
        rows = s.db.comments_of(group, parent_id)
        return ok(status="success", response=[
            com.comment_to_wpp(x, serial, name_of(s, x["author"])) for x in rows])

    def prepare_send(s, group, parent_id, parent_from_me, parent_author, text):
        """Encrypts the comment (blocks: reads whatsmeow's database). Returns the
        message to send or the code of why it cannot."""
        own = [x for x in (s.lid, s.me) if x]
        # neonize's own EncryptComment first: it finds the secret itself
        if own:
            from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
            sender = own[0] if parent_from_me else parent_author
            sealed = enc_secrets.seal_comment(
                s.client, enc_secrets.root_info(group, sender, parent_from_me, parent_id),
                Message.FromString(com.serialize_text(text)))
            if sealed is not None:
                return sealed, ""
        path = os.path.join(s.folder, com.SESSION_FILE)
        found = None
        for who in (own if parent_from_me else [parent_author]):
            found = com.message_secret(path, group, who, parent_id)
            if found:
                break
        if not found or not own:
            return None, "comment_secret_unavailable"
        secret, stored_sender = found
        parent_sender = stored_sender if parent_from_me and stored_sender else parent_author
        if parent_from_me and not parent_sender:
            parent_sender = own[0]
        # EncryptComment: encrypts as the account itself (LID) and with the
        # message's sender as the database stored it
        payload, iv = com.encrypt_comment(
            secret, parent_id, stored_sender or parent_sender, own[0],
            com.serialize_text(text))
        return com.comment_message(group, parent_from_me, parent_sender, parent_id,
                                   payload, iv), ""

    async def send_comment(req):
        s = session(req)
        serial = req.match_info["messageId"]
        try:
            body = await req.json()
        except Exception:
            body = None
        text = body.get("text") if isinstance(body, dict) else None
        parts = serial_parts(serial)
        if parts is None or not isinstance(text, str) or not text.strip():
            return error("invalid_comment_request", 400)
        parent_from_me, group, parent_id, parent_author = parts
        if not parent_from_me and not parent_author:
            return error("invalid_comment_request", 400)
        if not ready(s):
            return unavailable()
        refusal = await check(s, group, parent_id)
        if refusal is not None:
            return refusal
        try:
            message, reason = await execute(
                prepare_send, s, group, parent_id, parent_from_me, parent_author, text)
        except Exception as e:
            log.warning("message-comments: did not encrypt (%s)", type(e).__name__)
            return error("comment_unconfirmed", 502, retrySafe=False)
        if message is None:         # nothing was sent
            return error(reason, 503, retrySafe=True)
        try:
            response = await execute(s.client.send_message, jid_from_text(group), message)
        except Exception as e:
            log.warning("message-comments: send without confirmation (%s)", type(e).__name__)
            return error("comment_unconfirmed", 502, retrySafe=False)
        com.record_sent_comment(
            s, group, parent_id, serial, getattr(response, "ID", "") or "", text)
        return web.json_response(
            {"status": "success", "response": {"messageSendResult": "OK"}}, status=201)

    r.add_get("/api/{tok}/message-comments/{messageId}", get_comments)
    r.add_post("/api/{tok}/message-comments/{messageId}", send_comment)
