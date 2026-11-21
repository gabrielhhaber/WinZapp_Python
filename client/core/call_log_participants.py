"""Who took part in a call that started one-to-one and became a group call.

WhatsApp keeps such a call as an ordinary ``call_log`` record of the
one-to-one chat (or of the account's own chat, for one this account placed)
whose ``callParticipants`` lists more than the two ends: ``[{participant,
outcome}]``, the participant a JID string (measured over CDP, 2026-11-17).
The phone and WhatsApp Web then name the call after its people instead of
the chat: "A", "A and B", "A, B and C", "A and 4 more people"
(WAWebCallLogTitle, WAWebParticipantListUtils.formatParticipantWidsPreserveOrder).
The order is WAWebCallLogUtils.sortCallParticipantWids': whoever called this
account first, then saved contacts, then everyone else, as listed.

Pure: no wx, no window. MainWindow.call_log_participants_text() supplies the
names and the account's own users.
"""

from __future__ import annotations

from core.group_activity import join_names

# More names than this and the title becomes "first and N more people".
_NAMED_AT_MOST = 3


def _plain_jid(value) -> str:
    """A participant JID with no device, phone JIDs in the canonical form."""
    if isinstance(value, dict):
        value = value.get("_serialized") or ""
    text = str(value or "")
    if "@" not in text:
        return ""
    user, server = text.rsplit("@", 1)
    user = user.split(":")[0]
    if not user:
        return ""
    if server == "c.us":
        server = "s.whatsapp.net"
    return f"{user}@{server}"


def call_participant_jids(raw_participants) -> list:
    """The JIDs of a raw ``callParticipants`` list, once each, in order."""
    out = []
    for entry in raw_participants or ():
        jid = _plain_jid(entry.get("participant") if isinstance(entry, dict) else entry)
        if jid and jid not in out:
            out.append(jid)
    return out


def _user(jid) -> str:
    return str(jid or "").partition("@")[0].split(":")[0]


def other_call_participants(jids, mine, person=None) -> list:
    """*jids* without this account (*mine*: its users' digits), one entry per
    person: *person* maps a JID to the one form the app knows that person by
    (MainWindow._call_person), so an @lid and its number count once."""
    person = person or (lambda jid: jid)
    out, seen = [], set()
    for jid in jids or ():
        key = _user(person(jid))
        if not key or _user(jid) in mine or key in mine or key in seen:
            continue
        seen.add(key)
        out.append(jid)
    return out


def order_call_participants(jids, caller="", is_saved=None, person=None) -> list:
    """WhatsApp's order: *caller* (who called this account, if anyone) first,
    then saved contacts, then the rest, keeping the listed order otherwise."""
    person = person or (lambda jid: jid)
    caller_key = _user(person(caller)) if caller else ""
    is_saved = is_saved or (lambda _jid: False)

    def rank(indexed):
        index, jid = indexed
        first = 0 if caller_key and _user(person(jid)) == caller_key else 1
        return (first, 0 if is_saved(jid) else 1, index)

    return [jid for _i, jid in sorted(enumerate(jids or ()), key=rank)]


def participants_title(names, t) -> str:
    """"A", "A and B", "A, B and C", or "A and N more people" (N the rest)."""
    names = [n for n in names or () if n]
    if len(names) <= _NAMED_AT_MOST:
        return join_names(names, t)
    return t("call_log_participants_more").format(first=names[0], count=len(names) - 1)


def is_upgraded_call(chat_jid, others) -> bool:
    """A call of a chat that is not a group, with two or more other people:
    the one-to-one call that became a group call. A plain one-to-one call
    keeps the chat's own name."""
    return not str(chat_jid or "").endswith("@g.us") and len(others or ()) >= 2
