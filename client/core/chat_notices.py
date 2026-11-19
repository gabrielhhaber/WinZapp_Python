"""Notices WhatsApp shows at the top of a one-to-one chat, made locally.

WhatsApp Web draws "Messages and calls are end-to-end encrypted..." and the
disappearing-messages notice itself; neither arrives as text from WPPConnect
(the encryption event is a bodyless ``e2e_notification``, which WinZapp keeps
out of the conversation on purpose). So the sentences are WinZapp's own, built
from facts the client can state:

* every one-to-one chat is end-to-end encrypted, so the encryption notice
  depends only on the JID being a person's, never on an event that may or may
  not have arrived;
* the timer is the chat's ``ephemeralDuration``, as in core/chat_settings.py.

Groups, broadcast lists and channels get no encryption claim here: channels
are not end-to-end encrypted, and groups and lists have their own notices.
"""

from core.chat_settings import disappearing_duration, disappearing_value, owner_call

_ONE_TO_ONE_SUFFIXES = ("@c.us", "@s.whatsapp.net", "@lid")


def is_one_to_one(jid) -> bool:
    """True for a person's JID (phone or @lid), false for everything else."""
    return isinstance(jid, str) and jid.endswith(_ONE_TO_ONE_SUFFIXES)


def encryption_lines(jid, i18n) -> list:
    """The full encryption sentence for the info dialog, or [] when the chat
    is not one-to-one."""
    if not is_one_to_one(jid):
        return []
    return [f"{i18n.t('chat_encryption_label')}: {i18n.t('chat_encryption_notice')}"]


def empty_conversation_key(jid) -> str:
    """The key of the sentence an empty conversation's one row starts with:
    a channel has updates, not messages, and a reader opening one with none
    hears that rather than a chat's "no messages"."""
    if isinstance(jid, str) and jid.endswith("@newsletter"):
        return "no_updates_in_channel"
    return "no_messages_in_conversation"


def conversation_start_text(owner, jid, i18n, empty_text) -> str:
    """The text of the row an empty conversation shows.

    *empty_text* is the existing "no messages" sentence. For a one-to-one chat
    the short encryption notice follows it and, only when the chat has a
    disappearing-messages timer running, so does the timer. An off or unknown
    timer adds nothing: a missing value is never reported as "off".
    """
    if not is_one_to_one(jid):
        return empty_text
    parts = [empty_text, i18n.t("chat_encryption_notice_short")]
    seconds = disappearing_duration(owner_call(owner, "get_chat", jid))
    if seconds:  # None (unknown) and 0 (off) both say nothing here
        label = i18n.t("chat_disappearing_label")
        parts.append(f"{label}: {disappearing_value(seconds, i18n)}.")
    return " ".join(parts)
