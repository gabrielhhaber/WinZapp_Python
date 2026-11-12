"""Validate a live roster and format names without exposing WhatsApp IDs."""
import re
from core.call_matching import is_placeholder_call_id

_PERSON = re.compile(r"\d+@(?:lid|s\.whatsapp\.net|c\.us)")

#: A participant's state in call/participants, as the phone lists a group
#: call: connected, then ringing (called, not answered yet), then those who
#: left, declined or did not answer. A response without states (an older
#: server) lists only connected people.
CONNECTED = "connected"
STATES = (CONNECTED, "ringing", "left", "declined", "missed")


def active_participants_available(active, state):
    call_id = active.get("call_id") if isinstance(active, dict) else None
    return (isinstance(call_id, str) and bool(call_id) and not is_placeholder_call_id(call_id)
            and state in ("ACTIVE", "REJOINING"))


def parse_participants(body, call_id):
    data = body.get("response") if isinstance(body, dict) else None
    if (not isinstance(data, dict) or data.get("id") != call_id
            or type(data.get("available")) is not bool
            or not isinstance(data.get("participants"), list)
            or len(data["participants"]) > 1024):
        raise ValueError("Invalid participants response")
    if not data["available"]:
        if data["participants"]:
            raise ValueError("Ended call has participants")
        return None
    rows, seen = [], set()
    for item in data["participants"]:
        if (not isinstance(item, dict) or not isinstance(item.get("jid"), str)
                or not _PERSON.fullmatch(item["jid"]) or type(item.get("isSelf")) is not bool
                or not isinstance(item.get("name"), str)
                or item.get("state", CONNECTED) not in STATES):
            raise ValueError("Invalid participant")
        jid = item["jid"].replace("@c.us", "@s.whatsapp.net")
        key = "self" if item["isSelf"] else jid
        if key in seen:
            continue
        seen.add(key)
        rows.append({**item, "jid": jid, "key": key, "state": item.get("state", CONNECTED)})
    return rows


def connected_only(participants):
    return [p for p in participants or () if p.get("state", CONNECTED) == CONNECTED]


def _shown_name(name, i18n):
    if not isinstance(name, str) or not name.strip() or _PERSON.fullmatch(name.strip()):
        return i18n.t("unknown_contact")
    return " ".join(name.split())


def _user(jid) -> str:
    return str(jid or "").partition("@")[0].split(":")[0]


def participant_rows(participants, i18n, name_of, left=()):
    """The rows of the participant list, as the phone lists a group call:
    who is connected, then who is being called, then who left, declined or
    did not answer, each marked so. *left* ([(jid, name)]) are people this
    device saw connected that the server no longer lists at all."""
    def label(person_name, state):
        name = _shown_name(person_name, i18n)
        return (name if state == CONNECTED
                else i18n.t(f"call_participant_{state}_row").format(name=name))[:511]

    rows = []
    listed = set()
    for state in STATES:
        for person in participants:
            if person.get("state", CONNECTED) != state:
                continue
            listed.add(_user(person["jid"]))
            try:
                name = (i18n.t("call_participants_you") if person["isSelf"] else
                        name_of(person["jid"], person["name"]))
            except Exception:
                name = person["name"]
            key = person["key"] if state == CONNECTED else f"{state}:{person['key']}"
            rows.append((key, label(name, state)))
    for jid, hint in left or ():
        if _user(jid) in listed:
            continue
        try:
            name = name_of(jid, hint)
        except Exception:
            name = hint
        rows.append(("left:" + jid, label(name, "left")))
    return rows
