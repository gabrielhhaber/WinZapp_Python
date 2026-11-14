"""Group calls already going on: call/group/ongoing, call/ongoing,
call/participants and call/group/join, the routes WinZapp's WPPConnect side
answers from WhatsApp Web's own call store (callController.ts,
groupCallRuntime.ts; docs/reference/ongoing-group-calls.md).

Neonize has no such store. The bridge remembers the group calls it saw (an
offer that rang here, a call placed from here) and asks the server about each
one with the lobby query WhatsApp Web sends for a call the account is not in
(neonize's Calls.lobby, meowcaller's api/ongoing-group-join): the answer
lists every member with a state, and the call goes on while someone is
"connected". Joining sends the same <accept> WhatsApp Web's Join does.

The pure functions are at the top (tested in test_ongoing_calls.py).
"""
import asyncio
import logging
import threading
import time

from aiohttp import web

import msgmap
from groups import user_of
from redaction import redact

log = logging.getLogger("bridge")

# How long a call seen ringing is still worth asking about.
KEEP = 12 * 3600
# The client polls every 10 s; one lobby answer serves the group check and the
# Calls list of the same round.
LOBBY_FRESH = 8.0
# Who is in the call: WhatsApp Web's Connected participant state.
CONNECTED = "connected"


def device_jid(jid) -> str:
    """neonize JID -> 'user[:device]@server' with the device kept: the lobby
    query names the call's creator exactly as the offer did."""
    if jid is None or not getattr(jid, "User", ""):
        return ""
    device = int(getattr(jid, "Device", 0) or 0)
    return f"{jid.User}:{device}@{jid.Server}" if device else f"{jid.User}@{jid.Server}"


def person_jid(participant) -> str:
    """The address the client shows a participant by: the number when the
    roster has it, the LID otherwise; never a device."""
    for key in ("pn", "jid"):
        text = str(participant.get(key) or "")
        if text:
            user, _, server = text.partition("@")
            return f"{user.split(':')[0]}@{server}"
    return ""


def connected_rows(participants, mine, name_of) -> list:
    """[{jid, name, isSelf}] of the people connected to the call, the shape
    groupCallRuntime.participants returns. `mine` are the account's own users
    (number and LID digits); `name_of(jid, lid)` gives the saved name."""
    rows, seen = [], set()
    for p in participants or ():
        if not isinstance(p, dict) or p.get("state") != CONNECTED:
            continue
        jid = person_jid(p)
        if not jid:
            continue
        is_self = bool({user_of(jid), user_of(p.get("jid") or "")} & mine)
        key = "self" if is_self else jid
        if key in seen:
            continue
        seen.add(key)
        rows.append({"jid": jid, "name": "" if is_self else (name_of(jid, p.get("jid") or "") or ""),
                     "isSelf": is_self})
    return rows


#: meowcaller's roster states -> call/participants states (client/core/
#: call_participants.py STATES). Seen on a real lobby: connected, outgoing
#: (ringing), receipt (the ring reached the phone), invited (on the call's
#: list but out of it: a member who left shows so, and nobody rings them);
#: the others are whatsmeow's own call outcome words. An unknown state is
#: left out.
ROSTER_STATES = {
    CONNECTED: "connected",
    "outgoing": "ringing", "receipt": "ringing", "incoming": "ringing",
    "invited": "invited",
    "rejected": "declined", "declined": "declined",
    "terminated": "left", "left": "left",
    "timeout": "missed", "timed_out": "missed", "missed": "missed",
}


def all_rows(participants, mine, name_of) -> list:
    """[{jid, name, isSelf, state}] of everyone the roster lists, connected
    or not, as the phone lists a group call; connected readings win."""
    rows, index = [], {}
    for p in participants or ():
        if not isinstance(p, dict):
            continue
        state = ROSTER_STATES.get(str(p.get("state") or "").lower())
        jid = person_jid(p)
        if not state or not jid:
            continue
        is_self = bool({user_of(jid), user_of(p.get("jid") or "")} & mine)
        if is_self and state != "connected":
            continue
        key = "self" if is_self else jid
        if key in index:
            if state == "connected":
                rows[index[key]]["state"] = "connected"
            continue
        index[key] = len(rows)
        rows.append({"jid": jid, "name": "" if is_self else (name_of(jid, p.get("jid") or "") or ""),
                     "isSelf": is_self, "state": state})
    return rows


def is_going_on(lobby) -> bool:
    return bool(lobby) and any(isinstance(p, dict) and p.get("state") == CONNECTED
                               for p in lobby.get("participants") or ())


