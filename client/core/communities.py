"""The account's communities and the actions on them.

Reading is WinZapp's own route (GET communities), and so is promotion: the
library's cannot promote anyone who is not an admin already. The other
actions are the upstream wppconnect-server routes, used as they are: they
answer the library's raw result, which has no single shape (an object, a
`success` flag, or no body at all for a function that returns nothing). So an
answer only says whether the request was refused; an action is believed when
a fresh reading shows its effect. See run_action().

Nothing here retries an action: creating a community twice is worse than
saying "not confirmed".
"""

import re
from dataclasses import dataclass
from core.api_client import api_get, api_post
from core.group_operations import group_jid
from core import read_back

_PERSON = re.compile(r"\d+(?::\d+)?@(?:c\.us|lid|s\.whatsapp\.net)")
_IDENTIFIER = re.compile(r"\d+(?:-\d+)?(?::\d+)?@(?:g\.us|lid|c\.us|s\.whatsapp\.net)\b")
NAME_LIMIT = 100
DESCRIPTION_LIMIT = 2048


@dataclass(frozen=True)
class Subgroup:
    jid: str
    name: str
    announcement: bool
    general: bool


@dataclass(frozen=True)
class Admin:
    jid: str
    creator: bool


@dataclass(frozen=True)
class Community:
    jid: str
    name: str
    description: str
    i_am_admin: bool
    admins: tuple
    subgroups: tuple

    @property
    def announcement_group(self):
        return next((group for group in self.subgroups if group.announcement), None)


def _name(value):
    # The provider's raw identifier must never become a display name.
    return value.strip() if isinstance(value, str) and not _IDENTIFIER.search(value) else ""


def parse_communities(body):
    """The communities of a `GET communities` answer; ValueError when it is
    not one. An entry that is not a community is dropped, never guessed."""
    data = body.get("response") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(data, list):
        raise ValueError("communities_unavailable")
    communities = []
    for item in data:
        jid = group_jid(item.get("id")) if isinstance(item, dict) else ""
        if not jid:
            continue
        admins = tuple(
            Admin(admin["id"], admin.get("superAdmin") is True)
            for admin in item.get("admins") or ()
            if isinstance(admin, dict) and isinstance(admin.get("id"), str)
            and _PERSON.fullmatch(admin["id"]))
        subgroups = tuple(
            Subgroup(group_jid(group.get("id")), _name(group.get("subject")),
                     group.get("announcement") is True, group.get("general") is True)
            for group in item.get("subgroups") or ()
            if isinstance(group, dict) and group_jid(group.get("id")))
        description = item.get("description")
        communities.append(Community(
            jid, _name(item.get("subject")), description.strip() if isinstance(description, str) else "",
            item.get("iAmAdmin") is True, admins, subgroups))
    return communities


def _url(connection, endpoint):
    server, port, token = connection
    return f"{server}:{port}/api/{token}/{endpoint}", {"Authorization": f"Bearer {token}"}


def list_communities(connection):
    url, headers = _url(connection, "communities")
    response = api_get(url, headers=headers, timeout=20)
    if response.status_code != 200:
        raise ValueError("communities_unavailable")
    return parse_communities(response.json())


def _act(connection, endpoint, payload):
    """Send one action. Returns when it was not refused, which is not yet
    "it happened": run_action() reads the effect back."""
    url, headers = _url(connection, endpoint)
    response = api_post(url, json=payload, headers=headers, timeout=60)
    if response.status_code != 200:
        raise ValueError("community_action_refused")
    try:
        body = response.json()
    except ValueError:
        return  # a library function that returns nothing answers 200 with no body
    if isinstance(body, dict) and (body.get("status") == "error" or body.get("success") is False):
        # demote answers 200 with success: false when WhatsApp refuses it.
        raise ValueError("community_action_refused")


def _groups(jids):
    groups = [group_jid(jid) for jid in jids]
    if not all(groups):
        raise ValueError("invalid_group")
    return groups


