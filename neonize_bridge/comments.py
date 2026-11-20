"""Comments on community announcements: the cipher, the message secret and the
receiving. The routes are in comment_routes.py.

In a community announcements group, whoever comments on a message sends an
`EncCommentMessage`: the comment (a serialized `waE2E.Message`) encrypted with
AES-GCM by a key derived from the `messageSecret` of the commented message.
The derivation is the same as whatsmeow's (msgsecret.go, `generateMsgSecretKey`):

    key = HKDF-SHA256(ikm=secret, no salt,
                      info = original ID + original sender (without
                             device) + comment sender (without
                             device) + "Enc Comment", 32 bytes)

AES-GCM with a 12-byte IV, with NO additional data (only poll votes and event
replies carry `ID\\0sender`) and the tag at the end of the ciphertext.

whatsmeow already keeps the `messageSecret` of every group message that goes
through it, in `whatsmeow_message_secrets` of session.db: here that database is
only read, in read-only mode (`message_secret`). Without the secret the comment
becomes `ciphertext`, which the client shows as "could not read".

WARNING: nothing here was proven against real WhatsApp. The cipher has fixed
vectors (test_comments.py), computed with Go's `crypto/hkdf` and
`crypto/cipher` on the same logic as msgsecret.go; the format of the sent
comment and the server's acceptance of it have no proof at all.

Nothing here logs a JID, number, name or message text.
"""
import logging
import os
import pathlib
import sqlite3
import time

import enc_secrets
import msgmap
import group_receipts

log = logging.getLogger("bridge")

COMMENT_USAGE = "Enc Comment"
IV_SIZE = 12
SESSION_FILE = "session.db"

KIND_COMMENT = "comment"
KIND_DELETED = "revoked"
KIND_UNREADABLE = "ciphertext"

PERSON_SERVERS = ("s.whatsapp.net", "lid")


class UnreadableComment(Exception):
    """The comment did not open with any of the sender combinations."""


# ------------------------------------------------------------------ cipher
def without_device(jid: str) -> str:
    """'u:12@s.whatsapp.net' / 'u.0:12@lid' -> 'u@s.whatsapp.net' (`ToNonAD`:
    without agent or device). A WPPConnect form (@c.us) becomes whatsmeow's."""
    user, _, server = str(jid or "").partition("@")
    user = user.split(":")[0].split(".")[0]
    if server in ("c.us", ""):
        server = "s.whatsapp.net"
    return f"{user}@{server}" if user else ""


def comment_key(secret: bytes, original_id: str, original_sender: str,
                comment_sender: str) -> bytes:
    """The AES-256 key of a comment (generateMsgSecretKey, "Enc Comment")."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    info = (original_id + without_device(original_sender)
            + without_device(comment_sender) + COMMENT_USAGE).encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(secret)


def encrypt_comment(secret, original_id, original_sender, comment_sender,
                    plaintext: bytes, iv: bytes = None):
    """(ciphertext with the tag, iv) of an already serialized comment."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = iv if iv is not None else os.urandom(IV_SIZE)
    key = comment_key(secret, original_id, original_sender, comment_sender)
    return AESGCM(key).encrypt(iv, bytes(plaintext), None), iv


