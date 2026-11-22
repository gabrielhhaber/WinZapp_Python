"""WhatsApp call links, without wx: reading a link, finding one in a message,
and what the waiting room of a link call says.

A call link is WhatsApp's "New call link": https://call.whatsapp.com/voice/<token>
or .../video/<token>, a group call anyone with the link can join. Opened in a
browser, its page hands whatsapp://call/<voice|video>/<token> to the program
registered for whatsapp: links, which can be WinZapp. Both engines
create and join it the way WhatsApp Web does (client/api_patches
callLinkRuntime.ts, neonize_bridge/call_links.py); the first person to join
becomes the call's admin. Someone who opens the link without a WhatsApp
account (a guest) always waits in the call's waiting room until the admin
lets them in, so the admin is told who is waiting.
"""

from __future__ import annotations

import re

# WhatsApp Web's WAWebParseCallLinkType: call.whatsapp.com and its
# web.whatsapp.com/call/ form; a token is 22 characters (WhatsApp Web refuses
# any other length as "Invalid call link").
_LINK = re.compile(
    r"https://(?:call\.whatsapp\.com|web\.whatsapp\.com/call)/(video|voice)/(\w+)(?:\?[^\s]*)?",
    re.IGNORECASE | re.ASCII)
# What the call link's web page asks the system to open, to hand the call to
# an installed WhatsApp (WAWebCallLinkDesktopAppDetect builds
# "whatsapp://call/<voice|video>/<token>"; WAWebApiParse reads it back), and
# so what WinZapp receives when it is registered for whatsapp: links
# (core/whatsapp_link_association.py). Never matched inside a message.
_APP_LINK = re.compile(r"whatsapp://call/(video|voice)/(\w+)/?(?:\?[^\s]*)?",
                       re.IGNORECASE | re.ASCII)
TOKEN_LENGTH = 22
#: Longer than any real link by a wide margin; a bound for text that arrives
#: from outside the process (command line, IPC).
MAX_CALL_LINK_LENGTH = 512


def parse_call_link(text) -> tuple:
    """(is_video, canonical https://call.whatsapp.com/... URL) when *text* is a
    call link and nothing else, else (None, "")."""
    text = str(text or "").strip()
    if len(text) > MAX_CALL_LINK_LENGTH:
        return None, ""
    found = _LINK.fullmatch(text) or _APP_LINK.fullmatch(text)
    return _canonical(found) if found else (None, "")


def link_is_voice_only(link) -> bool:
    """True when *link* is a call link made for voice: its call cannot take a
    camera (WhatsApp refuses the video upgrade there, status 670028)."""
    return parse_call_link(link)[0] is False


def _canonical(found) -> tuple:
    if len(found.group(2)) != TOKEN_LENGTH:
        return None, ""
    kind = found.group(1).lower()
    return kind == "video", f"https://call.whatsapp.com/{kind}/{found.group(2)}"


def waiting_people(response) -> list:
    """[{jid, name, guest}] of a call/link/waiting-room answer, rows without
    an address dropped."""
    people = (response or {}).get("people") if isinstance(response, dict) else None
    rows = []
    for person in people or ():
        if isinstance(person, dict) and isinstance(person.get("jid"), str) and person["jid"]:
            rows.append({"jid": person["jid"], "name": str(person.get("name") or ""),
                         "guest": bool(person.get("guest"))})
    return rows


def joined_call_id(body) -> str:
    """The call id a call/link/join answer carries. Raises ValueError when
    there is none: the join did not happen, whatever the status said."""
    call_id = str(body.get("id") or "") if isinstance(body, dict) else ""
    if not call_id:
        raise ValueError("The call link join answered no call id")
    return call_id


def waiting_room_admin(response) -> bool:
    """Whether a call/link/waiting-room answer says this user is the call's
    admin: only the admin may see or decide who waits."""
    return isinstance(response, dict) and response.get("admin") is True


def newly_waiting(announced, people) -> list:
    """The people of *people* not in *announced* (a set of jids): the ones to
    tell the admin about. Someone who left the room and asks again is new
    again once *announced* is rebuilt from the room (waiting_jids)."""
    return [p for p in people if p["jid"] not in announced]


def waiting_jids(people) -> set:
    return {p["jid"] for p in people}


def person_label(i18n, person) -> str:
    """How a waiting person is spoken and listed: their name, or "a guest" /
    "someone" -- never their number, which a guest does not even have."""
    name = str((person or {}).get("name") or "").strip()
    if name:
        return name
    return i18n.t("call_link_unnamed_guest" if (person or {}).get("guest") else "call_link_unnamed_person")


def arrivals_announcement(i18n, people) -> str:
    """What is spoken when *people* (newly_waiting) start waiting to enter."""
    if not people:
        return ""
    if len(people) == 1:
        return i18n.t("call_link_waiting_one").format(name=person_label(i18n, people[0]))
    return i18n.t("call_link_waiting_many").format(count=len(people))
