"""WinZapp's channels window (Alt+9): `channels/list`, `channels/directory`,
`channels/info`, `channels/follow`, `channels/unfollow` and `channels/mute`,
an admin pinning an update in the channel's conversation (`channels/pin`), and
the channel's people: how many admins (`channels/admins`), its followers with
their roles (`channels/followers`), the owner's pending admin invites
(`channels/admin-pending`), inviting an admin (`channels/admin-invite`), taking
an invite back (`channels/admin-revoke`), removing an admin or leaving as one
(`channels/admin-demote`) and handing the channel to an admin
(`channels/owner-change`).

The WPPConnect side calls WhatsApp Web's own MEX jobs for each read
(client/api_patches/src/controller/channelsPanelController.ts); the bridge
sends the same queries (`send_mex`, neonize patch 0005) and answers the same
XWA2Newsletter nodes, which the client reads once (client/core/channels.py).
The query ids were read over CDP on 2026-11-04:

- 25399611239711790, the followed channels (`xwa2_newsletter_subscribed`);
- 26301059626252132, a directory search (`xwa2_newsletters_directory_search`);
- 25806748772361516, the recommended channels (`xwa2_newsletters_recommended`);
- 27456920720571478, one channel by id or invite code (`xwa2_newsletter`);
- 26125047313831973, the directory list by view and category
  (`xwa2_newsletters_directory_list`);
- 27165709459706559 and 28007176042216937, pinning and unpinning updates by
  server id (`xwa2_newsletter_pin_messages`, `xwa2_newsletter_unpin_messages`);
- 26278439461859188, the admin count (`xwa2_newsletter_admin`);
- 9387141988078609 and 9656078347839416, creating and revoking an admin invite
  for a user's LID (`xwa2_newsletter_admin_invite_create` /
  `_revoke`). The invite itself reaches the person as a
  newsletterAdminInviteMessage in their chat, as WhatsApp Web's
  sendNewsletterAdminInviteMessage sends it;
- 27472091235714801, the followers with their roles (`xwa2_newsletter_followers`);
- 28049623068067370, the owner's pending admin invites (`xwa2_newsletter_admin`,
  `pending_admin_invites`);
- 9880997548630971 and 9546742745432473, demoting an admin and changing the
  owner (`xwa2_newsletter_admin_demote`, `xwa2_newsletter_change_owner`).
These four were read with a channel's info open in WhatsApp Web, which loads
them only for its owner.

Following and unfollowing are neonize's own (`follow_newsletter`,
`unfollow_newsletter`), and so is muting both ways (`newsletter_toggle_mute`).
Creating, editing and deleting are the routes of channels.py and
channel_admin.py. Not yet run against a real account.

The pure functions are at the top (tested in test_channels_panel.py).
"""
import asyncio
import logging
import re

from aiohttp import web

import raw_queries as rq
from channels import SERVER, channel_id

log = logging.getLogger("bridge")

SUBSCRIBED_QUERY, SUBSCRIBED_FIELD = "25399611239711790", "xwa2_newsletter_subscribed"
SEARCH_QUERY, SEARCH_FIELD = "26301059626252132", "xwa2_newsletters_directory_search"
RECOMMENDED_QUERY, RECOMMENDED_FIELD = "25806748772361516", "xwa2_newsletters_recommended"
CHANNEL_QUERY, CHANNEL_FIELD = "27456920720571478", "xwa2_newsletter"
LIST_QUERY, LIST_FIELD = "26125047313831973", "xwa2_newsletters_directory_list"
PIN_QUERY, PIN_FIELD = "27165709459706559", "xwa2_newsletter_pin_messages"
UNPIN_QUERY, UNPIN_FIELD = "28007176042216937", "xwa2_newsletter_unpin_messages"
ADMIN_INFO_QUERY, ADMIN_INFO_FIELD = "26278439461859188", "xwa2_newsletter_admin"
INVITE_QUERY, INVITE_FIELD = "9387141988078609", "xwa2_newsletter_admin_invite_create"
REVOKE_QUERY = "9656078347839416"
FOLLOWERS_QUERY, FOLLOWERS_FIELD = "27472091235714801", "xwa2_newsletter_followers"
PENDING_QUERY = "28049623068067370"          # field ADMIN_INFO_FIELD
DEMOTE_QUERY = "9880997548630971"
OWNER_QUERY = "9546742745432473"
FOLLOWER_LIMIT = 500
ROLES = ("OWNER", "ADMIN", "SUBSCRIBER")
CAPTION_LIMIT = 500
_PERSON = re.compile(r"(\d{5,20})@(lid|s\.whatsapp\.net|c\.us)")

