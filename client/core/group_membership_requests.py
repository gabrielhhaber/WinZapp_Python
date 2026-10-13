"""Pending requests to join a group, and the admin's answer to them.

The routes are WinZapp's own over the library's functions (see
groupMembershipRequestsController.ts). An answer of approve or reject only
says the request was not refused: it is believed when the pending list, asked
of the server again, no longer has the person. See settle().
"""

import re
from dataclasses import dataclass
from core.api_client import api_get, api_post
from core.group_operations import group_jid
from core.read_back import run_action

_PERSON = re.compile(r"\d+@(?:c\.us|lid)")
_IDENTIFIER = re.compile(r"\d+(?:-\d+)?(?::\d+)?@(?:g\.us|lid|c\.us|s\.whatsapp\.net)\b")
METHODS = ("InviteLink", "LinkedGroupJoin", "NonAdminAdd")


class NotAdmin(Exception):
    """WhatsApp refused the list: the account does not administer the group."""


@dataclass(frozen=True)
class MembershipRequest:
    jid: str
    added_by: str   # who put the request in; the requester for a link
    phone: str      # the number behind a @lid, when the page knows it
    name: str       # the profile or saved name the page knows, if any
    method: str     # one of METHODS, or "" when WhatsApp sent something else
    time: int | None


@dataclass(frozen=True)
class Pending:
    requests: tuple
    refreshed: bool  # False: the server could not be asked, the list may be old


def _person(value):
    return value if isinstance(value, str) and _PERSON.fullmatch(value) else ""


def parse_requests(body):
    data = body.get("response") if isinstance(body, dict) else None
    rows = data.get("requests") if isinstance(data, dict) else None
    if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(rows, list):
        raise ValueError("membership_requests_unavailable")
    requests = []
    for row in rows:
        jid = _person(row.get("id")) if isinstance(row, dict) else ""
        if not jid:
            continue
        name = row.get("name")
        # The provider's raw identifier must never become a display name.
        name = name.strip() if isinstance(name, str) and not _IDENTIFIER.search(name) else ""
        when = row.get("t")
        requests.append(MembershipRequest(
            jid, _person(row.get("addedBy")), _person(row.get("phone")), name,
            row.get("method") if row.get("method") in METHODS else "",
            when if type(when) is int and when > 0 else None))
    return Pending(tuple(requests), data.get("refreshed") is True)


def _url(connection, group, suffix=""):
    server, port, token = connection
    group = group_jid(group)
    if not group:
        raise ValueError("invalid_group")
    return (f"{server}:{port}/api/{token}/group-membership-requests/{group}{suffix}",
            {"Authorization": f"Bearer {token}"})


def list_requests(connection, group):
    url, headers = _url(connection, group)
    response = api_get(url, headers=headers, timeout=30)
    if response.status_code == 403:
        raise NotAdmin()
    if response.status_code != 200:
        raise ValueError("membership_requests_unavailable")
    return parse_requests(response.json())


def answer_requests(connection, group, jids, approve):
    """Send the answer once. Returning means "not refused", nothing more."""
    jids = list(jids)
    if not jids or len(jids) > 50 or not all(_person(jid) for jid in jids):
        raise ValueError("invalid_participant")
    url, headers = _url(connection, group, "/approve" if approve else "/reject")
    response = api_post(url, json={"participants": jids}, headers=headers, timeout=60)
    body = response.json()
    if response.status_code != 200 or not isinstance(body, dict) or body.get("status") != "success":
        raise ValueError("membership_action_refused")


def answered(jids):
    """No one of *jids* is pending any more, on a list the server confirmed:
    a list that could not be refreshed proves nothing."""
    jids = frozenset(jids)
    return lambda pending: pending.refreshed and jids.isdisjoint(request.jid for request in pending.requests)


def settle(connection, group, jids, approve, **options):
    """Answer the requests and read the pending list back.
    Returns (state, Pending or None), as core.read_back.run_action()."""
    jids = list(jids)
    return run_action(lambda: answer_requests(connection, group, jids, approve),
                      lambda: list_requests(connection, group), answered(jids), **options)
