"""Profile and privacy routes of the bridge: the account's own profile (name,
about, photo), the photo and about of other people and the account's privacy
settings. The response formats are the ones the WinZapp client reads
(core/whatsapp_profile.py, core/whatsapp_privacy.py, core/profile_picture.py,
main_window/identity.py).

The pure functions are at the top (tested in test_profile_privacy.py);
`register_routes` wires the routes to the server. Nothing here was proven
against a real account.
"""
import asyncio
import logging
import re

from aiohttp import web

import msgmap
import status_privacy
from about_cache import AboutCache, failure_code

log = logging.getLogger("bridge")

# A profile photo is small; above this it is not one.
MAX_PHOTO = 10 * 1024 * 1024

# The account's settings in WinZapp's vocabulary (the same as privacyController.ts) ->
# (name in neonize.utils.enum.PrivacySettingType, accepted values).
SETTINGS = {
    "lastSeen": ("LAST_SEEN", ("all", "contacts", "none")),
    "online": ("ONLINE", ("all", "match_last_seen")),
    "about": ("STATUS", ("all", "contacts", "none")),      # "status" is the about
    "profilePicture": ("PROFILE", ("all", "contacts", "none")),
    "readReceipts": ("READ_RECEIPTS", ("all", "none")),
    "groupAdd": ("GROUP_ADD", ("all", "contacts")),
}

# PrivacySettings.PrivacySetting of the neonize proto -> WhatsApp Web text.
# 1 (UNDEFINED) is left out: the client reads null as "value I do not know".
PRIVACY_VALUES = {2: "all", 3: "contacts", 4: "contact_blacklist",
                  5: "match_last_seen", 6: "known", 7: "none"}

# PrivacySettings field -> WinZapp setting.
PRIVACY_FIELDS = {"LastSeen": "lastSeen", "Online": "online", "Status": "about",
                  "Profile": "profilePicture", "ReadReceipts": "readReceipts",
                  "GroupAdd": "groupAdd"}

_PERSON_ID = re.compile(r"^\d+(?:@(?:c\.us|s\.whatsapp\.net|lid))?$")


def profile_jid(phone: str, is_group: bool = False) -> str:
    """The id the profile routes receive ('5511999', '5511999@c.us', 'x@lid')
    -> whatsmeow's 'user@server'. With `isGroup=true` the number is a group's."""
    phone = str(phone or "")
    if "@" in phone:
        return msgmap.jid_whatsmeow(phone)
    return phone + ("@g.us" if is_group else "@s.whatsapp.net")


def valid_person_id(text: str) -> bool:
    """Only a person: not a group, nor a channel, nor any expression."""
    return bool(_PERSON_ID.match(str(text or "")))


def valid_setting(setting, value) -> bool:
    """The setting exists and the value is among the ones WhatsApp accepts for it."""
    return (isinstance(setting, str) and isinstance(value, str)
            and setting in SETTINGS and value in SETTINGS[setting][1])


def settings_to_wpp(privacy_settings) -> dict:
    """neonize PrivacySettings -> `settings` of `POST /privacy`. A value that
    WhatsApp did not report (or that is not known) goes as None."""
    return {setting: PRIVACY_VALUES.get(int(getattr(privacy_settings, field, 0) or 0))
            for field, setting in PRIVACY_FIELDS.items()}


def setting_applied(privacy_settings, setting, value) -> bool:
    """WhatsApp's settings already show *value* for *setting*. A change that
    raised can still have reached the server (neonize before patch 0025
    raised on every change), so the route reads the settings back before
    telling the user it failed."""
    return privacy_settings is not None and settings_to_wpp(privacy_settings).get(setting) == value


def no_photo(error: Exception) -> bool:
    """The person/group has no photo or hid it from us: a definitive answer from
    the server, not a failure."""
    text = str(error).lower()
    return ("profile picture" in text or "item-not-found" in text
            or "not-authorized" in text or "unauthorized" in text)


def photo_to_wpp(info, jid: str):
    """ProfilePictureInfo -> the record the client reads (`imgFull`, `img`,
    `eurl`, `id`, `tag`); None when there is no URL."""
    url = getattr(info, "URL", "") or ""
    if not url:
        return None
    return {"eurl": url, "img": url, "imgFull": url, "id": jid,
            "tag": getattr(info, "ID", "") or ""}