#: WhatsApp's directory categories (WAWebNewsletterDirectoryCategoryUtils; the
#: "special events" ones are seasonal and not offered) and list views
#: (WAWebNewsletterDirectoryFilterUtils.NewsletterDirectoryListView).
CATEGORIES = ("BUSINESS", "ENTERTAINMENT", "LIFESTYLE", "NEWS", "ORGANIZATIONS", "PEOPLE",
              "SPORTS")
VIEWS = ("RECOMMENDED", "TRENDING", "POPULAR", "NEW")

_INVITE = re.compile(r"(?:whatsapp\.com/channel/)?([A-Za-z0-9_-]{10,64})/?$")


def channel_key(body) -> str:
    """The channel id ('123@newsletter'), the code of an invite link, or ''."""
    body = body if isinstance(body, dict) else {}
    channel = channel_id(body.get("id")) if body.get("id") else ""
    if channel:
        return channel
    match = _INVITE.search(str(body.get("invite") or "").strip())
    return match.group(1) if match else ""


def directory_request(body):
    """(query id, variables, field) of a directory read: a search for the
    text, or the directory's list in a view (recommended, trending, popular,
    new), both narrowed to one category when one is given, as WhatsApp Web's
    directory does. An unknown category or view is ignored, never sent."""
    body = body if isinstance(body, dict) else {}
    query = str(body.get("query") or "").strip()[:100]
    try:
        limit = max(1, min(50, int(body.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    category = str(body.get("category") or "").upper()
    categories = [category] if category in CATEGORIES else []
    view = str(body.get("view") or "").upper()
    view = view if view in VIEWS else ""
    if query:
        return SEARCH_QUERY, {"input": {"search_text": query, "categories": categories,
                                        "limit": limit, "start_cursor": None},
                              "fetch_status_metadata": False}, SEARCH_FIELD
    if not categories and view in ("", "RECOMMENDED"):
        return RECOMMENDED_QUERY, {"input": {"limit": limit, "country_codes": []},
                                   "fetch_status_metadata": False}, RECOMMENDED_FIELD
    return LIST_QUERY, {"input": {"view": view or "RECOMMENDED",
                                  "filters": {"country_codes": [], "categories": categories},
                                  "limit": limit, "start_cursor": None},
                        "fetch_status_metadata": False}, LIST_FIELD


def channel_request(key):
    """Variables of the one-channel read, as WhatsApp Web's mexGetNewsletter
    sends them for a JID or an invite code."""
    by_id = key.endswith("@" + SERVER)
    return {"input": {"key": key, "type": "JID" if by_id else "INVITE", "view_role": "GUEST"},
            "fetch_viewer_metadata": True, "fetch_full_image": by_id, "fetch_creation_time": True,
            "fetch_wamo_sub": False, "fetch_status_metadata": False,
            "fetch_pinned_messages": True}       # as mexGetNewsletter always asks


def nodes_of(answer):
    """The newsletter nodes of a list or directory answer."""
    if isinstance(answer, list):
        found = answer
    elif isinstance(answer, dict):
        found = answer.get("result")
    else:
        found = None
    return [n for n in found if isinstance(n, dict)] if isinstance(found, list) else []


def pin_request(body):
    """(query id, variables, field, answer) of pinning or unpinning one update,
    as WhatsApp Web's pinNewsletterMessage sends it (the update's server id,
    as text), or None for a body that does not name one."""
    body = body if isinstance(body, dict) else {}
    channel = channel_id(body.get("id")) if body.get("id") else ""
    server_id = body.get("serverId")
    pin = body.get("pin")
    if (not channel or not isinstance(pin, bool) or isinstance(server_id, bool)
            or not isinstance(server_id, int) or server_id <= 0):
        return None
    query_id, field = (PIN_QUERY, PIN_FIELD) if pin else (UNPIN_QUERY, UNPIN_FIELD)
    variables = {"newsletter_id": channel, "input": {"message_ids": [str(server_id)]}}
    return query_id, variables, field, {"id": channel, "serverId": server_id, "pinned": pin}


def admin_count(answer):
    """The admin count of an admin-info answer, or None."""
    count = answer.get("admin_count") if isinstance(answer, dict) else None
    try:
        count = int(count)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def person(text) -> str:
    """A person's JID ('<digits>@lid' or '<digits>@s.whatsapp.net'), or ''."""
    match = _PERSON.fullmatch(str(text or "").strip())
    if not match:
        return ""
    return match.group(1) + ("@lid" if match.group(2) == "lid" else "@s.whatsapp.net")


def admin_invite_request(body):
    """(channel, person, name, caption) of an admin invite, or None."""
    body = body if isinstance(body, dict) else {}
    channel = channel_id(body.get("id")) if body.get("id") else ""
    user = person(body.get("user"))
    if not channel or not user:
        return None
    name = str(body.get("name") or "").strip()[:100]
    caption = str(body.get("message") or "").strip()[:CAPTION_LIMIT]
    return channel, user, name, caption


def invite_expiration(answer) -> int:
    """The invite's expiration (Unix seconds) of a create answer, or 0."""
    value = answer.get("invite_expiration_time") if isinstance(answer, dict) else None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def invite_fields(channel, name, caption, expiration) -> dict:
    """The newsletterAdminInviteMessage's fields, as WhatsApp Web fills them
    (WAWebGenerateNewsletterAdminInviteMessageProto)."""
    return {"newsletterJID": channel, "newsletterName": name, "caption": caption,
            "inviteExpiration": expiration}


def followers_of(answer):
    """[{id, phone, name, role}] of a followers answer: owner and admins
    first, as WhatsApp Web lists them; an edge without an id is skipped."""
    edges = (((answer or {}).get("followers") or {}).get("edges")
             if isinstance(answer, dict) else None)
    out = []
    for edge in edges if isinstance(edges, list) else ():
        node = edge.get("node") if isinstance(edge, dict) else None
        user = person((node or {}).get("id"))
        if not user:
            continue
        role = str(edge.get("role") or "").upper()
        out.append({"id": user, "phone": person(node.get("pn")),
                    "name": str(node.get("display_name") or "").strip()[:100],
                    "role": role.lower() if role in ROLES else "subscriber"})
    order = {"owner": 0, "admin": 1}
    return sorted(out, key=lambda f: order.get(f["role"], 2))


def pending_of(answer):
    """The people with a pending admin invite (their JIDs)."""
    found = answer.get("pending_admin_invites") if isinstance(answer, dict) else None
    out = []
    for invite in found if isinstance(found, list) else ():
        user = person(((invite or {}).get("user") or {}).get("id")) if isinstance(invite, dict) else ""
        if user and user not in out:
            out.append(user)
    return out


def register_routes(r, *, session, ok, jid_from_text):
    async def execute(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def error(code, status=503):
        # A static code only: a native error can carry channel names and ids.
        return web.json_response({"status": "error", "code": code}, status=status)

    async def body_of(req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    def ready(s):
        return s.client is not None and s.status == "CONNECTED" and rq.available(s.client)

    async def mex(s, query_id, variables, field, code):
        try:
            return await execute(rq.send_mex, s.client, query_id, variables, field)
        except rq.QueryError as e:
            log.info("channels read refused (%s)", type(e).__name__)
            return error(code)

    async def list_channels(req):
        s = session(req)
        if not ready(s):
            return error("channels_unavailable")
        answer = await mex(s, SUBSCRIBED_QUERY, {"fetch_wamo_sub": False,
                                                 "fetch_status_metadata": False},
                           SUBSCRIBED_FIELD, "channels_unavailable")
        if isinstance(answer, web.Response):
            return answer
        return ok(status="success", response=nodes_of(answer))

    async def directory(req):
        s = session(req)
        if not ready(s):
            return error("channels_unavailable")
        query_id, variables, field = directory_request(await body_of(req))
        answer = await mex(s, query_id, variables, field, "channels_unavailable")
        if isinstance(answer, web.Response):
            return answer
        return ok(status="success", response=nodes_of(answer))

    async def info(req):
        s = session(req)
        key = channel_key(await body_of(req))
        if not key:
            return error("invalid_channel", 400)
        if not ready(s):
            return error("channels_unavailable")
        answer = await mex(s, CHANNEL_QUERY, channel_request(key), CHANNEL_FIELD,
                           "channel_not_found")
        if isinstance(answer, web.Response):
            return answer
        if not isinstance(answer, dict) or not answer.get("id"):
            return error("channel_not_found", 404)
        return ok(status="success", response=answer)

    def following(action, code):
        async def handler(req):
            s = session(req)
            channel = channel_id((await body_of(req)).get("id"))
            if not channel:
                return error("invalid_channel", 400)
            if s.client is None or s.status != "CONNECTED":
                return error("channels_unavailable")
            try:
                await execute(getattr(s.client, action), jid_from_text(channel))
            except Exception as e:
                log.info("channel %s refused (%s)", action, type(e).__name__)
                return error(code)
            return ok(status="success", response={"id": channel})
        return handler

    async def mute(req):
        s = session(req)
        body = await body_of(req)
        channel = channel_id(body.get("id"))
        if not channel or not isinstance(body.get("mute"), bool):
            return error("invalid_channel", 400)
        if s.client is None or s.status != "CONNECTED":
            return error("channels_unavailable")
        try:
            await execute(s.client.newsletter_toggle_mute, jid_from_text(channel), body["mute"])
        except Exception as e:
            log.info("channel mute refused (%s)", type(e).__name__)
            return error("channel_mute_failed")
        return ok(status="success", response={"id": channel, "muted": body["mute"]})

    async def pin(req):
        s = session(req)
        request = pin_request(await body_of(req))
        if request is None:
            return error("invalid_channel", 400)
        if not ready(s):
            return error("channels_unavailable")
        query_id, variables, field, done = request
        answer = await mex(s, query_id, variables, field, "channel_pin_failed")
        if isinstance(answer, web.Response):
            return answer
        return ok(status="success", response=done)

    async def admins(req):
        s = session(req)
        channel = channel_id((await body_of(req)).get("id"))
        if not channel:
            return error("invalid_channel", 400)
        if not ready(s):
            return error("channels_unavailable")
        answer = await mex(s, ADMIN_INFO_QUERY, {"newsletter_id": channel}, ADMIN_INFO_FIELD,
                           "channel_admins_unavailable")
        if isinstance(answer, web.Response):
            return answer
        return ok(status="success", response={"id": channel, "adminCount": admin_count(answer)})

    def lid_of(s, user):
        """The person's LID text, the form the invite names, or ''."""
        if user.endswith("@lid"):
            return user
        try:
            lid = s.client.get_lid_from_pn(jid_from_text(user))
        except Exception:
            return ""
        number = getattr(lid, "User", "")
        return f"{number}@lid" if number else ""

    async def admin_invite(req):
        s = session(req)
        request = admin_invite_request(await body_of(req))
        if request is None:
            return error("invalid_channel", 400)
        if not ready(s):
            return error("channels_unavailable")
        channel, user, name, caption = request
        lid = await execute(lid_of, s, user)
        if not lid:
            return error("channel_admin_invite_failed")
        answer = await mex(s, INVITE_QUERY, {"newsletter_id": channel, "user_id": lid},
                           INVITE_FIELD, "channel_admin_invite_failed")
        if isinstance(answer, web.Response):
            return answer
        expiration = invite_expiration(answer)
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, NewsletterAdminInviteMessage
        message = Message(newsletterAdminInviteMessage=NewsletterAdminInviteMessage(
            **invite_fields(channel, name, caption, expiration)))
        try:
            await execute(s.client.send_message, jid_from_text(lid), message)
        except Exception as e:
            # The invite exists, its message did not go out: the person never sees it.
            log.info("channel admin invite message refused (%s)", type(e).__name__)
            return error("channel_admin_invite_failed")
        return ok(status="success", response={"id": channel, "user": user, "expiration": expiration})

    async def followers(req):
        s = session(req)
        channel = channel_id((await body_of(req)).get("id"))
        if not channel:
            return error("invalid_channel", 400)
        if not ready(s):
            return error("channels_unavailable")
        answer = await mex(s, FOLLOWERS_QUERY,
                           {"input": {"newsletter_id": channel, "count": FOLLOWER_LIMIT}},
                           FOLLOWERS_FIELD, "channel_followers_unavailable")
        if isinstance(answer, web.Response):
            return answer
        return ok(status="success", response={"id": channel, "followers": followers_of(answer)})

    async def admin_pending(req):
        s = session(req)
        channel = channel_id((await body_of(req)).get("id"))
        if not channel:
            return error("invalid_channel", 400)
        if not ready(s):
            return error("channels_unavailable")
        answer = await mex(s, PENDING_QUERY, {"newsletter_id": channel}, ADMIN_INFO_FIELD,
                           "channel_admins_unavailable")
        if isinstance(answer, web.Response):
            return answer
        return ok(status="success", response={"id": channel, "pending": pending_of(answer)})

    def person_mutation(query_id, code):
        """A mutation naming one person by LID (revoke, demote, change owner):
        only an error answer says no."""
        async def handler(req):
            s = session(req)
            body = await body_of(req)
            channel = channel_id(body.get("id")) if body.get("id") else ""
            user = person(body.get("user"))
            if not channel or not user:
                return error("invalid_channel", 400)
            if not ready(s):
                return error("channels_unavailable")
            lid = await execute(lid_of, s, user)
            if not lid:
                return error(code)
            try:
                answer = await execute(s.client.send_mex, query_id,
                                       {"newsletter_id": channel, "user_id": lid})
            except Exception as e:
                log.info("channel %s refused (%s)", code, type(e).__name__)
                return error(code)
            if not isinstance(answer, dict) or answer.get("error"):
                log.info("channel %s refused", code)
                return error(code)
            return ok(status="success", response={"id": channel, "user": user})
        return handler

    r.add_post("/api/{tok}/channels/followers", followers)
    r.add_post("/api/{tok}/channels/admin-pending", admin_pending)
    r.add_post("/api/{tok}/channels/admin-demote",
               person_mutation(DEMOTE_QUERY, "channel_admin_demote_failed"))
    r.add_post("/api/{tok}/channels/owner-change",
               person_mutation(OWNER_QUERY, "channel_owner_change_failed"))
    r.add_post("/api/{tok}/channels/list", list_channels)
    r.add_post("/api/{tok}/channels/admins", admins)
    r.add_post("/api/{tok}/channels/admin-invite", admin_invite)
    r.add_post("/api/{tok}/channels/admin-revoke",
               person_mutation(REVOKE_QUERY, "channel_admin_revoke_failed"))
    r.add_post("/api/{tok}/channels/pin", pin)
    r.add_post("/api/{tok}/channels/mute", mute)
    r.add_post("/api/{tok}/channels/directory", directory)
    r.add_post("/api/{tok}/channels/info", info)
    r.add_post("/api/{tok}/channels/follow", following("follow_newsletter", "channel_follow_failed"))
    r.add_post("/api/{tok}/channels/unfollow",
               following("unfollow_newsletter", "channel_unfollow_failed"))
