"""Call links: call/link/create, call/link/join, call/link/waiting-room and
call/link/admit, the routes WinZapp's WPPConnect side answers from WhatsApp
Web's own call-link code (callController.ts, callLinkRuntime.ts).

A call link is WhatsApp's "New call link": https://call.whatsapp.com/voice/<token>
or .../video/<token>, a group call anyone with the link can join. neonize's
CallClient creates it and joins its call (meowcaller api/call-link-join,
captured from WhatsApp Web on 2026-11-18): the first to join becomes the
call's creator and admin. Whoever opens the link without a WhatsApp account
(a guest) always waits in the call's waiting room until the admin lets them
in; the client asks for that room while it is in a link call.

A call-link call has no chat: no peer, no group, and no call record is filed
for it (calls.record_finished).

The pure functions are at the top (tested in test_call_links.py).
"""
import asyncio
import logging
import re

from aiohttp import web

log = logging.getLogger("bridge")

# WhatsApp Web's WAWebParseCallLinkType (call.whatsapp.com, and web.whatsapp.com/call/)
_LINK = re.compile(r"^https://(?:call\.whatsapp\.com|web\.whatsapp\.com/call)/(video|voice|audio)/(\w+)(?:\?.*)?$",
                   re.IGNORECASE)
# WAWebVoipLinkPreviewCallLink: a token of any other length is "Invalid call link"
TOKEN_LENGTH = 22
# a waiting-room user who asked to enter (the captured state)
WAITING = "waiting_room_joined"


def parse_link(text) -> tuple:
    """(is_video, canonical https://call.whatsapp.com/... URL) of a call link,
    or (None, "") when `text` is not one."""
    found = _LINK.match(str(text or "").strip())
    if not found or len(found.group(2)) != TOKEN_LENGTH:
        return None, ""
    kind = found.group(1).lower()
    if kind == "audio":     # meowcaller names a voice link's media "audio"; WhatsApp's own URL says "voice"
        kind = "voice"
    return kind == "video", f"https://call.whatsapp.com/{kind}/{found.group(2)}"


def waiting_rows(room, name_of) -> list:
    """[{jid, name, guest}] of the people waiting to enter, from the layer's
    waiting room. `name_of(jid, pn)` gives a saved name; a guest is known by
    the name it typed. Someone with no name at all is still listed (the
    client says "someone"), never by number."""
    rows, seen = [], set()
    for user in (room or {}).get("users") or ():
        if not isinstance(user, dict) or str(user.get("state") or "") != WAITING:
            continue
        jid = str(user.get("jid") or "")
        if not jid or jid in seen:
            continue
        seen.add(jid)
        guest = bool(user.get("guest"))
        name = "" if guest else (name_of(str(user.get("pn") or "") or jid, jid) or "")
        rows.append({"jid": jid, "name": name or str(user.get("pushName") or ""), "guest": guest})
    return rows


def register_routes(r, *, session, ok):
    """Wire the call-link routes to the aiohttp router `r`."""

    def error(message, status=500):
        return web.json_response({"status": "error", "message": str(message)}, status=status)

    async def in_thread(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    async def body_of(req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    def ready(s):
        media = getattr(s, "call_media", None)
        if media is None or not media.call_links_available():
            return None, error("this neonize build has no call links", 501)
        if s.client is None or s.status != "CONNECTED":
            return None, error("Disconnected", 400)
        return media, None

    def call_id_of(body):
        call_id = str(body.get("callId") or "")
        return call_id if 0 < len(call_id) <= 512 else ""

    async def create(req):
        s = session(req)
        media, refused = ready(s)
        if refused is not None:
            return refused
        video = (await body_of(req)).get("isVideo") is True
        try:
            made = await in_thread(media.create_link, video)
        except Exception as e:
            log.info("call link not created (%s)", type(e).__name__)
            return error(f"WhatsApp did not create the call link: {e}")
        is_video, link = parse_link(made.get("link"))
        if not link:
            return error("WhatsApp answered no call link")
        return ok(status="success", response={"link": link, "isVideo": is_video})

    async def join(req):
        s = session(req)
        media, refused = ready(s)
        if refused is not None:
            return refused
        video, link = parse_link((await body_of(req)).get("link"))
        if not link:
            return error("A call link is required", 400)
        if media.live_call():
            return error("Another call is already active", 409)
        try:
            joined, answer = await in_thread(media.join_link, link, video)
        except Exception as e:
            log.info("call link join refused (%s)", type(e).__name__)
            return error(f"WhatsApp did not join the call link: {e}", 409)
        return ok(status="success", response={
            "id": joined, "isVideo": video, "isCallLink": True, "peerJid": "",
            "waiting": bool(answer.get("waiting")), "admin": bool(answer.get("admin")),
            "state": "CONNECTING" if answer.get("waiting") else "ACTIVE", "outgoing": False})

    async def waiting_room(req):
        s = session(req)
        media, refused = ready(s)
        if refused is not None:
            return refused
        call_id = call_id_of(await body_of(req))
        if not call_id:
            return error("A call id is required", 400)

        def name_of(jid, lid):
            found = s.db.contact_of(jid, lid)
            return found.get("name") or found.get("pushname") or ""

        def read():
            room = media.waiting_room(call_id)
            return room, (waiting_rows(room, name_of) if room is not None else [])
        try:
            room, people = await in_thread(read)
        except Exception as e:
            log.info("call link waiting room: %s", type(e).__name__)
            return error("The waiting room is unavailable")
        if room is None:
            return error("The call is over", 404)
        return ok(status="success", response={
            "id": call_id, "admin": bool(room.get("admin")), "enabled": bool(room.get("enabled")),
            "waiting": bool(room.get("waiting")), "people": people})

    async def admit(req):
        s = session(req)
        media, refused = ready(s)
        if refused is not None:
            return refused
        body = await body_of(req)
        call_id, user = call_id_of(body), str(body.get("jid") or "")
        if user.endswith("@c.us"):          # meowcaller takes a user by its server name
            user = user[:-len("@c.us")] + "@s.whatsapp.net"
        if not call_id or not user.endswith(("@lid", "@s.whatsapp.net")):
            return error("Parameters callId and jid are required!", 400)
        allow = body.get("admit") is not False
        try:
            await in_thread(media.decide_waiting, call_id, user, allow)
        except Exception as e:
            log.info("call link waiting room decision refused (%s)", type(e).__name__)
            return error(f"WhatsApp did not take the decision: {e}", 409)
        return ok(status="success", response={"id": call_id, "admitted": allow})

    r.add_post("/api/{tok}/call/link/create", create)
    r.add_post("/api/{tok}/call/link/join", join)
    r.add_post("/api/{tok}/call/link/waiting-room", waiting_room)
    r.add_post("/api/{tok}/call/link/admit", admit)
