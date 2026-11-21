"""Encrypted reactions and comments of community announcements through
neonize's own whatsmeow calls (neonize 0.12: `decrypt_reaction`,
`encrypt_reaction`, `decrypt_comment`, `encrypt_comment`).

The key of both is derived from the `messageSecret` of the commented message,
which whatsmeow itself keeps and looks up for every combination of number and
LID. comments.py has its own copy of the comment cipher that reads the secret
from whatsmeow's database; it stays as the fallback when a call fails or the
neonize build is older than 0.12.

Every function answers None instead of raising: the caller falls back. Nothing
here logs a JID, number or message text.
"""
import logging

import msgmap

log = logging.getLogger("bridge")


def root_info(chat: str, sender: str, from_me: bool, msg_id: str):
    """The neonize `MessageInfo` of the commented or reacted-to message, which
    is all whatsmeow reads of it (chat, sender, id and who sent it)."""
    from neonize.proto.Neonize_pb2 import JID, MessageInfo, MessageSource
    from neonize.utils import build_jid

    def jid(text):
        user, _, server = (text or "").partition("@")
        return build_jid(user.split(":")[0], server)
    empty = build_jid("", "")
    source = MessageSource(Chat=jid(chat), Sender=jid(sender), IsFromMe=bool(from_me),
                           IsGroup=chat.endswith("@g.us"), SenderAlt=empty,
                           RecipientAlt=empty, BroadcastListOwner=empty)
    return MessageInfo(MessageSource=source, ID=msg_id, ServerID=0, Type="", Pushname="",
                       Timestamp=0, Category="", Multicast=False, MediaType="", Edit="")


def is_encrypted_reaction(message) -> bool:
    return msgmap.unwrap(message).HasField("encReactionMessage")


def open_reaction(client, event):
    """(target id, emoji, key.fromMe, participant, sender timestamp in ms) of an
    encrypted reaction event, as msgmap.reaction gives them for a plain one, or
    None. The target is in the envelope: the encrypted reaction has no key."""
    if client is None or not is_encrypted_reaction(event.Message):
        return None
    try:
        opened = client.decrypt_reaction(event)
    except Exception as error:
        log.info("encrypted reaction I could not open (%s)", type(error).__name__)
        return None
    key = msgmap.unwrap(event.Message).encReactionMessage.targetMessageKey
    if not key.ID:
        return None
    return (key.ID, opened.text, bool(key.fromMe), msgmap.jid_wpp(key.participant),
            opened.senderTimestampMS)


def seal_reaction(client, info, reaction):
    """The `waE2E.Message` to send for an encrypted reaction, or None."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    try:
        return Message(encReactionMessage=client.encrypt_reaction(info, reaction))
    except Exception as error:
        log.info("reaction not encrypted (%s)", type(error).__name__)
        return None


def open_comment(client, event):
    """The comment (`waE2E.Message`) inside an encrypted comment event, or None."""
    if client is None or not hasattr(client, "decrypt_comment"):
        return None
    try:
        return client.decrypt_comment(event)
    except Exception as error:
        log.info("comment not opened by neonize (%s)", type(error).__name__)
        return None


def seal_comment(client, info, comment):
    """The `waE2E.Message` with the EncCommentMessage to send, or None."""
    if client is None or not hasattr(client, "encrypt_comment"):
        return None
    try:
        return client.encrypt_comment(info, comment)
    except Exception as error:
        log.info("comment not encrypted by neonize (%s)", type(error).__name__)
        return None