class Known:
    """The group calls this session saw, and their latest lobby answers."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._calls = {}        # id -> {"id", "creator", "chat", "video", "seen"}
        self._lobby = {}        # id -> (when, answer or None)

    def remember(self, call_id, creator, chat, video):
        if not call_id or not creator or not chat:
            return
        with self._lock:
            self._calls[call_id] = {"id": call_id, "creator": creator, "chat": chat,
                                    "video": bool(video), "seen": self._clock()}

    def forget(self, call_id):
        with self._lock:
            self._calls.pop(call_id, None)
            self._lobby.pop(call_id, None)

    def get(self, call_id):
        with self._lock:
            found = self._calls.get(call_id)
            return dict(found) if found else None

    def recent(self):
        """The calls still worth asking about, newest first."""
        now = self._clock()
        with self._lock:
            for old in [i for i, c in self._calls.items() if now - c["seen"] > KEEP]:
                self._calls.pop(old, None)
                self._lobby.pop(old, None)
            return sorted((dict(c) for c in self._calls.values()),
                          key=lambda c: c["seen"], reverse=True)

    def for_chat(self, chat):
        return next((c for c in self.recent() if c["chat"] == chat), None)

    def fresh_lobby(self, call_id):
        """(True, answer) when a lobby answer younger than LOBBY_FRESH exists."""
        with self._lock:
            found = self._lobby.get(call_id)
        if found and self._clock() - found[0] < LOBBY_FRESH:
            return True, found[1]
        return False, None

    def store_lobby(self, call_id, answer):
        with self._lock:
            self._lobby[call_id] = (self._clock(), answer)


def remember_offer(media, call_id, creator_jid, data):
    """An incoming offer of a group call (a group's, or a multiparty call
    filed under the caller's chat) becomes a call to ask about later."""
    if media is None or (not data.get("isGroup") and not data.get("isMultiparty")):
        return
    chat = data.get("groupJid") or msgmap.jid_wpp(data.get("peerJid") or "")
    known(media).remember(call_id, device_jid(creator_jid), chat, data.get("isVideo"))


def known(media) -> Known:
    """The registry of the session's call layer (CallMedia)."""
    found = getattr(media, "ongoing_calls", None)
    if found is None:
        found = media.ongoing_calls = Known()
    return found


def register_routes(r, *, session, ok):
    """Wire the ongoing group call routes to the aiohttp router `r`."""

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
        if media is None or not media.ongoing_available():
            return None, error("this neonize build does not find ongoing group calls", 501)
        if s.client is None or s.status != "CONNECTED":
            return None, error("Disconnected", 400)
        return media, None

    def lobby_of(media, call):
        """The server's answer about `call` (None once it is over); an answer
        from this polling round is reused. Runs on a thread."""
        registry = known(media)
        fresh, answer = registry.fresh_lobby(call["id"])
        if fresh:
            return answer
        answer = media.lobby(call["id"], call["creator"])
        registry.store_lobby(call["id"], answer)
        if answer is None:
            registry.forget(call["id"])
        return answer

    def live_entry(media, call):
        """{id, chatJid, isVideo} of a call still going on, or None."""
        if media.in_call(call["id"]):
            return {"id": call["id"], "chatJid": call["chat"], "isVideo": call["video"]}
        try:
            lobby = lobby_of(media, call)
        except Exception as e:
            log.info("ongoing call %s: lobby not answered (%s)", redact(call["id"]), type(e).__name__)
            return None
        if not is_going_on(lobby):
            return None
        return {"id": call["id"], "chatJid": call["chat"], "isVideo": bool(lobby.get("video"))}

    async def group_ongoing(req):
        s = session(req)
        media, refused = ready(s)
        if refused is not None:
            return refused
        group = str((await body_of(req)).get("groupJid") or "")
        if not group.endswith("@g.us"):
            return error("A group chat is required", 400)
        call = known(media).for_chat(group)
        entry = await in_thread(live_entry, media, call) if call else None
        return ok(status="success", response=None if entry is None else {
            "id": entry["id"], "groupJid": group, "isVideo": entry["isVideo"]})

    async def ongoing(req):
        s = session(req)
        media, refused = ready(s)
        if refused is not None:
            return refused

        def collect():
            return [e for e in (live_entry(media, c) for c in known(media).recent()) if e]
        return ok(status="success", response=await in_thread(collect))

    async def participants(req):
        s = session(req)
        media, refused = ready(s)
        if refused is not None:
            return refused
        call_id = str((await body_of(req)).get("callId") or "")
        if not call_id or len(call_id) > 512:
            return error("A call id is required", 400)
        mine = {user_of(s.me or ""), user_of(s.lid or "")} - {""}

        def name_of(jid, lid):
            found = s.db.contact_of(jid, lid)
            return found.get("name") or found.get("pushname") or ""

        def read():
            if media.in_call(call_id):
                roster = media.roster(call_id) or {}
                return roster.get("participants")
            call = known(media).get(call_id)
            lobby = lobby_of(media, call) if call else None
            return lobby.get("participants") if is_going_on(lobby) else None
        try:
            people = await in_thread(read)
        except Exception as e:
            log.info("call participants: %s", type(e).__name__)
            return error("Connected participants are unavailable")
        if people is None:
            return ok(status="success", response={"id": call_id, "available": False,
                                                  "participants": []})
        rows = await in_thread(all_rows, people, mine, name_of)
        return ok(status="success", response={"id": call_id, "available": True,
                                              "participants": rows})

    async def group_join(req):
        s = session(req)
        media, refused = ready(s)
        if refused is not None:
            return refused
        body = await body_of(req)
        call_id = str(body.get("callId") or "")
        chat = str(body.get("chatJid") or body.get("groupJid") or "")
        if not call_id or not chat:
            return error("Parameters callId and chatJid are required!", 400)
        call = known(media).get(call_id)
        if call is None or msgmap.jid_wpp(call["chat"]) != msgmap.jid_wpp(chat):
            return error("The selected group call is no longer available", 409)
        if media.live_call() and media.live_call() != call_id:
            return error("Another call is already active", 409)
        video = body.get("isVideo") is True
        try:
            joined = await in_thread(media.join_ongoing, call_id, call["creator"], call["chat"], video)
        except Exception as e:
            log.info("group call join refused (%s)", type(e).__name__)
            return error(f"WhatsApp did not join the selected group call: {e}", 409)
        return ok(status="success", response={
            "id": joined, "chatJid": call["chat"], "isVideo": video, "peerJid": call["chat"],
            "state": "CONNECTING", "outgoing": False})

    r.add_post("/api/{tok}/call/group/ongoing", group_ongoing)
    r.add_post("/api/{tok}/call/ongoing", ongoing)
    r.add_post("/api/{tok}/call/participants", participants)
    r.add_post("/api/{tok}/call/group/join", group_join)
