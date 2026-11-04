"""Adding people to the call in progress (WhatsApp's "Add participant").

A group call offers the group's members; a private call offers the saved
contacts, and adding one turns it into a call with several people, as on
WhatsApp.

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


def summary(results, names, i18n) -> str:
    """The line spoken after the request: how many were invited or rung, and
    who could not be."""
    ok = sum(1 for status in results.values() if status in ("invited", "rang"))
    failed = [names.get(jid) or i18n.t("unknown_contact")
              for jid, status in results.items() if status == "failed"]
    missing = [names.get(jid) or i18n.t("unknown_contact") for jid in names if jid not in results]
    failed += missing
    parts = []
    if ok:
        parts.append(i18n.t("call_invite_sent").format(count=ok))
    if failed:
        parts.append(i18n.t("call_invite_failed_people").format(names=", ".join(failed)))
    return " ".join(parts) or i18n.t("call_invite_failed")
