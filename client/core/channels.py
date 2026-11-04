"""The channels window (Alt+9): the account's channels, the channel directory,
and what can be done with a channel.

Both engines answer the same routes with WhatsApp's own XWA2Newsletter nodes
(client/api_patches/src/controller/channelsPanelController.ts,
neonize_bridge/channels_panel.py): `channels/list`, `channels/directory`,
`channels/info`, `channels/follow`, `channels/unfollow`, `channels/mute`, and
`channels/pin` (an admin pinning an update, by its server id).
Creating, editing and deleting a channel are the upstream newsletter routes
(`POST newsletter`, `PUT` / `DELETE newsletter/:id`), which the bridge answers
the same way.

Nothing here retries an action, and nothing returns or logs a provider's raw
error: a channel's name and id stay out of log.log. Pure parsing at the top
(tested in tests/test_channels.py).
"""

import re
import time
from dataclasses import dataclass

from core.api_client import api_post, api_request

NAME_LIMIT = 100
DESCRIPTION_LIMIT = 2048
INVITE_PREFIX = "https://whatsapp.com/channel/"
_CHANNEL = re.compile(r"\d+@newsletter")
_INVITE = re.compile(r"(?:whatsapp\.com/channel/)?([A-Za-z0-9_-]{10,64})/?$")
#: viewer roles that may edit or delete the channel
_OWNER, _ADMIN = "owner", "admin"
_GONE_STATES = ("DELETED", "NON_EXISTING")
#: the directory's categories and list views, in the order WhatsApp Web shows
#: them; both engines take these names (the seasonal "special events" ones
#: are left out)
CATEGORIES = ("BUSINESS", "ENTERTAINMENT", "LIFESTYLE", "NEWS", "ORGANIZATIONS", "PEOPLE",
              "SPORTS")
VIEWS = ("RECOMMENDED", "TRENDING", "POPULAR", "NEW")


@dataclass(frozen=True)
class Channel:
    jid: str
    name: str
    description: str
    followers: int | None     # None when the answer did not say
    verified: bool
    role: str                 # owner, admin, subscriber, guest, or "" when unknown
    muted: bool | None        # None when the answer did not say
    invite: str
    created: int              # Unix seconds, 0 when unknown
    pinned: tuple = ()        # server ids of the pinned updates, when the answer said

    @property
    def following(self) -> bool:
        return self.role in (_OWNER, _ADMIN, "subscriber")

    @property
    def owner(self) -> bool:
        return self.role == _OWNER

    @property
    def admin(self) -> bool:
        return self.role in (_OWNER, _ADMIN)

    @property
    def link(self) -> str:
        return INVITE_PREFIX + self.invite if self.invite else ""


def _text(value):
    if isinstance(value, dict):
        value = value.get("text")
    return value.strip() if isinstance(value, str) else ""


def _int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def admin_role(role) -> bool:
    """Whether *role* may post and pin in the channel (owner or admin)."""
    return role in (_OWNER, _ADMIN)


