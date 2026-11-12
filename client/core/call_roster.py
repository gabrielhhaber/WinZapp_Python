"""Who joined and who left a group call, between two reports of who is
connected (the `call-participants` socket event: callParticipantsBridge.ts on
WPPConnect, call_media.report_rosters on Neonize). Both engines send the whole
connected list; the comparison lives here, once."""

#: how many calls' last lists are kept (a call's id is never reused)
KEPT = 8


def user_of(jid) -> str:
    return str(jid or "").partition("@")[0].split(":")[0]


def roster_changes(previous, connected, mine=frozenset()):
    """(joined, left) jids between *previous* (None: the first report, which
    says nothing: the people already there did not just join) and
    *connected*. *mine* are this account's own users (number and LID
    digits): the account itself never joins or leaves its own call here."""
    now = [jid for jid in dict.fromkeys(connected or ()) if user_of(jid) not in mine]
    if previous is None:
        return [], []
    before = set(previous)
    current = set(now)
    return ([jid for jid in now if jid not in before],
            [jid for jid in previous if jid not in current])


def remember(rosters, call_id, connected, mine=frozenset()):
    """Store *call_id*'s list (without the account) and keep only the last
    KEPT calls. Returns roster_changes() against what was stored."""
    changes = roster_changes(rosters.get(call_id), connected, mine)
    rosters.pop(call_id, None)
    rosters[call_id] = [jid for jid in dict.fromkeys(connected or ()) if user_of(jid) not in mine]
    while len(rosters) > KEPT:
        rosters.pop(next(iter(rosters)))
    return changes


def note_seen(seen_by_call, call_id, people, mine=frozenset()):
    """Remember everyone *people* ([(jid, name)]) shows connected to *call_id*,
    so that someone who leaves stays listed as gone, as the phone lists
    them. The account itself is never kept; only the last KEPT calls are."""
    seen = seen_by_call.pop(call_id, None) or {}
    for jid, name in people or ():
        if jid and user_of(jid) not in mine:
            if name or jid not in seen:
                seen[jid] = name or seen.get(jid, "")
    seen_by_call[call_id] = seen
    while len(seen_by_call) > KEPT:
        seen_by_call.pop(next(iter(seen_by_call)))


def left_of(seen_by_call, call_id, connected):
    """[(jid, name)] of who was seen in *call_id* and is no longer connected,
    in the order they were first seen. Compared by user, so the same person
    reported once by number and once by LID is not "gone"."""
    now = {user_of(jid) for jid in connected or ()}
    return [(jid, name) for jid, name in (seen_by_call.get(call_id) or {}).items()
            if user_of(jid) not in now]
