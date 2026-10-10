"""Identity candidates and ownership of optimistic chat reads."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(eq=False)
class ReadStateConfirmation:
    """Overlapping reads of the same chat/activity share positive evidence."""
    chat: dict
    timestamp: int
    activity: tuple
    server_version: int
    confirmed: bool = False
    retry_needed: bool = False
    rollback: ReadStateRequest | None = None


@dataclass(frozen=True, eq=False)
class ReadStateRequest:
    """A callback owns this exact request and chat, not just a second-wide t."""
    remote_jid: str
    chat: dict
    previous_unread: int
    read_timestamp: int
    server_unread: int
    server_version: int
    confirmation: ReadStateConfirmation


def read_state_activity(chat: dict) -> tuple:
    """Observe arrivals even when visible chats keep their unread count at zero.

    t is second-wide and the local unread counter deliberately ignores a
    visible arrival. Message identity must also delimit a read confirmation.
    """
    records = ((chat.get("messages") or {}).get("messages") or {}).get("records") or []
    latest = records[-1] if records else chat.get("lastMessage") or {}
    key = latest.get("key") or {}
    return (len(records), key.get("id") or latest.get("_local_id"),
            key.get("fromMe"), latest.get("messageTimestamp"))


def read_state_targets(remote_jid: str, phone_to_lid: dict, lid_to_phone: dict):
    """Prefer a known LID, retaining the phone alias; never bridge a group.

    The phone input itself remains a candidate after resolving its LID.
    Converting the input to its LID twice used to silently discard it.
    """
    jid = remote_jid
    if jid.endswith("@c.us"):
        jid = jid.rsplit("@", 1)[0] + "@s.whatsapp.net"
    if jid.endswith("@lid"):
        candidates = [jid, lid_to_phone.get(jid, "")]
    elif jid.endswith("@s.whatsapp.net"):
        candidates = [phone_to_lid.get(jid, ""), jid]
    else:
        candidates = [jid]
    targets = []
    for candidate in candidates:
        if not candidate:
            continue
        if candidate.endswith("@s.whatsapp.net"):
            candidate = candidate.rsplit("@", 1)[0] + "@c.us"
        target = (candidate, candidate.endswith("@lid"))
        if target not in targets:
            targets.append(target)
    return targets
