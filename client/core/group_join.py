"""Invitation preview and explicit join; neither operation retries or persists codes."""

import re
from dataclasses import dataclass
from core.api_client import api_post
from core.group_operations import group_jid


@dataclass(frozen=True)
class GroupPreview:
    jid: str
    name: str
    description: str
    size: int | None
    approval: bool | None


# Re-exported: the callers of this module have always taken it from here.
from core.group_invitation_link import invitation_code  # noqa: F401


def _response(connection, endpoint, payload, accepted):
    server, port, token = connection
    response = api_post(f"{server}:{port}/api/{token}/{endpoint}", json=payload,
                        headers={"Authorization": f"Bearer {token}"}, timeout=20)
    body = response.json()
    data = body.get("response") if isinstance(body, dict) else None
    if (response.status_code not in accepted or not isinstance(body, dict)
            or body.get("status") != "success" or not isinstance(data, dict)):
        raise ValueError("group_invite_unconfirmed")
    return data


def preview_group(connection, code):
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{10,64}", code):
        raise ValueError("invalid_invite")
    data = _response(connection, "group-info-from-invite-link", {"invitecode": code}, (200,))
    jid = group_jid(data.get("id"))
    name = data.get("subject")
    if not jid or not isinstance(name, str) or not name.strip():
        raise ValueError("group_preview_unconfirmed")
    # The provider's raw identifier must never become the group's display name.
    if re.search(r"\d+(?:-\d+)?(?::\d+)?@(?:g\.us|lid|c\.us|s\.whatsapp\.net)\b", name):
        name = ""
    size = data.get("size")
    approval = data.get("membershipApprovalMode")
    return GroupPreview(jid, name, data.get("desc") if isinstance(data.get("desc"), str) else "",
                        size if type(size) is int and size >= 0 else None,
                        approval if type(approval) is bool else None)


def join_group(connection, code, expected_jid):
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{10,64}", code) or not group_jid(expected_jid):
        raise ValueError("invalid_invite")
    data = _response(connection, "join-code", {"inviteCode": code}, (200, 201))
    if group_jid(data.get("id")) != expected_jid or type(data.get("pendingApproval")) is not bool:
        raise ValueError("group_join_unconfirmed")
    return data["pendingApproval"]
