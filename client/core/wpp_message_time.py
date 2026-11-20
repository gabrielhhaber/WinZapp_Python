"""When a WPPConnect message was sent, and what to do when it does not say.

A message get-messages returns is not always the message itself. When a reply
quotes a message the page has not loaded, WhatsApp Web builds a stand-in from
the quote (`fromQuotedMsg`) and keeps it in its MsgStore under the real id;
the history query then hands that stand-in back in the real message's place,
with its text but without `t`, `timestamp` or `ack`. Measured live
(2026-11-19): the stand-in of a group message received at 10:30:53 had no
`t`, yet still carried `clientReceivedTsMillis` (10:30:53.648). Stamped "now"
on a restart, it jumped below every later message of the conversation.

The server repairs that answer from WhatsApp Web's IndexedDB
(client/api_patches/src/util/messageStampRepair.ts); this is the client's
half, for a server that did not. A time that is only the receive time (the
server marks it `timestampSource: "received"`, or the payload has no `t`) is
used for the order but flagged GUESSED_TIMESTAMP, so a real time already
stored for the message always wins over it.
"""

GUESSED_TIMESTAMP = "_timestamp_guessed"
RECEIVED_SOURCE = "received"


def _seconds(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        seconds = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    if seconds > 1_000_000_000_000:
        seconds //= 1000
    return max(seconds, 0)


def wpp_message_seconds(wpp_msg) -> int | None:
    """The message's time in seconds, or None when the payload does not say.

    `timestamp`/`t` are WhatsApp's own; a quote stand-in still has the time
    this device received the message, which is the next best thing.
    """
    if not isinstance(wpp_msg, dict):
        return None
    for key in ("timestamp", "t"):
        seconds = _seconds(wpp_msg.get(key))
        if seconds:
            return seconds
    received_ms = wpp_msg.get("clientReceivedTsMillis")
    if isinstance(received_ms, bool):
        return None
    try:
        seconds = int(float(received_ms or 0)) // 1000
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def wpp_time_is_real(wpp_msg) -> bool:
    """Whether the payload's time is WhatsApp's own, not the receive time."""
    if not isinstance(wpp_msg, dict) or wpp_msg.get("timestampSource") == RECEIVED_SOURCE:
        return False
    return any(_seconds(wpp_msg.get(key)) for key in ("timestamp", "t"))


def keep_known_timestamp(msg, stored) -> dict:
    """*msg*, whose time is a guess, with the time of its *stored* copy.

    A real stored time replaces the guess and clears the flag. A stored guess
    too: the earlier of the two is kept and the flag stays, or a message with
    no time source would be re-stamped "now" on every sync and climb the chat
    list. *msg* is returned as is when nothing changes, else a copy.
    """
    if not isinstance(msg, dict) or not msg.get(GUESSED_TIMESTAMP) or not isinstance(stored, dict):
        return msg
    seconds = _seconds(stored.get("messageTimestamp"))
    if not seconds:
        return msg
    kept = dict(msg)
    if stored.get(GUESSED_TIMESTAMP):
        seconds = min(seconds, _seconds(msg.get("messageTimestamp")) or seconds)
    else:
        kept.pop(GUESSED_TIMESTAMP, None)
    kept["messageTimestamp"] = seconds
    return kept


def carry_over_known_timestamps(messages, stored) -> int:
    """Give a message whose time was guessed the time already stored for it
    (keep_known_timestamp). Returns how many messages took a stored time.
    """
    known = {}
    for record in stored or ():
        message_id = (record.get("key") or {}).get("id") if isinstance(record, dict) else None
        if message_id:
            known[message_id] = record
    carried = 0
    for message in messages or ():
        if not isinstance(message, dict) or not message.get(GUESSED_TIMESTAMP):
            continue
        kept = keep_known_timestamp(message, known.get((message.get("key") or {}).get("id")))
        if kept is not message:
            message.clear()
            message.update(kept)
            carried += 1
    return carried
