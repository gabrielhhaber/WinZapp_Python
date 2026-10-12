"""Interpret provider results without turning transport success into membership."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GroupCreation:
    jid: str
    members_confirmed: bool


def group_jid(value):
    if isinstance(value, dict):
        value = value.get("_serialized") or value.get("user")
    if not isinstance(value, str):
        return ""
    if re.fullmatch(r"\d+(?:-\d+)?", value):
        value += "@g.us"
    return value if re.fullmatch(r"\d+(?:-\d+)?@g\.us", value) else ""


def created_group_jid(body):
    if not isinstance(body, dict) or str(body.get("status", "success")).lower() != "success":
        return ""
    response = body.get("response")
    if not isinstance(response, dict):
        return ""
    info = response.get("groupInfo")
    if isinstance(info, list) and len(info) == 1 and isinstance(info[0], dict):
        return group_jid(info[0].get("id"))
    return group_jid(response.get("gid"))


def created_group(body, requested):
    jid = created_group_jid(body)
    if not jid:
        return None
    response = body["response"]
    info = response.get("groupInfo")
    participants = info[0].get("participants") if isinstance(info, list) and len(info) == 1 else response.get("participants")
    outcomes = member_outcomes({"response": {"result": [participants]}}, requested)
    return GroupCreation(jid, bool(outcomes) and all(state == "added" for _, state in outcomes))


def member_outcomes(body, requested):
    """Return (jid, state) in request order; missing/conflicting results are unknown.

    An invitation code is not a delivery receipt. 421 is pending approval.
    Match PN suffixes, never infer an unmapped LID/PN relationship.
    """
    def canonical(jid):
        return jid.replace("@s.whatsapp.net", "@c.us") if isinstance(jid, str) else ""

    found = {}
    response = body.get("response") if isinstance(body, dict) else None
    groups = response.get("result") if isinstance(response, dict) else None
    if isinstance(groups, list) and str(body.get("status", "success")).lower() == "success":
        for group in groups:
            if not isinstance(group, dict):
                continue
            for jid, info in group.items():
                key = canonical(jid)
                code = info.get("code") if isinstance(info, dict) else None
                code = str(code) if type(code) in (int, str) else ""
                state = {"200": "added", "409": "added", "421": "pending"}.get(code)
                if state is None:
                    state = "invitable" if code == "403" and info.get("invite_code") else (
                        "failed" if code.isdigit() else "unknown")
                found[key] = state if key not in found or found[key] == state else "unknown"
    return [(jid, found.get(canonical(jid), "unknown")) for jid in requested]


def group_permissions(is_admin, is_member, metadata):
    """Known restrictions win; unknown edit permission never grants admin rights.

    For legacy servers, adding remains available for members and is checked
    by the provider, matching the existing Overview behavior.
    """
    edit = bool(is_admin or (is_member and metadata.get("restrict") is False))
    mode = metadata.get("memberAddMode")
    add = bool(is_admin or (is_member and mode != "admin_add"))
    return edit, add


def invitation_link(value):
    if not isinstance(value, str):
        return ""
    return value if re.fullmatch(r"https://chat\.whatsapp\.com/[A-Za-z0-9]{10,64}", value) else ""
