"""Who joined and who left a group call, between two reports of who is
connected (the `call-participants` socket event: callParticipantsBridge.ts on
WPPConnect, call_media.report_rosters on Neonize). Both engines send the whole
connected list; the comparison lives here, once."""

#: how many calls' last lists are kept (a call's id is never reused)
KEPT = 8


def user_of(jid) -> str:
    return str(jid or "").partition("@")[0].split(":")[0]


def person_key(jid, person=None) -> str:
    """What two addresses of the same person share. *person* turns a jid into
    the one form the app knows that person by (the number of an @lid it has a
    mapping for: MainWindow._call_person); without it, or for an @lid nobody
    mapped yet, the address's own user. The roster names a person by number
    in one report and by @lid in another (seen in the offers of one call,
    2026-11-14), and their digits have nothing in common."""
    return user_of(person(jid) if person else jid)


def roster_changes(previous, connected, mine=frozenset(), person=None):
    """(joined, left) jids between *previous* (None: the first report, which
    says nothing: the people already there did not just join) and
    *connected*. *mine* are this account's own users (number and LID
    digits): the account itself never joins or leaves its own call here.
    Someone reported under their other address neither left nor joined."""
    now = [jid for jid in dict.fromkeys(connected or ()) if user_of(jid) not in mine]
    if previous is None:
        return [], []
    before = {person_key(jid, person) for jid in previous}
    current = {person_key(jid, person) for jid in now}
    return ([jid for jid in now if person_key(jid, person) not in before],
            [jid for jid in previous if person_key(jid, person) not in current])


def remember(rosters, call_id, connected, mine=frozenset(), person=None):
    """Store *call_id*'s list (without the account) and keep only the last
    KEPT calls. Returns roster_changes() against what was stored."""
    changes = roster_changes(rosters.get(call_id), connected, mine, person)
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


def left_of(seen_by_call, call_id, listed, person=None):
    """[(jid, name)] of who was seen in *call_id* and is no longer *listed*,
    in the order they were first seen, each person once. Compared by
    person_key(), so the same person reported once by number and once by LID
    is not "gone" (it took *person* to be true: a LID's digits are not the
    number's)."""
    now = {person_key(jid, person) for jid in listed or ()}
    gone = {}
    for jid, name in (seen_by_call.get(call_id) or {}).items():
        key = person_key(jid, person)
        if key not in now and key not in gone:
            gone[key] = (jid, name)
    return list(gone.values())