def decrypt_comment(secret, original_id, original_senders, comment_senders,
                    iv: bytes, payload: bytes) -> bytes:
    """The opened comment. WhatsApp is swapping numbers for LIDs, so whoever
    commented and whoever wrote the original message can appear in one form or
    the other: tries each combination (whatsmeow's `decryptMsgSecret` tries the
    key's sender and the stored one). Raises UnreadableComment."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(iv or b"") != IV_SIZE or not payload or not secret:
        raise UnreadableComment("format")
    seen = set()
    for original in original_senders:
        for commenter in comment_senders:
            pair = (without_device(original), without_device(commenter))
            if not all(pair) or pair in seen:
                continue
            seen.add(pair)
            key = comment_key(secret, original_id, *pair)
            try:
                return AESGCM(key).decrypt(bytes(iv), bytes(payload), None)
            except InvalidTag:
                continue
    raise UnreadableComment("tag")


def original_sender_from_key(key_from_me: bool, participant: str, comment_sender: str):
    """Who wrote the commented message, as whatsmeow deduces it from the key
    (getOrigSenderFromKey, group case): `fromMe` in the key means the message's
    author is whoever commented; otherwise it is the key's `participant`, which
    has to be a person. '' if it cannot be known."""
    if key_from_me:
        return without_device(comment_sender)
    participant = without_device(participant)
    if participant.partition("@")[2] in PERSON_SERVERS:
        return participant
    return ""


# ------------------------------------------------------------------ secret
_QUERY_WITH_MAP = """
SELECT key, sender_jid FROM whatsmeow_message_secrets
WHERE message_id=?3 AND (chat_jid=?1 OR chat_jid=(
    CASE
        WHEN ?1 LIKE '%@lid'
            THEN (SELECT pn || '@s.whatsapp.net' FROM whatsmeow_lid_map WHERE lid=replace(?1, '@lid', ''))
        WHEN ?1 LIKE '%@s.whatsapp.net'
            THEN (SELECT lid || '@lid' FROM whatsmeow_lid_map WHERE pn=replace(?1, '@s.whatsapp.net', ''))
    END
)) AND (sender_jid=?2 OR sender_jid=(
    CASE
        WHEN ?2 LIKE '%@lid'
            THEN (SELECT pn || '@s.whatsapp.net' FROM whatsmeow_lid_map WHERE lid=replace(?2, '@lid', ''))
        WHEN ?2 LIKE '%@s.whatsapp.net'
            THEN (SELECT lid || '@lid' FROM whatsmeow_lid_map WHERE pn=replace(?2, '@s.whatsapp.net', ''))
    END
)) LIMIT 1
"""
# the same query without the number<->LID map (a session.db without the table)
_QUERY_SIMPLE = """
SELECT key, sender_jid FROM whatsmeow_message_secrets
WHERE message_id=?3 AND chat_jid=?1 AND sender_jid=?2 LIMIT 1
"""


def message_secret(path: str, chat: str, sender: str, msg_id: str):
    """(messageSecret, stored sender) of a message, read from whatsmeow's
    session.db (`GetMessageSecret`: matches the chat and the sender also by the
    LID/number form, and returns the sender as it was stored), or None.
    Read-only: the bridge never writes to that database."""
    if not (path and chat and sender and msg_id and os.path.exists(path)):
        return None
    uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro"
    params = (without_device(chat), without_device(sender), msg_id)
    try:
        con = sqlite3.connect(uri, uri=True, timeout=3)
    except sqlite3.Error:
        log.warning("comments: the whatsmeow database did not open")
        return None
    try:
        try:
            row = con.execute(_QUERY_WITH_MAP, params).fetchone()
        except sqlite3.OperationalError:
            row = con.execute(_QUERY_SIMPLE, params).fetchone()
    except sqlite3.Error:
        log.warning("comments: the message secret could not be read")
        return None
    finally:
        con.close()
    if not row or not row[0]:
        return None
    return bytes(row[0]), str(row[1] or "")


# --------------------------------------------------- content and formats
def comment_text(message) -> str:
    """The text of a comment (a waE2E.Message). A comment with no text
    (media without a caption) comes back empty."""
    inner = msgmap.unwrap(message)
    if inner.conversation:
        return inner.conversation
    if inner.HasField("extendedTextMessage"):
        return inner.extendedTextMessage.text
    for field in msgmap.MEDIA_KINDS:
        if inner.HasField(field) and hasattr(getattr(inner, field), "caption"):
            return getattr(inner, field).caption
    return ""


def message_serial(chat: str, msg_id: str, from_me: bool, author_wpp: str) -> str:
    """'fromMe_chat_ID[_author]': WPPConnect's serialized id of a group message."""
    serial = f"{'true' if from_me else 'false'}_{chat}_{msg_id}"
    if not from_me and author_wpp:
        serial += f"_{author_wpp}"
    return serial


def comment_to_wpp(row: dict, parent_serial: str, name: str = "") -> dict:
    """A stored row -> an item of /message-comments (core/community_comments.py)."""
    kind = row["type"]
    return {"id": row["id"], "parentMsgId": parent_serial, "chatId": row["chat"],
            "author": row["author"], "authorName": name,
            "fromMe": bool(row["from_me"]), "timestamp": int(row["ts"]),
            "type": kind, "body": row["body"] if kind == KIND_COMMENT else ""}


def count_event(session: str, parent_serial: str, chat: str, quantity: int) -> dict:
    return {"session": session, "id": parent_serial, "chatId": chat, "replyCount": int(quantity)}


def preview_event(session: str, chat: str, row) -> dict:
    """`kind: preview`: the comment the chat list shows, or none."""
    preview = None
    if row is not None:
        preview = {"type": "comment",
                   "msgKey": message_serial(chat, row["id"], bool(row["from_me"]),
                                            row["author"]),
                   "sender": row["author"], "timestamp": int(row["ts"])}
    return {"session": session, "kind": "preview", "chatId": chat, "preview": preview}


def comment_message(chat: str, parent_from_me: bool, parent_sender: str, parent_id: str,
                    payload: bytes, iv: bytes):
    """The `waE2E.Message` with the EncCommentMessage to send (EncryptComment)."""
    from neonize.proto.waCommon.WACommon_pb2 import MessageKey
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import EncCommentMessage, Message
    return Message(encCommentMessage=EncCommentMessage(
        targetMessageKey=MessageKey(remoteJID=chat, participant=without_device(parent_sender),
                                    fromMe=parent_from_me, ID=parent_id),
        encPayload=payload, encIV=iv))


