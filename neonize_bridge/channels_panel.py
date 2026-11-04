"""WinZapp's channels window (Alt+9): `channels/list`, `channels/directory`,
`channels/info`, `channels/follow`, `channels/unfollow` and `channels/mute`.

The WPPConnect side calls WhatsApp Web's own MEX jobs for each read
(client/api_patches/src/controller/channelsPanelController.ts); the bridge
sends the same queries (`send_mex`, neonize patch 0006) and answers the same
XWA2Newsletter nodes, which the client reads once (client/core/channels.py).
The query ids were read over CDP on 2026-11-04:

- 25399611239711790, the followed channels (`xwa2_newsletter_subscribed`);
- 26301059626252132, a directory search (`xwa2_newsletters_directory_search`);
- 25806748772361516, the recommended channels (`xwa2_newsletters_recommended`);
- 27456920720571478, one channel by id or invite code (`xwa2_newsletter`).

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
    text, or the recommended channels when there is none."""
    body = body if isinstance(body, dict) else {}
    query = str(body.get("query") or "").strip()[:100]
    try:
        limit = max(1, min(50, int(body.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    if query:
        return SEARCH_QUERY, {"input": {"search_text": query, "categories": [], "limit": limit,
                                        "start_cursor": None},
                              "fetch_status_metadata": False}, SEARCH_FIELD
    return RECOMMENDED_QUERY, {"input": {"limit": limit, "country_codes": []},
                               "fetch_status_metadata": False}, RECOMMENDED_FIELD


def channel_request(key):
    """Variables of the one-channel read, as WhatsApp Web's mexGetNewsletter
    sends them for a JID or an invite code."""
    by_id = key.endswith("@" + SERVER)
    return {"input": {"key": key, "type": "JID" if by_id else "INVITE", "view_role": "GUEST"},
            "fetch_viewer_metadata": True, "fetch_full_image": by_id, "fetch_creation_time": True,
            "fetch_wamo_sub": False, "fetch_status_metadata": False,
            "fetch_pinned_messages": False}


def nodes_of(answer):
    """The newsletter nodes of a list or directory answer."""
    if isinstance(answer, list):
        found = answer
    elif isinstance(answer, dict):
        found = answer.get("result")
    else:
        found = None
    return [n for n in found if isinstance(n, dict)] if isinstance(found, list) else []


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

    r.add_post("/api/{tok}/channels/list", list_channels)
    r.add_post("/api/{tok}/channels/mute", mute)
    r.add_post("/api/{tok}/channels/directory", directory)
    r.add_post("/api/{tok}/channels/info", info)
    r.add_post("/api/{tok}/channels/follow", following("follow_newsletter", "channel_follow_failed"))
    r.add_post("/api/{tok}/channels/unfollow",
               following("unfollow_newsletter", "channel_unfollow_failed"))