def about_of(infos) -> str:
    """The about ('Sobre') of the first person in the get_user_info response."""
    for item in infos or ():
        text = getattr(getattr(item, "UserInfo", None), "Status", "") or ""
        if text:
            return text
    return ""


def is_jpeg(data: bytes) -> bool:
    """whatsmeow only sends profile photos as JPEG."""
    return bytes(data[:3]) == b"\xff\xd8\xff"


def profile_photo(data: bytes) -> bytes:
    """The photo in the only format whatsmeow sends (JPEG). The client also
    lets the user pick PNG (core/whatsapp_profile.py): converts, as with the
    group photo. JPEG goes through as it came; a file that is not an image
    raises an exception."""
    if is_jpeg(data):
        return data
    from groups_extra import photo_to_jpeg
    return photo_to_jpeg(data)


def own_profile(name, about, has_photo) -> dict:
    """`{name, status, photo}` of `my-profile`: what could not be read goes as
    None (the client shows the rest), and empty text is also 'I do not know'."""
    def text(value):
        return value.strip() or None if isinstance(value, str) else None
    return {"name": text(name), "status": text(about),
            "photo": has_photo if isinstance(has_photo, bool) else None}


def register_routes(r, *, session, ok, jid_from_text, jid_text):
    """Wire the profile and privacy routes to the aiohttp router `r`."""

    async def execute(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def error(message, status=500):
        return web.json_response({"status": "error", "message": str(message)}, status=status)

    def error_code(code, status, **extra):
        # privacy/* answers by `code` (and `kind`), like privacyController.ts;
        # the client never sees the exception text, which can have a phone number.
        return web.json_response({"status": "error", "code": code, **extra}, status=status)

    def connected(s):
        return s.client is not None and s.status == "CONNECTED"

    async def my_profile(req):
        s = session(req)
        if not connected(s):
            return error_code("profile_unavailable", 503)
        me = jid_from_text(s.me)
        name = about = photo = None
        try:
            name = (await execute(s.client.get_me)).PushName
        except Exception as e:
            log.info("my-profile: name not read (%s)", type(e).__name__)
        try:
            about = about_of(await execute(s.client.get_user_info, me))
        except Exception as e:
            log.info("my-profile: about not read (%s)", type(e).__name__)
        try:
            photo = photo_to_wpp(await execute(s.client.get_profile_picture, me), "") is not None
        except Exception as e:
            # no photo is an answer; any other failure leaves "I do not know"
            photo = False if no_photo(e) else None
        return ok(status="success", response=own_profile(name, about, photo))

    async def change_username(req):
        s = session(req)
        body = await req.json()
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return error("name is required", 400)
        if not connected(s):
            return error("Disconnected", 400)
        try:
            await execute(s.client.set_profile_name, name.strip())
        except Exception as e:
            log.warning("change-username: %s", type(e).__name__)
            return error(e)
        return ok(status="success", response={})

    async def set_profile_status(req):
        s = session(req)
        body = await req.json()
        text = body.get("status")
        if not isinstance(text, str):
            return error("status is required", 400)
        if not connected(s):
            return error("Disconnected", 400)
        try:
            await execute(s.client.set_status_message, text)
        except Exception as e:
            log.warning("profile-status: %s", type(e).__name__)
            return error(e)
        return ok(status="success", response={})

    async def get_profile_status(req):
        s = session(req)
        if not connected(s):
            return error("Disconnected", 400)
        jid = jid_from_text(profile_jid(req.match_info["phone"]))
        key = jid_text(jid)
        cache = getattr(s, "about_cache", None)
        if cache is None:
            cache = s.about_cache = AboutCache()
        known = cache.lookup(key)
        if known is None and cache.paused():
            return error_code("rate_limited", 503)
        if known is None:
            try:
                about = about_of(await execute(s.client.get_user_info, jid))
            except Exception as e:
                log.info("profile-status: %s %s", type(e).__name__, failure_code(e))
                cache.failed(key, e)
                return error(e)
            cache.remember(key, about)
        else:
            about, failure = known
            if about is None:
                return error(failure)
        return ok(status="success", response={"id": msgmap.jid_wpp(key), "status": about})

    async def profile_pic(req):
        s = session(req)
        if not connected(s):
            return error("Disconnected", 400)
        group = req.query.get("isGroup", "").lower() == "true"
        jid = jid_from_text(profile_jid(req.match_info["phone"], group))
        try:
            info = await execute(s.client.get_profile_picture, jid)
        except Exception as e:
            if no_photo(e):
                return ok(status="success", response=None)
            log.info("profile-pic: %s", type(e).__name__)
            return error(e)
        return ok(status="success",
                  response=photo_to_wpp(info, msgmap.jid_wpp(jid_text(jid))))

    async def set_profile_pic(req):
        s = session(req)
        if not connected(s):
            return error("Disconnected", 400)
        data = None
        reader = await req.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file" and part.filename is not None:
                data = bytes(await part.read(decode=False))
                break
        if not data:
            return error("file is required", 400)
        if len(data) > MAX_PHOTO:
            return error("photo is too large", 413)
        try:
            jpeg = await execute(profile_photo, data)
        except Exception as e:
            log.info("set-profile-pic: image not converted (%s)", type(e).__name__)
            return error("unsupported image", 415)
        try:
            await execute(s.client.set_profile_photo, jpeg)
        except Exception as e:
            log.warning("set-profile-pic: %s", type(e).__name__)
            return error(e)
        return ok(status="success", response={})

    async def get_phone_number(req):
        s = session(req)
        if not connected(s) or not s.me:
            return error("Disconnected", 400)
        return ok(status="success", response=msgmap.jid_wpp(s.me), mapper="device")

    async def last_seen(req):
        # The answer comes from the presence memory (presence.py), fed by the
        # events; "unsupported" is what the client understands as "no data or
        # hidden", without insisting.
        if not valid_person_id(req.match_info["phone"]):
            return web.json_response(
                {"status": "error", "response": "Invalid contact id"}, status=400)
        import time

        import presence
        memory = getattr(session(req), "presences", None)
        snapshot = memory.read(req.match_info["phone"], time.time()) if memory else None
        return ok(status="success", **presence.last_seen_response(snapshot))

    async def privacy(req):
        s = session(req)
        if not connected(s):
            return error_code("privacy_unavailable", 503)
        try:
            privacy_settings = await execute(s.client.get_privacy_settings)
        except Exception as e:
            log.info("privacy: %s", type(e).__name__)
            return error_code("privacy_unavailable", 503)
        # `status` (who sees my Status) comes from the app state copy
        # (status_privacy.py); None hides the choice in the client.
        try:
            status = await execute(status_privacy.read_status, s)
        except Exception as e:
            log.info("privacy: status not read (%s)", type(e).__name__)
            status = None
        return ok(status="success", response={
            "settings": settings_to_wpp(privacy_settings), "status": status})

    async def privacy_set(req):
        s = session(req)
        body = await req.json()
        setting, value = body.get("setting"), body.get("value")
        if not valid_setting(setting, value):
            return error_code("invalid_privacy_setting", 400)
        if not connected(s):
            return error_code("privacy_unavailable", 503)
        from neonize.utils.enum import PrivacySetting, PrivacySettingType
        try:
            await execute(s.client.set_privacy_setting,
                          PrivacySettingType[SETTINGS[setting][0]], PrivacySetting(value))
        except Exception as e:
            try:
                current = await execute(s.client.get_privacy_settings)
            except Exception:
                current = None
            if setting_applied(current, setting, value):
                log.info("privacy/set: %s raised but %s is applied", type(e).__name__, setting)
                return ok(status="success")
            log.warning("privacy/set: %s", type(e).__name__)
            return error_code("privacy_setting_failed", 502, kind=type(e).__name__)
        return ok(status="success")

    r.add_get("/api/{tok}/my-profile", my_profile)
    r.add_post("/api/{tok}/change-username", change_username)
    r.add_post("/api/{tok}/profile-status", set_profile_status)
    r.add_get("/api/{tok}/profile-status/{phone}", get_profile_status)
    r.add_get("/api/{tok}/profile-pic/{phone}", profile_pic)
    r.add_post("/api/{tok}/set-profile-pic", set_profile_pic)
    r.add_get("/api/{tok}/get-phone-number", get_phone_number)
    r.add_get("/api/{tok}/last-seen/{phone}", last_seen)
    r.add_post("/api/{tok}/privacy", privacy)
    r.add_post("/api/{tok}/privacy/set", privacy_set)