def server_id_of(message) -> int:
    """A channel update's server id (what pinning names), or 0."""
    value = message.get("serverId") if isinstance(message, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def pinned_ids(thread, now=None):
    """The server ids of a channel's pinned updates (thread_metadata's
    pinned_messages), leaving out a pin whose time has run out."""
    now = time.time() if now is None else now
    found = thread.get("pinned_messages") if isinstance(thread, dict) else None
    out = []
    for pin in found if isinstance(found, list) else ():
        if not isinstance(pin, dict):
            continue
        server_id, expiry = _int(pin.get("message_id")), _int(pin.get("expiry_ts"))
        if server_id and (not expiry or expiry > now) and server_id not in out:
            out.append(server_id)
    return tuple(out)


def pinned_messages(records, pinned):
    """Copies of the loaded *records* whose server id is pinned, marked as
    such: the channel's entry of the pinned-messages overview."""
    wanted = set(pinned or ())
    return [dict(m, pinInChat=True) for m in records or () if server_id_of(m) in wanted]


def parse_channel(node, *, default_role=""):
    """A Channel from one XWA2Newsletter node, or None for anything that is
    not a channel, or one WhatsApp says is gone."""
    if not isinstance(node, dict):
        return None
    jid = node.get("id")
    if not isinstance(jid, str) or not _CHANNEL.fullmatch(jid):
        return None
    state = node.get("state")
    if isinstance(state, dict) and str(state.get("type") or "").upper() in _GONE_STATES:
        return None
    thread = node.get("thread_metadata") if isinstance(node.get("thread_metadata"), dict) else {}
    viewer = node.get("viewer_metadata") if isinstance(node.get("viewer_metadata"), dict) else {}
    role = str(viewer.get("role") or default_role or "").strip().lower()
    mute = str(viewer.get("mute") or "").strip().upper()
    invite = thread.get("invite")
    return Channel(
        jid=jid, name=_text(thread.get("name")), description=_text(thread.get("description")),
        followers=_int(thread.get("subscribers_count")),
        verified=str(thread.get("verification") or "").upper() == "VERIFIED",
        role=role, muted=True if mute == "ON" else False if mute == "OFF" else None,
        invite=invite.strip() if isinstance(invite, str) else "",
        created=_int(thread.get("creation_time")) or 0, pinned=pinned_ids(thread))


def parse_channels(body, *, default_role=""):
    """The channels of a list or directory answer; ValueError when it is not one."""
    data = body.get("response") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(data, list):
        raise ValueError("channels_unavailable")
    seen, out = set(), []
    for node in data:
        channel = parse_channel(node, default_role=default_role)
        if channel is not None and channel.jid not in seen:
            seen.add(channel.jid)
            out.append(channel)
    return out


def invite_code(text) -> str:
    """The invite code of a channel link ('https://whatsapp.com/channel/<code>'
    or the code itself), or ''. Another link (a group's chat.whatsapp.com)
    is not a channel's."""
    text = str(text or "").strip()
    if "/" in text.rstrip("/") and "whatsapp.com/channel/" not in text:
        return ""
    match = _INVITE.search(text)
    return match.group(1) if match else ""


def sorted_channels(channels):
    """By name (case-insensitive), then id: the order the list shows."""
    return sorted(channels, key=lambda c: (c.name.casefold(), c.jid))


# ── requests ────────────────────────────────────────────────────────────────


def _url(connection, endpoint):
    server, port, token = connection
    return f"{server}:{port}/api/{token}/{endpoint}", {"Authorization": f"Bearer {token}"}


def _post(connection, endpoint, payload, code, timeout=30):
    url, headers = _url(connection, endpoint)
    response = api_post(url, json=payload, headers=headers, timeout=timeout)
    if response.status_code not in (200, 201):
        raise ValueError(code)
    try:
        return response.json()
    except ValueError as error:
        raise ValueError(code) from error


def list_channels(connection):
    return parse_channels(_post(connection, "channels/list", {}, "channels_unavailable"))


def directory_body(query="", limit=30, category="", view=""):
    """The `channels/directory` body: the search text, and the category and
    list view only when they narrow anything (all categories and the
    recommended list are what an empty body already reads)."""
    body = {"query": str(query or "")[:100], "limit": int(limit)}
    if category in CATEGORIES:
        body["category"] = category
    if view in VIEWS and view != VIEWS[0]:
        body["view"] = view
    return body


def directory_announcement(query="", category="", view=""):
    """The key that says how many channels a directory read gave: what
    WhatsApp recommends only when nothing narrowed the list."""
    if query or category in CATEGORIES or (view in VIEWS and view != VIEWS[0]):
        return "channels_found"
    return "channels_recommended"


def directory(connection, query="", limit=30, category="", view=""):
    """Channels found for *query*, or the directory's list in *view*, narrowed
    to *category* when one is given. Nobody's role there is known: a channel
    already followed is shown as such by the caller, from the followed list."""
    return parse_channels(_post(connection, "channels/directory",
                                directory_body(query, limit, category, view),
                                "channels_unavailable"), default_role="guest")


def channel_info(connection, *, jid="", invite=""):
    body = _post(connection, "channels/info", {"id": jid} if jid else {"invite": invite},
                 "channel_not_found")
    data = body.get("response") if isinstance(body, dict) else None
    channel = parse_channel(data, default_role="guest")
    if channel is None:
        raise ValueError("channel_not_found")
    return channel


def follow(connection, jid):
    _post(connection, "channels/follow", {"id": jid}, "channel_follow_failed")


def unfollow(connection, jid):
    _post(connection, "channels/unfollow", {"id": jid}, "channel_unfollow_failed")


def pin_update(connection, jid, server_id, pin):
    """Pin or unpin one update of a channel this account administers."""
    body = _post(connection, "channels/pin", {"id": jid, "serverId": int(server_id), "pin": bool(pin)},
                 "channel_pin_failed")
    if not isinstance(body, dict) or body.get("status") != "success":
        raise ValueError("channel_pin_failed")


def set_muted(connection, jid, muted):
    _post(connection, "channels/mute", {"id": jid, "mute": bool(muted)}, "channel_mute_failed")


def create_channel(connection, name, description):
    """The id of the channel just created, or '' when the answer did not say."""
    body = _post(connection, "newsletter",
                 {"name": name[:NAME_LIMIT], "options": {"description": description[:DESCRIPTION_LIMIT]}},
                 "channel_create_failed", timeout=60)
    data = body.get("response", body) if isinstance(body, dict) else {}
    jid = (data.get("idJid") or data.get("id")) if isinstance(data, dict) else ""
    return jid if isinstance(jid, str) and _CHANNEL.fullmatch(jid) else ""


def edit_channel(connection, jid, name, description):
    url, headers = _url(connection, f"newsletter/{jid}")
    response = api_request("PUT", url, json={"name": name[:NAME_LIMIT],
                                             "description": description[:DESCRIPTION_LIMIT]},
                           headers=headers, timeout=60)
    if response.status_code not in (200, 201):
        raise ValueError("channel_edit_failed")


def delete_channel(connection, jid):
    url, headers = _url(connection, f"newsletter/{jid}")
    response = api_request("DELETE", url, headers=headers, timeout=60)
    if response.status_code not in (200, 201):
        raise ValueError("channel_delete_failed")
    try:
        body = response.json()
    except ValueError:
        return
    if body is False or (isinstance(body, dict) and body.get("status") == "error"):
        raise ValueError("channel_delete_failed")
