"""The rows of the starred messages window (issue #708).

The starred messages of every chat, newest first (Database.get_starred_messages),
each read as "chat, sender: text, date" (the sender only in a group or for
the account's own message: in a one-to-one chat it is the chat). The search
field narrows them by any part of the row, accents and case folded as the
contact search does.

Tested in test_starred_list.py.
"""
from dataclasses import dataclass

from core.utils import normalize_for_search


@dataclass(frozen=True)
class StarredEntry:
    jid: str
    message: dict
    text: str

    @property
    def message_id(self) -> str:
        return str((self.message.get("key") or {}).get("id") or "")


def one_line(text) -> str:
    return " ".join(str(text or "").split())


def row_text(i18n, chat, sender, text, when) -> str:
    """The row a screen reader reads; empty parts are left out."""
    body = one_line(text) or i18n.t("starred_no_text")
    key = "starred_row_sender" if sender else "starred_row"
    row = i18n.t(key).format(chat=one_line(chat), sender=one_line(sender), text=body)
    return f"{row}, {when}" if when else row


def matching(entries, query):
    """*entries* whose row holds every word of *query* (all of them for an
    empty query), in their order."""
    words = normalize_for_search(query or "", "nfd").split()
    if not words:
        return list(entries)
    return [e for e in entries
            if all(w in normalize_for_search(e.text, "nfd") for w in words)]


def without(entries, entry):
    """*entries* without the one for the same message of the same chat."""
    return [e for e in entries
            if not (e.jid == entry.jid and e.message_id == entry.message_id)]