def _community(jid):
    jid = group_jid(jid)
    if not jid:
        raise ValueError("invalid_community")
    return jid


def create_community(connection, name, description, group_jids):
    name = name.strip() if isinstance(name, str) else ""
    description = description.strip() if isinstance(description, str) else ""
    if not name or len(name) > NAME_LIMIT or len(description) > DESCRIPTION_LIMIT:
        raise ValueError("invalid_community")
    _act(connection, "create-community",
         {"name": name, "description": description, "groupIds": _groups(group_jids)})


def deactivate_community(connection, jid):
    _act(connection, "deactivate-community", {"id": _community(jid)})


def link_groups(connection, jid, group_jids):
    _act(connection, "add-community-subgroup", {"id": _community(jid), "groupsIds": _groups(group_jids)})


def unlink_groups(connection, jid, group_jids):
    _act(connection, "remove-community-subgroup", {"id": _community(jid), "groupsIds": _groups(group_jids)})


def promote_admin(connection, jid, person):
    # WinZapp's own route (communityAdminController.ts), which takes the
    # person in WhatsApp Web's own forms only.
    person = person.replace("@s.whatsapp.net", "@c.us") if isinstance(person, str) else ""
    if not re.fullmatch(r"\d+@(?:c\.us|lid)", person):
        raise ValueError("invalid_participant")
    _act(connection, f"community-admin/{_community(jid)}/promote", {"participant": person})


def demote_admin(connection, jid, person):
    # The library's own route: an admin is in the list it looks the person up in.
    if not isinstance(person, str) or not _PERSON.fullmatch(person):
        raise ValueError("invalid_participant")
    _act(connection, "demote-community-participant", {"id": _community(jid), "participantsId": [person]})


def run_action(connection, action, achieved, read=None, **options):
    """Run *action* once and read the communities back until *achieved* says
    its effect is there: (state, communities), as core.read_back.run_action()."""
    read = read or list_communities
    return read_back.run_action(action, lambda: read(connection), achieved, **options)


def find(communities, jid):
    return next((community for community in communities if community.jid == jid), None)


def created(known_jids, name):
    """A community that was not there before and carries the name asked for."""
    name = name.strip()
    return lambda communities: any(
        community.jid not in known_jids and community.name == name for community in communities)


def gone(jid):
    return lambda communities: find(communities, jid) is None


def linked(jid, group_jids, present=True):
    """Every one of *group_jids* is (or, with present=False, none is) a group
    of the community."""
    def achieved(communities):
        community = find(communities, jid)
        if community is None:
            return False
        current = {group.jid for group in community.subgroups}
        return current.issuperset(group_jids) if present else current.isdisjoint(group_jids)
    return achieved


def admin(jid, person_key, key, present=True):
    """The person is (or is no longer) an admin. *key* maps a JID to the same
    value for every form of one person (@lid, @c.us, 8 or 9 digits)."""
    def achieved(communities):
        community = find(communities, jid)
        if community is None:
            return False
        return (person_key in {key(member.jid) for member in community.admins}) is present
    return achieved


def linkable_groups(chats, communities, is_locked=lambda jid: False):
    """(jid, name) of the groups that can be offered for linking, by name:
    the account's named groups that are not a community nor in one already.
    Whether the account is an admin of each is WhatsApp's to decide. A locked
    group is never named (MainWindow.is_chat_locked), as in every other picker."""
    taken = {community.jid for community in communities}
    for community in communities:
        taken.update(group.jid for group in community.subgroups)
    rows = []
    for jid, chat in chats.items():
        if not group_jid(jid) or jid in taken or not isinstance(chat, dict) or is_locked(jid):
            continue
        name = _name(chat.get("name"))
        if name:
            rows.append((jid, name))
    return sorted(rows, key=lambda row: (row[1].casefold(), row[0]))
