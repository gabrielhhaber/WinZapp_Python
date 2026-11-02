"""Read and played receipts that respect the account's privacy setting.

Reported: with "Read receipts" turned off in WhatsApp's privacy settings,
contacts still saw the blue ticks for what was read in WinZapp on Neonize.
The bridge always sent the `read` receipt (and `played` for a voice message).
WhatsApp's own clients, with the setting off, send `read-self` / `played-self`
instead: the account's other devices learn the message was read, the sender
does not. In a group the receipts always go, whatever the setting, as on the
phone.

The setting comes from whatsmeow's cached privacy settings (fetched once,
kept current by WhatsApp's own notices and by the bridge's privacy route). An
unknown value keeps the last one known, and with none known the receipt is
the plain one, as before.

A read goes by these receipts alone. WhatsApp's own clients send the
markChatAsRead state notice only to undo a chat marked unread by hand, and the
reads they make on other devices reach the bridge as `READ` receipts of ours,
with no notice behind them. The bridge used to send a notice for every chat
opened: 585 in five days, each a patch of its own in `regular_low`. A device
applies those one at a time (0.7 s each, measured on WhatsApp Web), so after
days away it took seven minutes to reach them, long after the messages of those
days had come in and been counted as unread again. See needs_read_notice().
"""
import logging

from profile_privacy import settings_to_wpp

log = logging.getLogger("bridge")


def receipt_name(chat, kind, read_receipts):
    """The neonize ReceiptType name for a `kind` ('READ' or 'PLAYED') receipt
    in `chat`, with the account's `read_receipts` setting ('all', 'none' or
    None when unknown)."""
    if read_receipts == "none" and not str(chat or "").endswith("@g.us"):
        return kind + "_SELF"
    return kind


def read_receipts_setting(client, last_known=None):
    """'all' or 'none' from whatsmeow's privacy settings; `last_known` when
    they do not say (whatsmeow answers an empty struct on a failed fetch)."""
    try:
        value = settings_to_wpp(client.get_privacy_settings()).get("readReceipts")
    except Exception:
        log.exception("read receipts setting")
        value = None
    return value if value in ("all", "none") else last_known


def needs_read_notice(unread, receipts, marked_unread):
    """Whether reading a chat also takes a state notice: it was marked unread by
    hand, which no receipt undoes, or it counts more `unread` messages than the
    `receipts` going out can name (history the store does not hold)."""
    return bool(marked_unread) or unread > receipts


class ReadReceipts:
    """`Session` method choosing the receipt it sends."""

    _read_receipts = None

    def receipt_for(self, chat, kind):
        """The neonize ReceiptType to send for `kind` ('READ'/'PLAYED') in `chat`."""
        from neonize.utils.enum import ReceiptType
        self._read_receipts = read_receipts_setting(self.client, self._read_receipts)
        return getattr(ReceiptType, receipt_name(chat, kind, self._read_receipts))
