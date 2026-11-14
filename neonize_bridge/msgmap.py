"""whatsmeow message (proto) -> object in the WhatsApp Web format that
WPPConnect returns (type "chat", body, id "false_<chat>_<ID>", from, to, t).
No dependency on the server: a pure function, tested in test_msgmap.py."""
import base64
import hashlib
import json
import logging

import buttons
import commerce_messages

# layers that only wrap another message
WRAPPERS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
            "viewOnceMessageV2Extension", "documentWithCaptionMessage",
            "deviceSentMessage", "editedMessage", "botInvokeMessage",
            # status layers: "Add yours" and question statuses, spoiler and
            # mentioned-group wrappers, animated stickers. Unwrapped, their
            # text/media shows; before, each was an "Incompatible message".
            "statusAddYours", "questionMessage", "spoilerMessage",
            "groupMentionedMessage", "lottieStickerMessage")

log = logging.getLogger("bridge")

MEDIA_KINDS = {  # field -> (WhatsApp Web type, default mimetype)
    "imageMessage": ("image", "image/jpeg"),
    "videoMessage": ("video", "video/mp4"),
    "audioMessage": ("audio", "audio/ogg; codecs=opus"),
    "documentMessage": ("document", ""),
    "stickerMessage": ("sticker", "image/webp"),
}


def seconds(ts):
    """Timestamp (seconds or milliseconds) -> whole seconds."""
    t = float(ts or 0)
    return int(t / 1000 if t > 1e11 else t)


def jid_wpp(jid: str) -> str:
    """'x:12@s.whatsapp.net' -> 'x@c.us' (WhatsApp Web uses @c.us)."""
    if not jid:
        return ""
    user, _, server = jid.partition("@")
    user = user.split(":")[0]
    if server == "s.whatsapp.net":
        server = "c.us"
    return f"{user}@{server}"


def jid_whatsmeow(jid: str) -> str:
    """'x@c.us' -> 'x@s.whatsapp.net'."""
    user, _, server = (jid or "").partition("@")
    return f"{user}@{'s.whatsapp.net' if server in ('c.us', '') else server}"


def unwrap(msg):
    """Message without the wrapping layers (ephemeral, view once, edit...)."""
    for _ in range(6):
        for name in WRAPPERS:
            try:
                if not msg.HasField(name):
                    continue
            except ValueError:
                continue
            inner = getattr(msg, name).message
            if not inner.ListFields():
                return msg
            msg = inner
            break
        else:
            return msg
    return msg


def media_record_of(message):
    """The minimum WhatsApp requires to download the media later (keys and
    path), without thumbnail, caption or quoted reply; proto bytes or None if
    the message has no media."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    inner = unwrap(message)
    try:
        if inner.HasField("ptvMessage"):      # round video: downloads as videoMessage
            video = type(inner.videoMessage)()
            video.CopyFrom(inner.ptvMessage)
            inner = Message(videoMessage=video)
    except ValueError:
        pass
    for name in MEDIA_KINDS:
        try:
            if not inner.HasField(name):
                continue
        except ValueError:
            continue
        sub = type(getattr(inner, name))()
        sub.CopyFrom(getattr(inner, name))
        for field in ("contextInfo", "JPEGThumbnail", "caption"):
            try:
                sub.ClearField(field)
            except ValueError:
                pass
        return Message(**{name: sub}).SerializeToString()
    return None


def quoted(w):
    """Minimal message for the quote bubble from the WhatsApp Web format."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    m = Message()
    kind, body = w.get("type"), w.get("body") or ""
    if kind == "image":
        m.imageMessage.caption = w.get("caption") or ""
        m.imageMessage.mimetype = w.get("mimetype") or "image/jpeg"
    elif kind == "video":
        m.videoMessage.caption = w.get("caption") or ""
        m.videoMessage.mimetype = w.get("mimetype") or "video/mp4"
    elif kind in ("audio", "ptt"):
        m.audioMessage.PTT = kind == "ptt"
        m.audioMessage.mimetype = w.get("mimetype") or "audio/ogg; codecs=opus"
        try:
            sec = int(w.get("duration") or w.get("seconds") or 0)
        except (TypeError, ValueError):
            sec = 0
        if sec > 0:
            m.audioMessage.seconds = sec
    elif kind == "document":
        m.documentMessage.fileName = w.get("filename") or ""
        m.documentMessage.caption = w.get("caption") or ""
        m.documentMessage.mimetype = w.get("mimetype") or ""
    elif kind == "sticker":
        m.stickerMessage.mimetype = w.get("mimetype") or "image/webp"
    else:
        m.conversation = body
    return m


def quoted_participant(w, me):
    """Who wrote the quoted message, in whatsmeow format."""
    if w.get("fromMe"):
        return me
    return jid_whatsmeow(w.get("author") or w.get("from") or "")


