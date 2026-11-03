"""Archived chats and new messages: WhatsApp's "Keep chats archived".

Reported: with Neonize, a message arrived in an archived group and WinZapp
went on showing it under Archived, as if nothing had come, while the phone
had brought it back to the chat list. With "Keep chats archived" off, each of
WhatsApp's clients takes an archived chat out of the archive when a message
arrives; no state notice says so (WhatsApp Web did it by itself for
WPPConnect). The setting is the app state entry `setting_unarchiveChats`
(SyncActionValue.unarchiveChatsSetting.unarchiveChats: True means a new
message unarchives), kept by app_state_mirror.

What WhatsApp Web does (WAWebMsgRangeContainmentHandlers.handleForArchive,
read over CDP): a message beyond the range the archive covered takes the chat
out when the setting allows it, muted or not. A message deleted for everyone
is not a new message and leaves the chat where it is.

Never seen, the setting counts as on, WhatsApp's default: the chat stays
archived, as the bridge always left it. The first archived chat that gets a
message while the copy of `regular_low` is not complete asks for it once, so
the next message already follows the setting.

WinZapp's Settings > WhatsApp > Privacy reads and changes the setting through
`keep-chats-archived` (register_routes): GET answers `keepArchived`, the value
the bridge follows (on when WhatsApp never stored one), POST `{"keepArchived": bool}` sends the same app state
mutation WhatsApp Web's own switch sends (WAWebArchiveSettingSync, version 4)
and answers what the copy holds afterwards. A build without app state
mutations answers 501.

The pure functions are at the top (tested in test_chat_archive.py).
"""
import asyncio
import logging
import threading

from aiohttp import web

import app_state_mirror
import msgmap
from store import SYSTEM_TYPES

log = logging.getLogger("bridge")

SETTING = "setting_unarchiveChats"
COLLECTION = "regular_low"
VERSION = 4


def unarchives_on_message(entries) -> bool:
    """From the mirror's entries of SETTING [(index, version, value bytes)]:
    whether a new message takes a chat out of the archive. None stored: False."""
    if not entries:
        return False
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import SyncActionValue
    _index, _version, value = max(entries, key=lambda entry: entry[1])
    try:
        parsed = SyncActionValue.FromString(bytes(value))
    except Exception:
        return False
    return bool(parsed.unarchiveChatsSetting.unarchiveChats)


#: Received messages that are not new ones: a group notice, a call record,
#: a deletion for everyone.
NOT_NEW = (*SYSTEM_TYPES, "revoked")


def keep_archived(entries):
    """"Keep chats archived" from the mirror's entries of SETTING: the value
    on_arrival() follows, so on when WhatsApp never stored one."""
    return not unarchives_on_message(entries)


def setting_value(keep):
    """The SyncActionValue WhatsApp Web sends for its "Keep chats archived"."""
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import SyncActionValue
    value = SyncActionValue()
    value.unarchiveChatsSetting.unarchiveChats = not keep
    return value


def parse_request(body):
    """`{"keepArchived": bool}` -> the bool; None when it is not one."""
    keep = body.get("keepArchived") if isinstance(body, dict) else None
    return keep if isinstance(keep, bool) else None


def leaves_archive(archived, message, unarchives) -> bool:
    """Whether `message` (WPPConnect format) takes its archived chat out: a new
    one received, with the setting off."""
    return (bool(archived) and bool(unarchives) and not message.get("fromMe")
            and message.get("type") not in NOT_NEW)


def on_arrival(s, chat, message):
    """After `message` was stored in `chat`: unarchive it when WhatsApp would,
    and tell the client as the phone's own notices do (chats-update). A chat
    kept under both its number and its LID (chat_aliases) is archived under
    whichever address the phone's notice named, so every address is looked at."""
    if message.get("fromMe"):
        return
    keys = [key for key in (s.chat_keys(chat) or [chat]) if s.db.archived(key)]
    if not keys or not app_state_mirror.supported():
        return
    mirror = app_state_mirror.mirror_of(s)
    if not mirror.seeded(COLLECTION):
        _learn_setting_once(s)
    if not leaves_archive(True, message, unarchives_on_message(mirror.entries(SETTING))):
        return
    log.info("a message unarchived an archived chat (keep chats archived is off)")
    for key in keys:
        s.db.chat(key, archived=0)
        chat_w = msgmap.jid_wpp(key)
        s.emit("chats-update", {"session": s.name, "data": [{
            "remoteJid": chat_w, "id": chat_w, "archive": False}]})


def _learn_setting_once(s):
    """The setting arrives with a full copy of regular_low: asked for once per
    run, on a thread (it is a download; the message handler must not wait)."""
    if getattr(s, "_unarchive_setting_asked", False):
        return
    s._unarchive_setting_asked = True
    threading.Thread(target=app_state_mirror.ensure_seeded, args=(s, COLLECTION),
                     daemon=True, name="unarchive-setting").start()


def register_routes(r, *, session):
    """Wire `keep-chats-archived` to the aiohttp router `r`."""

    def failure(code, status):
        return web.json_response({"status": "error", "code": code}, status=status)

    def answer(keep):
        return web.json_response({"status": "success", "response": {"keepArchived": keep}})

    def ready(s):
        if not app_state_mirror.supported():
            return failure("keep_archived_unsupported", 501)
        if s.client is None or s.status != "CONNECTED":
            return failure("keep_archived_unavailable", 503)
        return None

    def read(s):
        if not app_state_mirror.ensure_seeded(s, COLLECTION):
            return "unread"
        return keep_archived(app_state_mirror.mirror_of(s).entries(SETTING))

    async def get_keep_archived(req):
        s = session(req)
        refused = ready(s)
        if refused is not None:
            return refused
        keep = await asyncio.get_running_loop().run_in_executor(None, read, s)
        if keep == "unread":
            return failure("keep_archived_unavailable", 503)
        return answer(keep)

    def apply(s, keep):
        app_state_mirror.send(s, COLLECTION, [([SETTING], VERSION, setting_value(keep), False)])
        return keep_archived(app_state_mirror.mirror_of(s).entries(SETTING))

    async def set_keep_archived(req):
        s = session(req)
        try:
            body = await req.json()
        except ValueError:
            body = {}
        keep = parse_request(body)
        if keep is None:
            return failure("invalid_keep_archived", 400)
        refused = ready(s)
        if refused is not None:
            return refused
        try:
            now = await asyncio.get_running_loop().run_in_executor(None, apply, s, keep)
        except Exception:
            log.exception("keep-chats-archived")
            return failure("keep_archived_failed", 502)
        log.info("keep chats archived set to %s", now)
        return answer(now)

    r.add_get("/api/{tok}/keep-chats-archived", get_keep_archived)
    r.add_post("/api/{tok}/keep-chats-archived", set_keep_archived)