def serialize_text(text: str) -> bytes:
    """The serialized text comment (what goes inside the cipher)."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    return Message(conversation=text).SerializeToString()


# --------------------------------------------------------------- receiving
def jid_text(jid) -> str:
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


def resolve_parent_serial(s, chat: str, parent_id: str, from_me: bool, author_wpp: str) -> str:
    """The serialized id of the commented message: the stored message's, which
    is what the client has, or the one deduced from the key."""
    stored_message = s.db.wpp_message(parent_id)
    if stored_message is not None:
        return stored_message["id"]["_serialized"]
    return message_serial(chat, parent_id, from_me, author_wpp)


def _own_users(s) -> set:
    return {x.partition("@")[0].split(":")[0] for x in (s.me, s.lid) if x} - {""}


def announce(s, chat: str, parent_id: str, parent_serial: str):
    """Tells the app the new reply count and the chat preview."""
    s.emit("announcement-replies", count_event(
        s.name, parent_serial, chat, s.db.count_comments(chat, parent_id)))
    s.emit("announcement-replies", preview_event(
        s.name, chat, s.db.last_comment(chat)))


def record_sent_comment(s, chat, parent_id, parent_serial, comment_id, text):
    """Stores the comment this account just sent (the server does not return it
    to this device) and tells the app."""
    if not comment_id:
        return
    mine = msgmap.jid_wpp(s.lid or s.me)
    s.db.store_comment(comment_id, parent_id, chat, mine, int(time.time()),
                       KIND_COMMENT, text, True, parent_serial)
    announce(s, chat, parent_id, parent_serial)


def _open(s, chat, enc, sender, from_me, event=None):
    """(kind, text) of the comment: neonize opens it (whatsmeow knows the secret
    under every form of the JIDs); otherwise it is opened here with the commented
    message's secret, or 'ciphertext' if it cannot."""
    if event is not None:
        native = enc_secrets.open_comment(getattr(s, "client", None), event)
        if native is not None:
            return KIND_COMMENT, comment_text(native)
    key = enc.targetMessageKey
    original = original_sender_from_key(bool(key.fromMe), key.participant, sender)
    if not original:
        return KIND_UNREADABLE, ""
    found = message_secret(os.path.join(s.folder, SESSION_FILE),
                           chat, original, key.ID)
    if not found:
        return KIND_UNREADABLE, ""
    secret, stored = found
    originals = [original] + ([stored] if stored else [])
    commenters = [sender]
    if from_me:     # the account shows up as number or as LID, depending on the device
        commenters += [x for x in (s.lid, s.me) if x]
    try:
        opened = decrypt_comment(secret, key.ID, originals, commenters,
                                 bytes(enc.encIV), bytes(enc.encPayload))
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
        return KIND_COMMENT, comment_text(Message.FromString(opened))
    except Exception as error:
        log.info("comment I could not open (%s)", type(error).__name__)
        return KIND_UNREADABLE, ""


def receive(s, chat: str, o, e) -> bool:
    """True if the message was an announcement comment (stored and announced) or
    the notice that a comment was deleted. Called by the message handler, like
    the one for poll votes."""
    inner = msgmap.unwrap(e.Message)
    if inner.HasField("encCommentMessage"):
        return _comment_received(s, chat, o, e, inner.encCommentMessage)
    if inner.HasField("protocolMessage"):
        p = inner.protocolMessage
        if p.type == 0 and p.key.ID:    # REVOKE
            return _comment_deleted(s, chat, p.key.ID, o)
    return False


def _comment_received(s, chat, o, e, enc) -> bool:
    key = enc.targetMessageKey
    if not chat.endswith("@g.us") or not key.ID:
        return True      # a comment only exists in a group; it is not a message to show
    sender = jid_text(o.Sender)
    from_me = bool(o.IsFromMe)
    kind, text = _open(s, chat, enc, sender, from_me, e)
    author = msgmap.jid_wpp(sender) or (msgmap.jid_wpp(s.me) if from_me else "")
    parent_author = msgmap.jid_wpp(key.participant)
    parent_is_mine = (parent_author.partition("@")[0] in _own_users(s)) \
        or (bool(key.fromMe) and from_me)
    parent_serial = resolve_parent_serial(s, chat, key.ID, parent_is_mine, parent_author)
    s.db.store_comment(e.Info.ID, key.ID, chat, author, msgmap.seconds(e.Info.Timestamp),
                       kind, text, from_me, parent_serial)
    if e.Info.Pushname and not from_me and sender:
        s.db.contact(sender, pushname=e.Info.Pushname)
    announce(s, chat, key.ID, parent_serial)
    return True


def _comment_deleted(s, chat, comment_id, o) -> bool:
    stored = s.db.comment_author(chat, comment_id)
    if stored is None:
        return False       # deleted something else: follows the messages path
    author, from_own_device = stored
    if not ((o.IsFromMe and from_own_device)
            or group_receipts.keys_of(jid_text(o.Sender)) & group_receipts.keys_of(author)):
        # only the author deletes their own comment (removal by a group
        # administrator is not handled: the comment stays visible); the message is consumed
        log.info("notice of deleting someone else's comment ignored")
        return True
    found = s.db.revoke_comment(chat, comment_id)
    if found is None:
        return False       # deleted something else: follows the messages path
    announce(s, chat, *found)
    return True
