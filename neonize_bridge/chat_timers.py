"""Temporary messages on what the bridge SENDS.

Reported: in a chat with temporary messages on, a message sent from WinZapp
showed on the phone as permanent ("the sender may be using an incompatible
device"), but not every message. whatsmeow sends a message exactly as it is
built and never adds the chat's timer (only the timer-change notice carries
it, send.go), and WhatsApp reads the timer of each message from its own
`contextInfo.expiration`. A message without it is a permanent message in a
temporary chat. The ones that came out right were those that already carried
a contextInfo copied from elsewhere.

So the bridge keeps each chat's timer (chats.ephemeral / ephemeral_ts in the
store) from everything that tells it:

- the history sync (`Conversation.ephemeralExpiration`);
- a received message: the timer-change notice (protocolMessage
  EPHEMERAL_SETTING, which also says "off") or the `expiration` its sender's
  device put in it;
- a group's info event (`GroupInfoEv.Ephemeral`);
- the bridge's own `temporary-messages` and group `ephemeral` routes;

and `install()` wraps the client's `send_message` so every message to a chat
with a timer goes out with it. A message with expiration 0 is not "off": an
old device sends none, and only the notice turns the timer off.

A status (status@broadcast) never has a timer: it goes away by itself after
a day, WhatsApp Web sends it with no `expiration`, and the server refuses a
status that carries one ("server returned error 400"). The chat
status@broadcast still received statuses, and a status that stated a timer
gave it one, so every video and voice status after that was refused while the
same video went to a group. `has_timer()` keeps it out on reading as well,
for the stores that already hold one.

The pure functions are at the top (test_chat_timers.py).
"""
import logging

import msgmap

log = logging.getLogger("bridge")

#: ProtocolMessage.Type.EPHEMERAL_SETTING
EPHEMERAL_SETTING = 3
#: Contents that never carry the chat's timer.
UNTIMED = frozenset((
    "protocolMessage", "reactionMessage", "encReactionMessage", "pollUpdateMessage",
    "keepInChatMessage", "pinInChatMessage", "senderKeyDistributionMessage",
    "messageContextInfo",
))


def has_timer(jid) -> bool:
    """Whether the chat `jid` can have temporary messages: not a status, nor a
    broadcast list."""
    return not str(jid or "").endswith("@broadcast")


def timer_in(message):
    """(seconds, setting timestamp) a received message states for its chat's
    timer, or None when it says nothing about it."""
    if message is None:
        return None
    inner = msgmap.unwrap(message)
    try:
        if inner.HasField("protocolMessage"):
            notice = inner.protocolMessage
            if int(notice.type) != EPHEMERAL_SETTING:
                return None
            return (int(notice.ephemeralExpiration or 0),
                    int(getattr(notice, "ephemeralSettingTimestamp", 0) or 0))
    except ValueError:
        return None
    for field, value in inner.ListFields():
        if field.name in UNTIMED or field.message_type is None:
            continue
        if "contextInfo" not in field.message_type.fields_by_name:
            continue
        if not value.HasField("contextInfo"):
            continue
        seconds = int(value.contextInfo.expiration or 0)
        if seconds > 0:
            return seconds, int(value.contextInfo.ephemeralSettingTimestamp or 0)
    return None


def stamped(message, seconds, setting_ts=0):
    """A copy of `message` that disappears after `seconds`; `message` itself
    when there is no timer or nothing in it carries one. A plain text
    (`conversation`) has no contextInfo: it becomes an extendedTextMessage,
    as WhatsApp's own clients send it in a temporary chat."""
    if not seconds:
        return message
    copy = type(message)()
    copy.CopyFrom(message)
    target = msgmap.unwrap(copy)
    if target.HasField("conversation"):
        text = target.conversation
        target.ClearField("conversation")
        target.extendedTextMessage.text = text
    for field, value in target.ListFields():
        if field.name in UNTIMED or field.message_type is None:
            continue
        if "contextInfo" not in field.message_type.fields_by_name:
            continue
        value.contextInfo.expiration = int(seconds)
        if setting_ts:
            value.contextInfo.ephemeralSettingTimestamp = int(setting_ts)
        return copy
    return message


def text_message(client, text):
    """The Message neonize builds for a plain `text`, mentions included, so
    it can be stamped before sending (send_message builds it only inside)."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
        ContextInfo, ExtendedTextMessage, Message)
    try:
        groups = client._parse_group_mention(text)
        mentioned = client._parse_mention(text, False)
    except Exception:
        groups, mentioned = [], []
    return Message(extendedTextMessage=ExtendedTextMessage(
        text=text, contextInfo=ContextInfo(mentionedJID=mentioned, groupMentions=groups)))


def install(client, timer_of):
    """Wrap `client.send_message` so a message to a chat with a timer carries
    it. `timer_of(jid_text)` answers (seconds, setting_ts) or None; a failure
    there sends the message as it was."""
    original = getattr(client, "send_message", None)
    if original is None:        # a client that sends nothing (a test's stand-in)
        return None

    def send_message(to, message, *args, **kwargs):
        try:
            jid = f"{to.User}@{to.Server}" if getattr(to, "User", "") else str(to or "")
            timer = timer_of(jid) if jid else None
            if timer and timer[0]:
                if isinstance(message, str) and not kwargs.get("link_preview") and not args:
                    message = text_message(client, message)
                if not isinstance(message, str):
                    message = stamped(message, timer[0], timer[1])
        except Exception:
            log.exception("temporary messages: timer not applied")
        return original(to, message, *args, **kwargs)

    client.send_message = send_message
    return send_message


class ChatTimers:
    """`Session` methods that keep and read the chats' timers (it also
    inherits chat_aliases.ChatAlias, whose `chat_keys` joins number and LID)."""

    def chat_timer(self, jid):
        """(seconds, setting_ts) of `jid`'s chat, under whichever of its
        addresses is stored; None when unknown or when it cannot have one
        (has_timer). Blocks on a neonize call."""
        if not has_timer(jid):
            return None
        for key in self.chat_keys(jid) or [jid]:
            timer = self.db.chat_timer(key)
            if timer is not None:
                return timer
        return None

    def record_timer(self, jid, seconds, setting_ts=0):
        """Store the timer under every address the chat has, so the number and
        the LID never disagree about it."""
        if not has_timer(jid):
            return
        fields = {"ephemeral": int(seconds or 0)}
        if setting_ts:
            fields["ephemeral_ts"] = int(setting_ts)
        for key in self.chat_keys(jid) or [jid]:
            self.db.chat(key, **fields)

    def observe_timer(self, jid, message):
        """A received message that states the chat's timer updates it."""
        timer = timer_in(message)
        if timer is not None:
            self.record_timer(jid, *timer)
