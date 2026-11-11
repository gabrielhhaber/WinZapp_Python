"""Timestamp of a message the user has just sent, before the server stamps it.

A pending row is stamped with the PC clock; every other row in the list carries
the server's stamp. With the PC clock behind the server (a few seconds is
enough), a rebuild of the list before the next poll reconciles the row sorted
the reply *above* the message it answers. A row we send is never older than
what is already on screen, so the stamp is raised to the newest server-stamped
row, and to the quoted message when there is one.
"""


def _seconds(value):
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return 0
    return ts // 1000 if ts > 1_000_000_000_000 else ts


def pending_send_timestamp(now, messages, quoted=None):
    """`now` (PC clock, seconds) raised to the newest stamp already shown.

    `messages` are the rows on screen; rows this client is still sending
    (`_local_id`/`_local_pending`) carry the PC clock and are skipped, and so
    are separators and anything that is not a message dict.
    """
    stamp = int(now)
    for msg in messages or ():
        if not isinstance(msg, dict) or msg.get("_local_id") or msg.get("_local_pending"):
            continue
        stamp = max(stamp, _seconds(msg.get("messageTimestamp")))
    if isinstance(quoted, dict):
        stamp = max(stamp, _seconds(quoted.get("messageTimestamp")))
    return stamp
