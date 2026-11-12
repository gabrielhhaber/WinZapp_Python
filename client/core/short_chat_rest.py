"""Short chats that keep coming back empty rest between retries (issue #766).

A chat short of a full page that the user never opened is fetched again at
every sweep of the backfill, but the phone is never asked about it (that
would be a notification nobody wants), so once WhatsApp has nothing more for
it, every retry returns nothing. Reported on a large account: 60 of 257
short chats came back empty at every pass, and the sweep they filled is what
deep history waits for, so deep visits ran about every 3.5 minutes while
each one stored up to 8,000 messages.

A chat whose fetch brings nothing now rests before the next try: 2 minutes,
then twice as long after every further empty try, up to 30 minutes. It is
still retried, so a chat WhatsApp fills later is still caught; it only stops
taking a turn in every sweep. Nothing rests:

* while history is still landing (a chunk can fill any chat at any time);
* once the user opened the chat (it gets the full treatment, phone included);
* after the fetch brought something (the streak starts over).

Pure: the state is a dict the caller keeps, the clock is passed in.
"""

FIRST_REST = 2 * 60
MAX_REST = 30 * 60


def resting(rest: dict, jid: str, now: float) -> bool:
    """Whether *jid* is still in its rest at *now*."""
    entry = rest.get(jid)
    return entry is not None and now < entry[1]


def due_chats(pending, rest: dict, now: float, *, opened, landing: bool) -> list:
    """The pending chats that take a turn now, in their order."""
    if landing:
        return list(pending)
    return [jid for jid in pending if opened(jid) or not resting(rest, jid, now)]


def note_fetch(rest: dict, jid: str, brought_something: bool, now: float, *, opened) -> None:
    """Record how *jid*'s fetch went: an empty one lengthens its rest."""
    if brought_something or opened(jid):
        rest.pop(jid, None)
        return
    streak = rest.get(jid, (0, 0.0))[0] + 1
    rest[jid] = (streak, now + min(FIRST_REST * 2 ** (streak - 1), MAX_REST))
