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
    # A group of the community the account is not in (WhatsApp lists those
    # once asked: refresh_community()), and whether joining it needs an
    # admin's approval. An older API sends neither: everything it lists is joined.
    joined: bool = True
    approval: bool = False
    # A request to join sent from this account that no admin has answered.
    requested: bool = False


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
                     group.get("announcement") is True, group.get("general") is True,
                     group.get("joined") is not False, group.get("approval") is True,
                     group.get("requested") is True)
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


def refresh_community(connection, jid):
    """*jid*'s community after asking WhatsApp for the groups the account is
    not in, as WhatsApp Web does when a community is opened (a read); None
    when it could not be read."""
    url, headers = _url(connection, f"community-subgroups/{jid}")
    response = api_get(url, headers=headers, timeout=20)
    if response.status_code != 200:
        return None
    return find(parse_communities(response.json()), jid)


#: What a join or a cancel can end in (communityJoinController.ts).
JOINED, REQUESTED, REQUEST_PENDING, CANCELLED = "joined", "requested", "request_pending", "cancelled"
_MEMBERSHIP_STATES = (JOINED, REQUESTED, REQUEST_PENDING, CANCELLED)


def _membership(connection, community_jid, subgroup_jid, action):
    """One join or cancel, never retried: a request that timed out may have
    reached WhatsApp. The state it ended in, or ValueError with the server's
    code ("unavailable" when there was none)."""
    url, headers = _url(connection, f"community-subgroups/{community_jid}/{subgroup_jid}/{action}")
    try:
        response = api_post(url, json={}, headers=headers, timeout=60)
        body = response.json()
    except Exception:
        raise ValueError("unavailable") from None
    state = (body.get("response") or {}).get("state") if isinstance(body, dict) else None
    if response.status_code == 200 and state in _MEMBERSHIP_STATES:
        return state
    code = body.get("code") if isinstance(body, dict) else None
    raise ValueError(code if isinstance(code, str) and code else "unavailable")


def join_subgroup(connection, community_jid, subgroup_jid):
    """Join the group, or ask to when an admin has to approve: JOINED,
    REQUESTED or REQUEST_PENDING."""
    return _membership(connection, community_jid, subgroup_jid, "join")


def cancel_join_request(connection, community_jid, subgroup_jid):
    """Withdraw a request to join: CANCELLED."""
    return _membership(connection, community_jid, subgroup_jid, "cancel-request")


def join_action(group):
    """What the join button offers on *group*, as WhatsApp Web's own button
    does (WAWebGroupJoinModalUtils.getGroupInviteActionText): "cancel" a
    request already sent, "request" when an admin approves who joins, "join"
    otherwise; None on a group the account is already in."""
    if group is None or group.joined:
        return None
    if group.requested:
        return "cancel"
    return "request" if group.approval else "join"


SUBGROUP_FILTERS = ("all", "joined", "not_joined")


def filter_subgroups(community, which):
    """The groups of *community* the dialog's filter shows: every one, the
    ones the account is in, or the ones it is not in."""
    groups = community.subgroups if community else ()
    if which == "joined":
        return tuple(group for group in groups if group.joined)
    if which == "not_joined":
        return tuple(group for group in groups if not group.joined)
    return tuple(groups)


def empty_groups_key(community, which):
    """What Enter on *community* says when the filter *which* leaves no
    group to move to. Only "groups I'm not in" can be empty for the
    account being in every one; otherwise it has joined none."""
    if which == "not_joined" and community is not None and community.subgroups:
        return "communities_in_every_group"
    return "communities_no_groups"


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


def most_recent_group(community, last_activity):
    """The group of *community* with the newest activity, or None when none
    of them has any. *last_activity(jid)* is 0 for a group with nothing on
    file; on a tie the group listed first wins. A group the account is not
    in is never the one: one it left keeps its conversation on file, and it
    cannot be opened."""
    best, best_ts = None, 0
    for group in community.subgroups if community else ():
        if not group.joined:
            continue
        ts = last_activity(group.jid)
        if ts > best_ts:
            best, best_ts = group, ts
    return best


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


def is_community_chat(chat, known_jids=()):
    """Is *chat* (a chat-list entry) a community itself, not one of its groups?

    WhatsApp Web marks the community's own chat groupMetadata.isParentGroup;
    *known_jids* (the communities already read) covers an entry that arrived
    without its metadata."""
    if not isinstance(chat, dict):
        return False
    meta = chat.get("groupMetadata")
    if isinstance(meta, dict) and meta.get("isParentGroup") is True:
        return True
    jid = chat.get("remoteJid") or chat.get("id")
    if isinstance(jid, dict):
        jid = jid.get("_serialized")
    return isinstance(jid, str) and jid.endswith("@g.us") and jid in set(known_jids)