def _b64(datum) -> str:
    return base64.b64encode(datum).decode() if datum else ""


def _field(msg):
    """Name of the filled content field (implicit oneof) or ''."""
    for f, _ in msg.ListFields():
        if f.name not in ("messageContextInfo", "senderKeyDistributionMessage"):
            return f.name
    return ""


def vote_indexes(names, selected):
    """Positions of the options of a vote: WhatsApp sends the SHA-256 of the text
    of each marked option."""
    by_hash = {hashlib.sha256(n.encode()).digest(): i for i, n in enumerate(names)}
    return sorted({by_hash[bytes(h)] for h in selected if bytes(h) in by_hash})


def edit(message):
    """(id of the edited message, new message) of an edit notice, or None."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ProtocolMessage
    inner = unwrap(message)
    if _field(inner) != "protocolMessage":
        return None
    p = inner.protocolMessage
    if p.type != ProtocolMessage.MESSAGE_EDIT or not p.key.ID:
        return None
    return p.key.ID, p.editedMessage


def pin(message):
    """(id of the message, pin?, duration in seconds) of a notice of pinning or
    unpinning a message, or None."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import PinInChatMessage
    inner = unwrap(message)
    if _field(inner) != "pinInChatMessage":
        return None
    p = inner.pinInChatMessage
    if not p.key.ID:
        return None
    duration = int(inner.messageContextInfo.messageAddOnDurationInSecs or 0)
    return p.key.ID, p.type == PinInChatMessage.PIN_FOR_ALL, duration


def media_field(message):
    """Name of the filled media field (imageMessage...) or ''."""
    for name in MEDIA_KINDS:
        try:
            if message.HasField(name):
                return name
        except ValueError:
            pass
    return ""


def vote(message):
    """(id of the poll, ) of a vote message, or None."""
    inner = unwrap(message)
    if _field(inner) != "pollUpdateMessage":
        return None
    return inner.pollUpdateMessage.pollCreationMessageKey.ID or None


def reaction(message):
    """(id of the target message, emoji, key.fromMe, participant) of a reaction
    message, or None if the message is not a reaction. Empty emoji = reaction
    removed. `fromMe` is from the point of view of whoever reacted."""
    inner = unwrap(message)
    if _field(inner) != "reactionMessage":
        return None
    r = inner.reactionMessage
    if not r.key.ID:
        return None
    return r.key.ID, r.text, bool(r.key.fromMe), jid_wpp(r.key.participant)


