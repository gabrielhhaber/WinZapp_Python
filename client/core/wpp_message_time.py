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
half, for a server that did not.
"""

GUESSED_TIMESTAMP = "_timestamp_guessed"


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


def carry_over_known_timestamps(messages, stored) -> int:
    """Give a message whose time was guessed the time already stored for it.

    A guess is "now", which places an old message below everything newer: the
    stored copy, when there is one with a real time, is the truth. Returns how
    many messages took the stored time.
    """
    known = {}
    for record in stored or ():
        if not isinstance(record, dict) or record.get(GUESSED_TIMESTAMP):
            continue
        message_id = (record.get("key") or {}).get("id")
        seconds = _seconds(record.get("messageTimestamp"))
        if message_id and seconds:
            known[message_id] = seconds
    carried = 0
    for message in messages or ():
        if not isinstance(message, dict) or not message.get(GUESSED_TIMESTAMP):
            continue
        seconds = known.get((message.get("key") or {}).get("id"))
        if seconds:
            message["messageTimestamp"] = seconds
            message.pop(GUESSED_TIMESTAMP, None)
            carried += 1
    return carried
