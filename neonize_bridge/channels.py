"""Channel (newsletter) routes of the bridge: create a channel and mute/unmute a
channel. The WinZapp client calls no channel route (@newsletter chats are
ignored, see CLAUDE.md), so the format of the responses follows the bridge's
other routes: {"status": "success", "response": ...}.

Left out here, on purpose: `PUT /newsletter/:id` (edit) and `DELETE /newsletter/:id`
(delete the channel). neonize 0.5.2 has no function for either (only
create/follow/unfollow/toggle_mute/info/messages); `unfollow_newsletter` stops
following, which is not deleting the channel, so it was not used instead.

The pure functions are at the top (tested in test_channels.py); `register_routes`
wires the routes to the server.
"""
import asyncio
import base64
import binascii
import logging

from aiohttp import web

log = logging.getLogger("bridge")

SERVER = "newsletter"
# Values of the neonize enums (NewsletterMuteState.ON, NewsletterThreadMetadata
# .VERIFIED), repeated here so the pure functions do not import neonize.
MUTE_ON = 1
VERIFIED = 1


def channel_id(text) -> str:
    """'123', '123@newsletter' -> '123@newsletter'. Empty if it is not a channel
    (another server, no user): neonize would treat a person's JID as a channel
    and the error would only come from WhatsApp."""
    user, _, server = str(text or "").strip().partition("@")
    if not user or server not in ("", SERVER):
        return ""
    return "%s@%s" % (user, SERVER)


def load_names(client, db, build_jid):
    """Writes the name of each stored channel that has none, which the listing
    needs (it goes out only with one, chat_listing.listed): once per channel,
    not a query for every channel at every connect. A channel WhatsApp no
    longer answers for, one the account left, stays as it was."""
    for jid in db.channels():
        user = jid.partition("@")[0]
        try:
            name = client.get_newsletter_info(build_jid(user, SERVER)).ThreadMeta.Name.Text
        except Exception as error:
            log.info("channel name refused (%s)", type(error).__name__)
            continue
        if name:
            db.channel_name(jid, name)


def image_from_text(text) -> bytes:
    """Base64 (or data URL) -> bytes; b'' if no image came.

    Never returns `str`: neonize treats a `str` as a file path or URL and would
    read any file on the disk. Raises ValueError if it is not base64."""
    text = str(text or "").strip()
    if not text:
        return b""
    if text.startswith("data:"):
        text = text.partition(",")[2]
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("picture must be base64") from error


def creation_request(body) -> dict:
    """Body of `POST /newsletter` -> {name, description, picture(bytes)}.
    The image can come in `picture` or in `options.picture`. Raises ValueError
    without a name or with an image that is not base64."""
    body = body if isinstance(body, dict) else {}
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    options = body.get("options")
    options = options if isinstance(options, dict) else {}
    image = body.get("picture") or options.get("picture")
    description = body.get("description")
    if description is None:
        description = options.get("description")
    return {"name": name, "description": str(description or ""),
            "picture": image_from_text(image)}


def mute_from(body) -> bool:
    """Body of `POST /mute-newsletter/:id` -> True to mute, False to unmute.
    Accepts `mute`, `value` or `muted`; with nothing, mutes."""
    if isinstance(body, dict):
        for key in ("mute", "value", "muted"):
            if key in body:
                value = body[key]
                if isinstance(value, str):
                    return value.strip().lower() not in ("false", "0", "no", "off", "")
                return bool(value)
    return True


def channel_to_wpp(meta, jid_text) -> dict:
    """neonize NewsletterMetadata -> the response dictionary of a channel.
    `jid_text` converts a neonize JID into 'user@server'."""
    thread, viewer = meta.ThreadMeta, meta.ViewerMeta
    return {
        "id": jid_text(meta.ID),
        "name": thread.Name.Text,
        "description": thread.Description.Text,
        "inviteCode": thread.InviteCode,
        "creationTime": int(thread.CreationTime or 0),
        "subscribersCount": int(thread.SubscriberCount or 0),
        "verified": thread.VerificationState == VERIFIED,
        "muted": viewer.Mute == MUTE_ON,
    }


def channel_not_found(error: Exception) -> bool:
    text = str(error).lower()
    return "not-found" in text or "not found" in text or "404" in text


def register_routes(r, *, session, ok, jid_from_text, jid_text):
    """Wire the channel routes to the aiohttp router `r`."""

    async def execute(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def error(message, status=500):
        return web.json_response({"status": "error", "message": str(message)}, status=status)

    def connected(s):
        return s.client is not None and s.status == "CONNECTED"

    async def json_body(req):
        try:
            return await req.json()
        except ValueError:
            return {}

    async def create_channel(req):
        s = session(req)
        body = await json_body(req)
        if not connected(s):
            return error("Disconnected", 400)
        try:
            request = creation_request(body)
        except ValueError as e:
            return error(e, 400)
        try:
            meta = await execute(
                s.client.create_newsletter, request["name"], request["description"],
                request["picture"])
        except Exception as e:
            log.exception("newsletter (create)")
            return error(e)
        return web.json_response(
            {"status": "success", "response": channel_to_wpp(meta, jid_text)}, status=201)

    async def mute_channel(req):
        s = session(req)
        body = await json_body(req)
        if not connected(s):
            return error("Disconnected", 400)
        channel = channel_id(req.match_info["id"])
        if not channel:
            return error("not a newsletter id", 400)
        mute = mute_from(body)
        try:
            await execute(
                s.client.newsletter_toggle_mute, jid_from_text(channel), mute)
        except Exception as e:
            if channel_not_found(e):
                return error("newsletter not found", 404)
            log.exception("mute-newsletter")
            return error(e)
        return ok(status="success", response={"id": channel, "muted": mute})

    r.add_post("/api/{tok}/newsletter", create_channel)
    r.add_post("/api/{tok}/mute-newsletter/{id}", mute_channel)