def _content(inner, field, chat_w, out) -> bool:
    """Fills `out` with the type and content of the message; False if it is only
    control/reaction (nothing to show)."""
    if field == "conversation":
        out["body"] = inner.conversation
    elif field == "extendedTextMessage":
        e = inner.extendedTextMessage
        out["body"] = e.text
        if e.title or e.description:
            out.update(title=e.title, description=e.description,
                       canonicalUrl=e.matchedText)
    elif field in MEDIA_KINDS or field == "ptvMessage":
        # round video: same fields as the video, with the client's own type
        kind, mime = MEDIA_KINDS["videoMessage" if field == "ptvMessage" else field]
        m = getattr(inner, field)
        if field == "audioMessage":
            kind = "ptt" if m.PTT else "audio"
            out["isPtt"] = bool(m.PTT)
        out.update(type="ptv" if field == "ptvMessage" else kind,
                   mimetype=m.mimetype or mime,
                   mediaKey=_b64(m.mediaKey),
                   clientUrl=m.URL or m.directPath,
                   size=int(m.fileLength or 0))
        for name in ("caption", "seconds", "fileName", "title"):
            if hasattr(m, name) and getattr(m, name):
                out[{"seconds": "duration"}.get(name, name)] = getattr(m, name)
        if field in ("videoMessage", "ptvMessage"):
            out["isGif"] = bool(m.gifPlayback)
        if field == "documentMessage":
            out["filename"] = m.fileName or m.title
    elif field == "locationMessage":
        loc = inner.locationMessage
        out.update(type="location", lat=loc.degreesLatitude,
                   lng=loc.degreesLongitude,
                   loc=loc.name or loc.address, body=loc.name)
    elif field == "liveLocationMessage":
        loc = inner.liveLocationMessage
        out.update(type="liveLocation", lat=loc.degreesLatitude,
                   lng=loc.degreesLongitude, loc=loc.caption, body=loc.caption)
    elif field == "contactMessage":
        out.update(type="vcard", body=inner.contactMessage.vcard,
                   displayName=inner.contactMessage.displayName)
    elif field == "contactsArrayMessage":
        # the client only has the single card ("vcard"): it becomes one card whose
        # name is the set's ("Ana and 2 others"); the cards stay together in the body
        cards = inner.contactsArrayMessage
        out.update(type="vcard",
                   body="\n".join(c.vcard for c in cards.contacts if c.vcard),
                   displayName=cards.displayName or next(
                       (c.displayName for c in cards.contacts if c.displayName), ""))
    elif field == "protocolMessage":
        p = inner.protocolMessage
        if p.type != 0:        # only the revoke (delete for everyone) matters
            return False
        target = p.key.ID
        ser = f"{'true' if p.key.fromMe else 'false'}_{chat_w}_{target}"
        out.update(type="revoked", body="", protocolMessageKey={
            "id": target, "remote": chat_w, "fromMe": bool(p.key.fromMe),
            "_serialized": ser})
    elif field.startswith("pollCreationMessage"):
        poll = getattr(inner, field)
        out.update(type="poll_creation", body=poll.name,
                   pollName=poll.name,
                   pollSelectableOptionsCount=int(poll.selectableOptionsCount or 0),
                   pollOptions=[{"name": o.optionName, "localId": i}
                                for i, o in enumerate(poll.options)])
    elif field == "eventMessage":
        import events
        out.update(events.event_fields(inner.eventMessage))
    elif field in CHANNEL_INVITES:
        out.update(channel_invite_fields(field, getattr(inner, field)))
    elif field == "orderMessage":
        out.update(commerce_messages.order_fields(inner.orderMessage))
    elif field == "productMessage":
        out.update(commerce_messages.product_fields(inner.productMessage))
    elif field == "richResponseMessage":
        out.update(rich_response_fields(inner.richResponseMessage))
    elif field in ("reactionMessage", "pollUpdateMessage", "keepInChatMessage", "",
                   "encEventResponseMessage", "secretEncryptedMessage"):
        return False
    elif field in ("buttonsMessage", "listMessage", "templateMessage", "highlyStructuredMessage",
                   "interactiveMessage", "buttonsResponseMessage", "listResponseMessage",
                   "templateButtonReplyMessage", "interactiveResponseMessage"):
        out.update(buttons.button_fields(inner))
    else:
        out["type"] = "unknown"
        # the proto field name carries no personal data; it is what a report of
        # an "Incompatible message" needs to be traced to a message type
        log.info("message with no mapping: field=%s", field)
        out.update(buttons.button_fields(inner))
    return True


#: A channel invitation received in a chat -> WhatsApp Web's type and the
#: model field wa-js serializes it under (client/core/channel_invites.py reads both)
CHANNEL_INVITES = {
    "newsletterFollowerInviteMessageV2": ("newsletter_follower_invite",
                                          "newsletterFollowerInviteInfo"),
    "newsletterAdminInviteMessage": ("newsletter_admin_invite", "newsletterAdminInviteInfo"),
}


def channel_invite_fields(field, invite) -> dict:
    """An invitation to follow a channel or to be its admin, in the shape
    WhatsApp Web's model gives it (WAWebParseNewsletter...InviteMessageProto)."""
    kind, info_field = CHANNEL_INVITES[field]
    info = {"newsletterId": invite.newsletterJID, "newsletterName": invite.newsletterName,
            "inviteMessage": invite.caption}
    expiration = int(getattr(invite, "inviteExpiration", 0) or 0)
    if expiration:
        info["inviteExpiration"] = expiration
    return {"type": kind, "body": invite.caption, info_field: info}


def rich_response_fields(rich) -> dict:
    """A reply of Meta AI, as WhatsApp Web serialises it: type "rich_response",
    no body, the text in `richResponse.fragments` and, in the newer layout, in
    `unifiedResponse` (JSON in the proto's bytes). The client reads both
    (core/meta_ai.py rich_response_text); without them the reply was "unknown"."""
    fragments = [{"type": "Text", "text": sub.messageText}
                 for sub in rich.submessages if sub.messageText]
    out = {"type": "rich_response", "body": "", "richResponse": {"fragments": fragments}}
    try:
        unified = json.loads(rich.unifiedResponse.data or b"null")
    except (ValueError, UnicodeDecodeError):
        unified = None
    if isinstance(unified, dict):
        out["unifiedResponse"] = unified
    return out


# content fields that carry contextInfo (reply, mention, forwarded)
CONTEXT = ("extendedTextMessage", "imageMessage", "videoMessage", "ptvMessage",
           "audioMessage", "documentMessage", "stickerMessage", "locationMessage",
           "liveLocationMessage", "contactMessage", "contactsArrayMessage",
           "templateMessage", "interactiveMessage", "buttonsMessage", "listMessage")

# what the client reads of a quote: the rest (keys, thumbnail...) would only bloat
# what it stores (core/utils.py _slim_quoted_message)
QUOTED_FIELDS = ("type", "body", "caption", "filename", "isPtt", "duration")


