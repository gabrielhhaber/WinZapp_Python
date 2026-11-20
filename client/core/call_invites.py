"""Adding people to the call in progress (WhatsApp's "Add participant").

A group call offers the group's members; a private call offers the saved
contacts, and adding one turns it into a call with several people, as on
WhatsApp. Below them every one-to-one conversation is offered, saved contact
or not, and a typed phone number can be called.

Who may be added, and what the `call/add-participants` answer says. Both
engines answer the same route: the WPPConnect side calls WhatsApp Web's own
inviteToCall (client/api_patches/src/util/groupCallRuntime.ts), the Neonize
bridge meowcaller's AddParticipant (neonize_bridge/call_invites.py). Someone
already on the call's list who has not joined is rung again ("rang").

Pure functions, tested in tests/test_call_invites.py. Names are resolved by
the caller: a JID never reaches a row or a spoken line.
"""
import re

from core.call_logic import MAX_GROUP_CALL_PARTICIPANTS
from core.utils import person_dedup_key

_PERSON = re.compile(r"\d+@(?:lid|s\.whatsapp\.net|c\.us)")
STATUSES = ("invited", "rang", "failed")


def _canonical(jid) -> str:
    return str(jid or "").replace("@c.us", "@s.whatsapp.net")


def candidates(members, in_call, is_self, same_person, name_of):
    """[(jid, name)] of the group's members who may be added, by name.

    *members* are the group's participant JIDs; *in_call* the JIDs the call
    already lists as connected. *is_self(jid)* and *same_person(a, b)* cover
    the number/LID forms of one person; *name_of(jid)* gives the spoken name.
    """
    present = [_canonical(j) for j in in_call or ()]
    out, seen = [], set()
    for jid in members or ():
        jid = _canonical(jid)
        if not _PERSON.fullmatch(jid) or jid in seen or is_self(jid):
            continue
        # only against who is in the call (a few): a private call offers
        # every saved contact, thousands of them
        if any(same_person(jid, other) for other in present):
            continue
        seen.add(jid)
        name = " ".join(str(name_of(jid) or "").split())
        out.append((jid, name))
    out.sort(key=lambda row: row[1].casefold())
    return out


def person_addresses(jids, phone_of_lid, saved_name=None):
    """{jid: address}: the one address each person of *jids* is offered and
    invited by, whichever of their forms a list holds.

    A saved contact is kept under its number and under its @lid
    (MainWindow.contacts), the same number with and without the Brazilian 9th
    digit, so the same person came twice and could be invited twice. Forms are
    joined through the @lid mapping (*phone_of_lid*); the @lid is kept, as sends
    prefer it. An @lid whose number is not known yet joins the one person whose
    saved name (*saved_name(jid)*) it carries: the address book named both, and
    until the mapping arrives they were listed twice (2026-11-17). That
    person's own address is kept, the one known to be theirs.
    """
    def name_of(jid):
        return " ".join(str((saved_name or (lambda _j: ""))(jid) or "").split()).casefold()

    groups, unbridged = {}, []
    for jid in (_canonical(j) for j in jids or ()):
        if jid.endswith("@lid") and not phone_of_lid.get(jid):
            unbridged.append(jid)
        else:
            groups.setdefault(person_dedup_key(jid, phone_of_lid), []).append(jid)
    out, by_name = {}, {}
    for key, members in groups.items():
        address = next((j for j in members if j.endswith("@lid")), members[0])
        for jid in members:
            out[jid] = address
            if name_of(jid):
                by_name.setdefault(name_of(jid), set()).add(address)
    for jid in unbridged:
        owners = by_name.get(name_of(jid), ()) if name_of(jid) else ()
        # two people under that one name: neither is guessed
        out[jid] = next(iter(owners)) if len(owners) == 1 else jid
    return out


def offered_lists(sources, phone_of_lid, saved_name=None):
    """[[address, ...], ...]: each list of *sources* with one address per
    person (person_addresses), the same address for a person in every list,
    so a mark made in one list reads as made in the other."""
    addresses = person_addresses([j for source in sources for j in source or ()],
                                 phone_of_lid, saved_name)
    out = []
    for source in sources:
        seen, row = set(), []
        for jid in source or ():
            address = addresses[_canonical(jid)]
            if address not in seen:
                seen.add(address)
                row.append(address)
        out.append(row)
    return out


