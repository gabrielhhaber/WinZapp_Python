"""The channels window (Alt+9): the account's channels, the channel directory,
and what can be done with a channel.

Both engines answer the same routes with WhatsApp's own XWA2Newsletter nodes
(client/api_patches/src/controller/channelsPanelController.ts,
neonize_bridge/channels_panel.py): `channels/list`, `channels/directory`,
`channels/info`, `channels/follow`, `channels/unfollow`, `channels/mute`, and
`channels/pin` (an admin pinning an update, by its server id), and the
admins: `channels/admins` (how many), `channels/followers` (the people with
their roles), `channels/admin-pending` (the owner's pending invites),
`channels/admin-invite` (an invite sent to the person's chat),
`channels/admin-revoke` (the owner taking it back), `channels/admin-demote`
(removing an admin, or leaving as one) and `channels/owner-change`.
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
INVITE_MESSAGE_LIMIT = 500
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
    reactions: str = ""       # ALL, BASIC or NONE (REACTION_SETTINGS), "" when unknown
    admin_profiles: bool | None = None  # "show admin profile", None when unknown

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


@dataclass(frozen=True)
class Member:
    """One person of a channel as its admins window lists them."""
    jid: str          # their @lid, or their number's JID
    phone: str        # their number's JID when the answer gave it, else ""
    name: str         # the name WhatsApp gave, possibly "" (never shown raw)
    role: str         # owner, admin, pending (an admin invite awaits) or subscriber


_ORDER = {"owner": 0, "admin": 1, "pending": 2}


def parse_followers(body):
    """The people of a `channels/followers` answer; ValueError when it is not one."""
    data = body.get("response") if isinstance(body, dict) else None
    found = data.get("followers") if isinstance(data, dict) else None
    if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(found, list):
        raise ValueError("channel_followers_unavailable")
    out = []
    for item in found:
        if not isinstance(item, dict):
            continue
        jid, phone = str(item.get("id") or ""), str(item.get("phone") or "")
        if not admin_target(jid):
            continue
        role = str(item.get("role") or "").lower()
        out.append(Member(jid, phone if admin_target(phone) else "",
                          str(item.get("name") or "").strip(),
                          role if role in ("owner", "admin") else "subscriber"))
    return out


def parse_pending(body):
    """The JIDs with a pending admin invite; ValueError when it is not an answer."""
    data = body.get("response") if isinstance(body, dict) else None
    found = data.get("pending") if isinstance(data, dict) else None
    if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(found, list):
        raise ValueError("channel_admins_unavailable")
    # WPPConnect may answer a number as @c.us, the followers as @s.whatsapp.net
    return [jid.replace("@c.us", "@s.whatsapp.net") for jid in found
            if isinstance(jid, str) and admin_target(jid)]


def members(followers, pending=(), me=None):
    """The window's rows: owner, admins, pending invites, then followers. A
    follower with a pending invite shows as pending; an invitee who does not
    follow is added. *me* (a Member) is added when the list leaves this
    account out, as a new channel's does."""
    pending = list(pending or ())
    rows = [Member(m.jid, m.phone, m.name, "pending") if m.jid in pending and m.role == "subscriber"
            else m for m in followers or ()]
    listed = {m.jid for m in rows} | {m.phone for m in rows if m.phone}
    rows += [Member(jid, "", "", "pending") for jid in pending if jid not in listed]
    if me is not None and me.jid not in listed and (not me.phone or me.phone not in listed):
        rows.append(me)
    return sorted(rows, key=lambda m: _ORDER.get(m.role, 3))


def member_name(known, server_name, unknown) -> str:
    """How a person is said: the name WinZapp knows them by, else the one
    WhatsApp gave, else *unknown*; never a JID."""
    for name in (known, server_name):
        name = str(name or "").strip()
        if name and "@" not in name:
            return name
    return unknown


def admin_target(jid, phone_to_lid=None) -> str:
    """The form a person is invited (or uninvited) by: their @lid when known,
    as WhatsApp names an admin, else their number as '<digits>@c.us'; '' for
    anything that is not a person."""
    jid = str(jid or "").strip()
    user, _, server = jid.partition("@")
    if not user.isdigit():
        return ""
    if server == "lid":
        return jid
    if server not in ("s.whatsapp.net", "c.us"):
        return ""
    phone = f"{user}@s.whatsapp.net"
    lid = (phone_to_lid or {}).get(phone) or (phone_to_lid or {}).get(f"{user}@c.us") or ""
    return lid if lid.endswith("@lid") else f"{user}@c.us"