def _ctx_of(inner):
    """contextInfo of the filled content field, or None."""
    for name in CONTEXT:
        try:
            if inner.HasField(name):
                return getattr(inner, name).contextInfo
        except ValueError:
            pass
    return None


def _quoted_wpp(quoted_message, chat_w):
    """Quoted message (proto) -> dictionary in WPPConnect's `quotedMsg` format,
    or None if there is nothing to show. Media carries only type, caption and
    file name; what has no preview of its own becomes text (the client's preview
    only shows a caption for photo, video, audio, document and sticker)."""
    inner = unwrap(quoted_message)
    q = {"type": "chat", "body": ""}
    if not _content(inner, _field(inner), chat_w, q):
        return None
    kind = q.get("type")
    if kind in ("image", "video", "ptv", "audio", "ptt", "document", "sticker"):
        q["type"] = "video" if kind == "ptv" else kind
        q.pop("body", None)
        q["caption"] = q.get("caption") or ""
    elif kind == "vcard":
        q = {"type": "contact", "caption": q.get("displayName") or ""}
    else:
        # text, poll, location...: the preview is the message's text
        q = {"type": "chat", "body": q.get("body") or ""}
    out = {k: q[k] for k in QUOTED_FIELDS if k in q}
    ctx = _ctx_of(inner)       # the quoted one's own mentions, still to be resolved
    mentioned = [jid_wpp(j) for j in ctx.mentionedJID if j] if ctx is not None else []
    if mentioned:
        out["mentionedJidList"] = mentioned
    return out


def _context(ctx, chat_w):
    """contextInfo (proto) -> reply, mention and forwarding fields of the
    WPPConnect format. `@lid` stays `@lid`: only `@s.whatsapp.net` becomes
    `@c.us` (jid_wpp)."""
    out = {}
    if ctx.stanzaID:
        out["quotedStanzaID"] = ctx.stanzaID
        out["quotedParticipant"] = jid_wpp(ctx.participant)
        if ctx.HasField("quotedMessage"):
            quoted_w = _quoted_wpp(ctx.quotedMessage, chat_w)
            if quoted_w:
                out["quotedMsg"] = quoted_w
    mentioned = [jid_wpp(j) for j in ctx.mentionedJID if j]
    if mentioned:
        out["mentionedJidList"] = mentioned
    if ctx.isForwarded:
        out["isForwarded"] = True
    return out


def build(*, chat: str, from_me: bool, id: str, sender: str, ts,
          message, sender_name: str = ""):
    """Object in WPPConnect format, or None if it is only control/reaction."""
    inner = unwrap(message)
    field = _field(inner)
    chat_w = jid_wpp(chat)
    sender_w = jid_wpp(sender)
    group = chat_w.endswith("@g.us")
    status = chat_w == "status@broadcast"      # the author is whoever posted
    serial = f"{'true' if from_me else 'false'}_{chat_w}_{id}"
    if group:
        serial += f"_{sender_w}"
    out = {
        "id": {"fromMe": from_me, "remote": chat_w, "id": id,
               "_serialized": serial},
        "fromMe": from_me,
        "from": "" if from_me else (sender_w if (group or status) else chat_w),
        "to": chat_w,
        "t": seconds(ts), "timestamp": seconds(ts),
        "isStatus": chat_w == "status@broadcast",
        "notifyName": sender_name,
        "type": "chat", "body": "",
    }
    if (group or status) and not from_me:
        out["author"] = sender_w
    if not _content(inner, field, chat_w, out):
        return None
    ctx = _ctx_of(inner)
    if ctx is not None:
        out.update(_context(ctx, chat_w))
    return out


def link_preview(options):
    """`options.linkPreview` of text sends -> ExtendedTextMessage fields
    (the link card), or None. The client already fetched the page on its machine:
    the bridge only passes the fields along, never fetches the URL (neonize would
    fetch from the user's machine a URL chosen by whoever calls: SSRF)."""
    preview = options.get("linkPreview") if isinstance(options, dict) else None
    if not isinstance(preview, dict):
        return None
    url = str(preview.get("canonicalUrl") or preview.get("matchedText") or "")
    title = str(preview.get("title") or "")
    if not url and not title:
        return None
    # in the proto the page address is matchedText (there is no canonicalURL field)
    return {"title": title, "description": str(preview.get("description") or ""),
            "matchedText": url}


def argb_color(text, default=0xFF1B8755):
    """'#25D366' -> 0xFF25D366 (background color of the text status)."""
    t = (text or "").lstrip("#")
    try:
        return 0xFF000000 | int(t, 16) if len(t) == 6 else default
    except ValueError:
        return default
