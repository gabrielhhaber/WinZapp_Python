"""Channel invitations received in a chat: to follow a channel, or to be one
of its admins.

WhatsApp Web receives both and shows them as cards (WAWebParseNewsletter-
FollowerInviteMessageProto and ...AdminInviteMessageProto, read 2026-11-09):
the channel's name, the inviter's words and, for an admin, when the invitation
expires. WinZapp showed them as nothing. The message arrives:

* from WPPConnect as wa-js serializes WhatsApp Web's model: type
  ``newsletter_follower_invite`` / ``newsletter_admin_invite`` with
  ``newsletterFollowerInviteInfo`` / ``newsletterAdminInviteInfo``
  ({newsletterId, newsletterName, inviteMessage, inviteExpiration});
* from the Neonize bridge in that same shape (neonize_bridge/msgmap.py).

Both become a message of the client's own types below, with
{newsletterJid, newsletterName, caption, inviteExpiration}. Enter on one
opens the channel, where it can be followed. Sending an invitation is not
offered: WhatsApp Web has no sender for it (only the phone sends one).

Pure logic, no wx; tested in tests/test_channel_invites.py.
"""

FOLLOWER = "newsletterFollowerInviteMessage"
ADMIN = "newsletterAdminInviteMessage"
INVITE_TYPES = (FOLLOWER, ADMIN)
#: WhatsApp Web's message type -> (the client's type, the model's info field)
WPP_TYPES = {
    "newsletter_follower_invite": (FOLLOWER, "newsletterFollowerInviteInfo"),
    "newsletter_admin_invite": (ADMIN, "newsletterAdminInviteInfo"),
}


def _jid(value) -> str:
    """A channel id as text: a Wid serializes as {_serialized} or {user, server}."""
    if isinstance(value, dict):
        serialized = value.get("_serialized")
        if isinstance(serialized, str) and serialized:
            return serialized
        user, server = value.get("user"), value.get("server")
        return f"{user}@{server}" if user and server else ""
    return value if isinstance(value, str) else ""


def from_wpp(wpp_type, wpp_msg):
    """(client type, message content) of a WPPConnect invitation, or None."""
    entry = WPP_TYPES.get(wpp_type)
    if entry is None or not isinstance(wpp_msg, dict):
        return None
    client_type, field = entry
    info = wpp_msg.get(field)
    info = info if isinstance(info, dict) else {}
    jid = _jid(info.get("newsletterId"))
    if not jid.endswith("@newsletter"):
        return None
    content = {"newsletterJid": jid,
               "newsletterName": str(info.get("newsletterName") or ""),
               "caption": str(info.get("inviteMessage") or "")}
    try:
        expiration = int(info.get("inviteExpiration") or 0)
    except (TypeError, ValueError):
        expiration = 0
    if expiration:
        content["inviteExpiration"] = expiration
    return client_type, {client_type: content}


def content_of(msg) -> dict:
    """The invitation's fields of a client message, or {}."""
    if not isinstance(msg, dict) or msg.get("messageType") not in INVITE_TYPES:
        return {}
    body = (msg.get("message") or {}).get(msg["messageType"])
    return body if isinstance(body, dict) else {}


def channel_of(msg):
    """(channel jid, channel name) the invitation is about, or None."""
    body = content_of(msg)
    jid = body.get("newsletterJid") or ""
    return (jid, body.get("newsletterName") or "") if jid.endswith("@newsletter") else None


def row_text(i18n, msg) -> str:
    """The message list's text: what is offered, the channel and the words."""
    body = content_of(msg)
    name = body.get("newsletterName") or i18n.t("channels_unnamed")
    key = ("channel_invite_admin_row" if msg.get("messageType") == ADMIN
           else "channel_invite_follow_row")
    text = i18n.t(key).format(name=name)
    caption = (body.get("caption") or "").strip()
    return f"{text}. {caption}" if caption else text