def server_id_of(message) -> int:
    """A channel update's server id (what pinning names), or 0."""
    value = message.get("serverId") if isinstance(message, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


#: what WhatsApp lets a channel's admins post besides text, as WinZapp posts it
#: (wa-js accepts "chat", "image", "video" and "poll_creation" for a channel)
CHANNEL_POST_KINDS = ("photo_video", "poll")
#: an attachment's media_type that is a photo or a video
CHANNEL_MEDIA_TYPES = ("image", "video")


def channel_post_kinds(capabilities):
    """The kinds the engine posts in a channel, from /send-capabilities.

    WPPConnect does not state them: wa-js posts photos, videos and polls to a
    channel through WhatsApp Web's own newsletter job, so a missing field is
    all of them. The Neonize bridge states `channelMedia` / `channelPolls`.
    None when nothing is known yet (the probe has not answered).
    """
    if not isinstance(capabilities, dict):
        return None
    if "channelMedia" not in capabilities and "channelPolls" not in capabilities:
        return set(CHANNEL_POST_KINDS)
    kinds = set()
    if capabilities.get("channelMedia") is True:
        kinds.add("photo_video")
    if capabilities.get("channelPolls") is True:
        kinds.add("poll")
    return kinds


def older_page_query(jid, oldest_msg) -> str:
    """What a channel's `before` page of get-messages adds to its query: the
    anchor's server id, so the Neonize bridge finds it even when it no longer
    remembers the id (it remembers ids only while it runs). WPPConnect ignores
    it. Empty for any other chat or an update with no server id."""
    if not str(jid or "").endswith("@newsletter"):
        return ""
    server_id = server_id_of(oldest_msg)
    return f"&serverId={server_id}" if server_id else ""


def history_ends_on_server(jid) -> bool:
    """True for a channel: its updates are on WhatsApp's server, which the
    page (WPPConnect) or the bridge (Neonize) has just read back to the
    channel's start, so an empty older page is the end. The phone has no
    channel history to send, and asking it (a peer-data request) only shows
    up on the phone."""
    return str(jid or "").endswith("@newsletter")


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


#: who may react to a channel's updates, as WhatsApp Web's channel settings
#: offer it (NewsletterReactionCodesSetting All, Basic, None; its Blocklist
#: is no choice of the settings screen)
REACTION_SETTINGS = ("ALL", "BASIC", "NONE")


def reaction_setting(thread) -> str:
    """thread_metadata.settings.reaction_codes.value, "" when absent or unknown."""
    settings = thread.get("settings") if isinstance(thread, dict) else None
    codes = settings.get("reaction_codes") if isinstance(settings, dict) else None
    value = str(codes.get("value") or "").upper() if isinstance(codes, dict) else ""
    return value if value in REACTION_SETTINGS else ""


def admin_profiles_setting(thread):
    """thread_metadata.settings.admin_profiles_enabled, None when absent (the
    channel queries do not carry it; WPPConnect adds what its page knows)."""
    settings = thread.get("settings") if isinstance(thread, dict) else None
    value = settings.get("admin_profiles_enabled") if isinstance(settings, dict) else None
    return value if isinstance(value, bool) else None


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
        created=_int(thread.get("creation_time")) or 0, pinned=pinned_ids(thread),
        reactions=reaction_setting(thread), admin_profiles=admin_profiles_setting(thread))


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


def admin_count(connection, jid):
    """How many admins the channel has, or None when the answer did not say."""
    body = _post(connection, "channels/admins", {"id": jid}, "channel_admins_unavailable")
    data = body.get("response") if isinstance(body, dict) else None
    count = data.get("adminCount") if isinstance(data, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None


def invite_admin(connection, jid, user, name="", message="") -> int:
    """Invite *user* (an admin_target) to administer the channel: WhatsApp
    sends them the invite in their chat. The invite's expiration (Unix
    seconds), 0 when the answer did not say."""
    body = _post(connection, "channels/admin-invite",
                 {"id": jid, "user": user, "name": str(name or "")[:NAME_LIMIT],
                  "message": str(message or "").strip()[:INVITE_MESSAGE_LIMIT]},
                 "channel_admin_invite_failed")
    if not isinstance(body, dict) or body.get("status") != "success":
        raise ValueError("channel_admin_invite_failed")
    data = body.get("response") if isinstance(body.get("response"), dict) else {}
    return _int(data.get("expiration")) or 0


def followers(connection, jid):
    return parse_followers(_post(connection, "channels/followers", {"id": jid},
                                 "channel_followers_unavailable"))


def pending_admins(connection, jid):
    return parse_pending(_post(connection, "channels/admin-pending", {"id": jid},
                               "channel_admins_unavailable"))


def _person_action(connection, route, jid, user, code):
    body = _post(connection, route, {"id": jid, "user": user}, code)
    if not isinstance(body, dict) or body.get("status") != "success":
        raise ValueError(code)


def demote_admin(connection, jid, user):
    """Remove *user* as an admin (the owner), or stop being one (yourself)."""
    _person_action(connection, "channels/admin-demote", jid, user, "channel_admin_demote_failed")


def change_owner(connection, jid, user):
    """Make *user*, an admin, the channel's owner."""
    _person_action(connection, "channels/owner-change", jid, user, "channel_owner_change_failed")


def revoke_admin_invite(connection, jid, user):
    _person_action(connection, "channels/admin-revoke", jid, user, "channel_admin_revoke_failed")


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


def set_channel_reactions(connection, jid, value):
    """Who may react to the channel's updates (REACTION_SETTINGS)."""
    if value not in REACTION_SETTINGS:
        raise ValueError("channel_reactions_failed")
    _post(connection, "channels/reactions", {"id": jid, "value": value},
          "channel_reactions_failed", timeout=60)


def set_channel_admin_profiles(connection, jid, enabled):
    """WhatsApp Web's "Show admin profile": each admin's own name and photo
    on the updates they share."""
    _post(connection, "channels/admin-profiles", {"id": jid, "enabled": bool(enabled)},
          "channel_admin_profiles_failed", timeout=60)


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


def member_actions(member, my_role, is_me=False):
    """What the admins window offers for the selected *member*, as WhatsApp
    Web does: the owner makes a follower an admin, cancels a pending invite,
    removes an admin or hands them the channel; an admin may leave. The owner
    cannot leave without handing the channel over first."""
    if member is None:
        return set()
    if my_role == _OWNER and not is_me:
        return {"subscriber": {"make_admin"}, "pending": {"revoke"},
                "admin": {"demote", "make_owner"}}.get(member.role, set())
    if my_role == _ADMIN and is_me:
        return {"leave"}
    return set()
