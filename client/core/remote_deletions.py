"""Which locally stored messages the phone no longer has.

Pulled out of MainWindow._reconcile_active_conversation_with_remote() because
getting this answer wrong does not show a stale bubble, it DELETES history:
whatever this returns goes to ConversationsPanel.remove_messages_by_id(), which
removes the row, the record and the database copy.

The comparison is between two windows that are not the same shape, and that is
the whole difficulty. The remote side is get-messages?count=<page size>: the
newest N entries WhatsApp Web holds, and those include entries WinZapp never
stores as a message of their own — the separate ``protocol``/``message_edit``
event every edit creates, undecryptable placeholders — so N remote entries
cover fewer real messages than N local ones. The local side is the chat's
records, which in turn hold things the server does not list under that id
(reactions under synthetic ``_rxn_`` ids) and sit in arrival order, not
timestamp order. Counting N from the end of each therefore does not land on the
same point in time, and whenever the local window reaches further back, its
oldest messages are simply outside what the server answered. Measured on a real
session (2026-09-16): one "no longer on the phone" removal in the open group on
nearly every 60 s poll — the list lost a row each time, and the removed message
was gone from the database.

Two rules close it, and both fail toward keeping a message:

- **Only messages newer than the oldest one the server returned are compared.**
  Strictly newer: a message sharing the edge timestamp may be the one the
  server's window stopped just short of.
- **Only real conversation content is compared** (the caller's predicate —
  ``is_countable_message()``). A reaction or a system event the phone
  "doesn't have" is a shape mismatch, not a deletion.

A missed phone-side deletion is cosmetic and fixes itself on the next reopen or
F5; a false one is irreversible. That asymmetry is why every doubt keeps the
message.

One consequence, accepted deliberately: a conversation cleared on the phone is
mirrored only while the server's answer stays *empty* for the confirming polls.
Once any new message lands, the answer's oldest timestamp is that message, no
older local message is compared any more, and the clear is not mirrored. Lifting
the bound for a short answer instead would bring back mass deletion on a
WhatsApp Web store that simply holds little history — the worse failure.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional


def message_timestamp_seconds(record: dict) -> int:
    """The record's timestamp in seconds, or 0 when it has none usable."""
    if not isinstance(record, dict):
        return 0
    ts = record.get("messageTimestamp") or record.get("timestamp") or 0
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return 0
    if ts > 1_000_000_000_000:
        ts //= 1000
    return max(ts, 0)


def oldest_timestamp(records: Iterable[dict]) -> Optional[int]:
    """Oldest usable timestamp among *records*, or None when none has one."""
    stamps = [message_timestamp_seconds(r) for r in records]
    stamps = [ts for ts in stamps if ts > 0]
    return min(stamps) if stamps else None


def comparable_local_records(
    records: list,
    limit: int,
    stable_cutoff: float,
    remote_oldest_ts: Optional[int],
    is_content: Callable[[dict], bool] = lambda r: True,
    extra_ids: Iterable[str] = (),
) -> list:
    """The local records the remote window can speak for, in record order.

    ``remote_oldest_ts`` None means the server returned nothing at all (an
    answer with entries but no usable timestamp is refused as ambiguous before
    this is reached), and only then does the old last-``limit`` slice decide on
    its own, because that is the shape a phone-side clear has
    (every local message gone), which the caller confirms over several polls
    before acting on.

    ``extra_ids`` are records to keep judging even once they fall out of the
    last-``limit`` slice — the ones already part of a running confirmation, so
    a deletion in a busy chat is not dropped from its own confirmation run by
    the messages that arrive while it is being confirmed. They still pass
    every other rule here.
    """
    if limit > 0 and len(records) > limit:
        extra = set(extra_ids or ())
        recent = [
            r for r in records[:-limit]
            if extra and isinstance(r, dict) and (r.get("key") or {}).get("id") in extra
        ] + records[-limit:]
    else:
        recent = records
    out = []
    for r in recent:
        if not isinstance(r, dict) or r.get("_local_pending"):
            continue
        mid = (r.get("key") or {}).get("id")
        # Synthetic ids (``_rxn_<target>``…) are WinZapp's own keys for things
        # WhatsApp never lists as a message under that id.
        if not mid or str(mid).startswith("_"):
            continue
        ts = message_timestamp_seconds(r)
        if not ts or ts >= stable_cutoff:
            continue
        if remote_oldest_ts is not None and ts <= remote_oldest_ts:
            continue
        try:
            if not is_content(r):
                continue
        except Exception:
            continue
        out.append(r)
    return out


def comparable_local_ids(
    records: list,
    limit: int,
    stable_cutoff: float,
    remote_oldest_ts: Optional[int],
    is_content: Callable[[dict], bool] = lambda r: True,
) -> set:
    """Ids of comparable_local_records() — see there."""
    return {
        (r.get("key") or {}).get("id")
        for r in comparable_local_records(
            records, limit, stable_cutoff, remote_oldest_ts, is_content
        )
    }
