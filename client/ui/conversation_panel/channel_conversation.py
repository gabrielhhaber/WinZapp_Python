"""A channel's conversation: who may post and pin there, and its pinned updates.

Opening a channel reads it once through `channels/info` (core/channels.py,
both engines), on the pinned-messages overview's own read
(pinned_messages.py): the answer says this account's role and which updates
are pinned. The role is remembered per account in
``MainWindow._channel_roles`` (the Channels window fills it too), so the
composer and the message menu know whether this account posts and pins
there. A pinned update is one of the loaded ones, found by its server id;
pinning names that id.
"""

import logging

from core import channels as ch
from core.channels import CHANNEL_MEDIA_TYPES, CHANNEL_POST_KINDS
from core.group_invitation import connection_identity


def is_channel(jid) -> bool:
    return str(jid or "").endswith("@newsletter")


def remember_channel_roles(main_window, channels):
    """The roles a channels read said (the Channels window's list)."""
    roles = main_window.__dict__.setdefault("_channel_roles", {})
    for channel in channels or ():
        if channel.role:
            roles[channel.jid] = channel.role


def channel_allows(main_window, kind) -> bool:
    """Whether the engine posts *kind* in a channel. Before the probe answers
    it is assumed, as for WPPConnect; WhatsApp refuses what it does not take."""
    kinds = getattr(main_window, "_channel_post_kinds", None)
    return kind in CHANNEL_POST_KINDS and (kinds is None or kind in kinds)


def refuse_in_channel(panel, kind="") -> bool:
    """True (and says why) when *panel*'s open conversation is a channel and
    *kind* is not something WinZapp posts there: text always, a photo or video
    ("photo_video") and a poll ("poll") when the engine does; never a reply,
    document, audio, contact or voice note. Every entry to those stops here,
    keys included."""
    conversation = getattr(panel, "conversation", None)
    jid = conversation.get("remoteJid", "") if isinstance(conversation, dict) else ""
    if not is_channel(jid):
        return False
    window = panel.main_window
    if kind and channel_allows(window, kind):
        return False
    rich = any(channel_allows(window, k) for k in CHANNEL_POST_KINDS)
    window.output(window.i18n.t("channel_post_kinds" if rich else "channel_text_only"))
    return True


def refuse_attachments_in_channel(panel, staged) -> bool:
    """refuse_in_channel() for a batch of staged attachments: in a channel all
    of them must be photos or videos."""
    if all((item or {}).get("media_type") in CHANNEL_MEDIA_TYPES for item in staged or ()):
        return refuse_in_channel(panel, "photo_video")
    return refuse_in_channel(panel)


class ChannelConversationMixin:
    def _channel_role(self, jid) -> str:
        return getattr(self.main_window, "_channel_roles", {}).get(jid, "")

    def _channel_admin(self, jid) -> bool:
        return is_channel(jid) and ch.admin_role(self._channel_role(jid))

    def _can_pin_channel_update(self, msg) -> bool:
        jid = (self.conversation or {}).get("remoteJid", "")
        return self._channel_admin(jid) and bool(ch.server_id_of(msg))

    def _read_channel_state(self, jid):
        """The channel (role, pinned updates), or None (background thread)."""
        try:
            return ch.channel_info(connection_identity(self.main_window), jid=jid)
        except Exception:
            # A fixed line: the answer can carry the channel's name and id.
            logging.warning("[channel-conversation] could not read the channel")
            return None

    def _channel_pins_from(self, jid, channel):
        """Main thread: remember the role and turn the pinned server ids into
        the overview's messages, or None when the read failed."""
        if channel is None:
            return None
        if channel.role:
            remember_channel_roles(self.main_window, [channel])
            refresh = getattr(self, "refresh_composer_permissions", None)
            if refresh is not None:
                refresh(jid, transition=False)
        records = (self.conversation or {}).get("messages", {}).get("messages", {}).get("records", [])
        return ch.pinned_messages(records, channel.pinned)

    def _write_channel_pin(self, jid, msg, pin) -> bool:
        """Pin or unpin *msg* in the channel (background thread)."""
        try:
            ch.pin_update(connection_identity(self.main_window), jid, ch.server_id_of(msg), pin)
        except Exception:
            logging.warning("[channel-conversation] pin refused")
            return False
        return True