def saved_name_of(contacts):
    """jid -> the name the address book gives (MainWindow.contacts), '' for
    anyone not saved: a nickname never makes two people one."""
    def name(jid):
        record = (contacts or {}).get(jid)
        if not isinstance(record, dict) or not (
                record.get("isMyContact") or record.get("isSaved") or record.get("syncToAddressbook")):
            return ""
        return str(record.get("name") or "")
    return name


def conversation_jids(chats, hidden=None):
    """The people of the one-to-one conversations of *chats* (MainWindow.chats,
    jid -> chat), saved contacts or not: the second list of the dialog.
    *hidden(jid)* is True for a chat that must not be named here: a locked one
    (its name is the vault's to show) or one the user deleted."""
    return [jid for jid in chats or ()
            if _PERSON.fullmatch(_canonical(jid)) and not (hidden and hidden(jid))]


def picked_people(rows, marks):
    """[(jid, name)] of the marked rows of every list, each person once."""
    out, seen = [], set()
    for jid, name in rows:
        if jid in marks and jid not in seen:
            seen.add(jid)
            out.append((jid, name))
    return out


def saved_contact_jids(contacts):
    """The address-book contacts of *contacts* (MainWindow.contacts, jid ->
    record): who a private call may be widened to, as WhatsApp's picker
    offers there."""
    out = []
    for jid, record in (contacts or {}).items():
        if isinstance(record, dict) and (record.get("isMyContact") or record.get("isSaved")
                                         or record.get("syncToAddressbook")):
            out.append(jid)
    return out


def room_left(connected_count) -> int:
    """How many more people the call can take (WhatsApp's group-call cap)."""
    return max(0, MAX_GROUP_CALL_PARTICIPANTS - max(0, int(connected_count or 0)))


def parse_results(body, call_id, asked):
    """{jid: status} from the `call/add-participants` answer. Raises ValueError
    on an answer about another call or one that names people not asked."""
    data = body.get("response") if isinstance(body, dict) else None
    if not isinstance(data, dict) or data.get("id") != call_id or not isinstance(data.get("results"), list):
        raise ValueError("Invalid add-participants response")
    asked = {_canonical(j): j for j in asked}
    out = {}
    for row in data["results"]:
        if not isinstance(row, dict) or row.get("status") not in STATUSES:
            raise ValueError("Invalid add-participants result")
        jid = _canonical(row.get("jid"))
        if jid not in asked:
            raise ValueError("A result for someone not asked")
        out[asked[jid]] = row["status"]
    return out


def request_timeout(people) -> int:
    """Seconds to wait for `call/add-participants`. The Neonize bridge answers
    only after WhatsApp's server confirmed each person, one at a time, and
    gives up on one after 15 s (meowcaller patch 0029): a fixed 60 s ran out
    with four people the server did not answer for."""
    return 30 + 20 * max(1, int(people or 0))


def calling_line(names, i18n) -> str:
    """The line spoken while the request is out: who is being called. "Added"
    is only said by summary(), once the server confirmed."""
    return i18n.t("call_invite_sending").format(
        names=", ".join(name or i18n.t("unknown_contact") for name in names))


def summary(results, names, i18n) -> str:
    """The line spoken after the request, when WhatsApp's server answered: who
    was invited or rung, and who could not be."""
    ok = [names.get(jid) or i18n.t("unknown_contact")
          for jid, status in results.items() if status in ("invited", "rang")]
    failed = [names.get(jid) or i18n.t("unknown_contact")
              for jid, status in results.items() if status == "failed"]
    missing = [names.get(jid) or i18n.t("unknown_contact") for jid in names if jid not in results]
    failed += missing
    parts = []
    if ok:
        parts.append(i18n.t("call_invite_sent_people").format(names=", ".join(ok)))
    if failed:
        parts.append(i18n.t("call_invite_failed_people").format(names=", ".join(failed)))
    return " ".join(parts) or i18n.t("call_invite_failed")
