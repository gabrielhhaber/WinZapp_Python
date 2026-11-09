"""Calls in the bridge: what whatsmeow can do with a call.

It receives the offer and the end of a call (CallOffer, CallTerminate) and knows
how to reject a call that is ringing (RejectCall). With WinZapp's neonize build a
session also has `call_media` (call_media.py, meowcaller): then `call/offer`,
`call/group/offer`, `call/upgrade-video`, `call/accept`, `call/end`,
`call/ensure-ended`, `call/status` and `call/audio/enable` place, answer and
carry voice and video calls, one-to-one or to a group. Without it (an older
build) they answer 501, and so do video (without ffmpeg, or a DLL without
neonize 0007) and group calls (a DLL without 0007), instead of pretending to
have worked. `call/diagnostics` answers what the
bridge knows of its call layer (`diagnostics`): WPPConnect's version reads the
page's VoIP stack, which does not exist here.

The formats of the events are the ones the client reads in core/websocket_client.py
(`incomingcall` and `callstate`); those of the refusal, the ones in
main_window/calls.py (`/call/reject` with {"callId"} or {} and a response with
`via`/`call.state`).

The pure functions are at the top (tested in test_calls.py); `register_routes`
wires the routes and `register_events` wires neonize's events to a session.
"""
import asyncio
import logging
import threading
import time

from aiohttp import web

import system_messages
import msgmap
import ongoing_calls
from redaction import redact
from call_placement_errors import placement_failure

log = logging.getLogger("bridge")

# A ring lasts about 2 minutes at most; what is far past that is no longer
# ringing and would only take memory.
RING_TTL = 600
# A call's offer is kept for as long as an answered call may still go on: its
# end (<terminate>) says neither the group nor whether it was a video call.
OFFER_TTL = 6 * 3600
# Past this many seconds from the offer's own time, an incoming call nobody
# answered is surely over on the caller's side. The call layer keeps such a
# call open forever when its end never reaches us (an offer delivered from
# the offline queue whose caller gave up meanwhile: one stayed "awaiting
# Answer/Reject" for 9 minutes), so the bridge ends it then.
RING_LIMIT = 150

NO_VIDEO_REASON = "video calls are not available through this bridge"
NO_GROUP_REASON = "group calls are not available through this bridge"
# What needs the call layer (call_media.py); without it, 501.
MEDIA_ROUTES = ("call/offer", "call/group/offer", "call/upgrade-video", "call/accept",
                "call/end", "call/ensure-ended", "call/status", "call/audio/enable")
NO_MEDIA_REASON = "this neonize build cannot place, answer or carry calls"


def attribute(node, name) -> str:
    """The first attribute `name` of the node or of any descendant ('' if none).
    `node` is a neonize Node (Tag, Attrs[name/text], Nodes)."""
    for a in node.Attrs:
        if a.name == name and a.text:
            return a.text
    for child in node.Nodes:
        found = attribute(child, name)
        if found:
            return found
    return ""


def has_tag(node, tag) -> bool:
    """Whether the node or any descendant has the tag (a video offer brings <video/>)."""
    if node.Tag == tag:
        return True
    return any(has_tag(child, tag) for child in node.Nodes)


def _jid_text(jid) -> str:
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return msgmap.jid_wpp(f"{jid.User}@{jid.Server}")


def caller(meta) -> str:
    """The id of whoever called, in WPPConnect's format. Prefers the number (@c.us)
    over the @lid when whatsmeow delivers both; the client knows how to resolve both."""
    creator = _jid_text(meta.callCreator)
    alternative = _jid_text(meta.callCreatorAlt)
    if creator.endswith("@lid") and alternative and not alternative.endswith("@lid"):
        return alternative
    return creator or alternative


def offer_event(e, now) -> dict:
    """neonize CallOffer -> the `data` of `incomingcall` (and of `callstate`)."""
    meta = e.basicCallMeta
    group = msgmap.jid_wpp(attribute(e.data, "group-jid"))
    return {
        "event": "offer", "state": "INCOMING_RING",
        "peerJid": caller(meta), "id": meta.callID,
        "isVideo": has_tag(e.data, "video"),
        "isGroup": bool(group), "groupJid": group,
        # a group call filed under the caller's chat (no group-jid) is still
        # one people can join later
        "isMultiparty": has_tag(e.data, "group_info"),
        "timestamp": msgmap.seconds(meta.timestamp), "observedAt": int(now),
        # an offer that stayed queued while the app was away: the client ignores it
        "offerReceivedWhileOffline": bool(attribute(e.data, "offline")),
    }


def lobby_group(lobby) -> str:
    """The group the lobby answer names ('' if it names none): an invitation to
    a group call can come without `group-jid`, and then the call looked like a
    private call from its creator."""
    group = msgmap.jid_wpp(str((lobby or {}).get("groupJid") or ""))
    return group if group.endswith("@g.us") else ""


def resolve_multiparty_group(s, media, data, creator):
    """Asks the server which group an invitation without `group-jid` belongs to
    and tells everything that remembers the call (the ongoing calls of the
    group's chat, its later states, its end). Blocks on the network: run it on
    its own thread."""
    try:
        group = lobby_group(media.lobby(data["id"], ongoing_calls.device_jid(creator)))
    except Exception as e:
        log.info("multiparty call: lobby not answered (%s)", type(e).__name__)
        return ""
    if not group:
        log.info("multiparty call: the lobby named no group")
        return ""
    if not s.ringing_calls.set_group(data["id"], group):
        return ""                       # it ended while the lobby answered
    media.set_call_group(data["id"], group)
    ongoing_calls.known(media).remember(
        data["id"], ongoing_calls.device_jid(creator), group, data.get("isVideo"))
    log.info("multiparty call: the lobby named its group")
    return group


def ring_left(offer_ts, now, limit=RING_LIMIT) -> float:
    """Seconds until an unanswered incoming call is surely over (0 when it
    already is). A missing offer time counts from now."""
    started = offer_ts if offer_ts and offer_ts <= now else now
    return max(0.0, started + limit - now)


def drop_if_unanswered(media, call_id) -> bool:
    """Ends `call_id` when it still rings unanswered; True when it did."""
    if media is None or media.status(call_id)["state"] != "INCOMING_RING":
        return False
    log.info("call not answered and over: ended")
    return bool(media.hangup(call_id))


def with_offer(data, offer) -> dict:
    """The end of a call completed with what its offer said: <terminate> has
    no group-jid nor <video>, so a group call's end used to look like the
    caller's private call (its call record landed in the caller's chat as a
    missed private call)."""
    if not offer:
        return data
    group = data["groupJid"] or offer.get("groupJid") or ""
    return {**data, "groupJid": group, "isGroup": bool(group),
            "isVideo": bool(data["isVideo"] or offer.get("isVideo"))}


def with_placed_group(data, placed) -> dict:
    """The end of a group call placed from here, completed with what the
    placement knew. WhatsApp's <terminate> names neither the group nor that the
    call was ours, and its creator is whichever member ended it, so the end
    looked like a private call from that member: the client never matched it
    to the call it was making and the call record landed in that member's
    chat. `placed` is CallMedia.placed_group."""
    if not placed or not placed.get("groupJid"):
        return data
    group = placed["groupJid"]
    return {**data, "peerJid": group, "groupJid": group, "isGroup": True, "outgoing": True,
            "isVideo": bool(data["isVideo"] or placed.get("isVideo"))}


def with_own_call(data, mine, placed_peer):
    """The end of a call this account placed, told as one: outgoing, with the
    person called as its peer. WhatsApp's <terminate> names the call's creator,
    which for a call placed here (or from another device of this account) is
    this account itself. Told as a received call, the client wrote the call
    over its own peer and the record landed in the user's own chat, "as if
    they had called themselves" (reported 2026-11-09). `mine` holds this
    account's JIDs (number and LID, WPPConnect format); `placed_peer` is
    CallMedia.placed_peer for the call ("" when not placed here: then no peer
    at all, never this account)."""
    if data.get("isGroup") or data.get("outgoing") or data.get("peerJid") not in mine:
        return data
    return {**data, "outgoing": True, "peerJid": msgmap.jid_wpp(placed_peer) if placed_peer else ""}


def end_state(reason) -> tuple:
    """CallTerminate reason -> (event, state) that the client understands. Any
    state outside ringing already makes the client silence the alert; the
    refinement only changes what it records."""
    m = str(reason or "").lower()
    if "timeout" in m:
        return "timeout", "NOT_ANSWERED"
    if "reject" in m or "decline" in m:
        return "ended", "REJECTED"
    if "accept" in m:
        return "ended", "HANDLED_REMOTELY"
    return "ended", "ENDED"


def end_event(e, now) -> dict:
    """neonize CallTerminate -> the `data` of `incomingcall` and `callstate`."""
    meta = e.basicCallMeta
    event, state = end_state(e.reason)
    group = msgmap.jid_wpp(attribute(e.data, "group-jid"))
    return {
        "event": event, "state": state,
        "peerJid": caller(meta), "id": meta.callID,
        "isVideo": has_tag(e.data, "video"),
        "isGroup": bool(group), "groupJid": group,
        "outgoing": False,
        "timestamp": msgmap.seconds(meta.timestamp), "observedAt": int(now),
    }


def refine_end(data, accepted) -> dict:
    """An end with no reason ("ENDED") of a call that SOMEONE ANSWERED before is a
    call answered on another device (the phone): it is not the call the author
    canceled, and the client counts that one as missed. `accepted` is what
    `Pending` kept from the CallAccept: (server instant, local instant) or None.
    Returns `data` with the state and the duration (seconds of the call) fixed;
    any other end, including the one that brings a reason, stays as it came."""
    if data["state"] != "ENDED" or not accepted:
        return data
    return {**data, "state": "HANDLED_REMOTELY",
            "callDuration": call_duration(accepted, data["timestamp"], data["observedAt"])}


def call_duration(accepted, server_ended, local_ended) -> int:
    """Seconds between the CallAccept and the CallTerminate: by the server's
    instants when both exist (the clock here may be wrong), otherwise by the
    local ones. Never negative."""
    server, local = accepted
    if server and server_ended:
        return max(0, int(server_ended) - int(server))
    return max(0, int(local_ended) - int(local))


def call_chat(db, meta, group) -> str:
    """The key (whatsmeow format) of the chat where the call record goes: the
    group, or the peer. The peer may be stored by number or by LID; the form
    that already has a chat is used, and in its absence the number."""
    if group:
        return msgmap.jid_whatsmeow(group)
    forms = [msgmap.jid_whatsmeow(j) for j in (_jid_text(meta.callCreator),
                                               _jid_text(meta.callCreatorAlt)) if j]
    forms.sort(key=lambda j: j.endswith("@lid"))          # the number first
    for form in forms:
        if db.chat_exists(form):
            return form
    return forms[0] if forms else ""


def answered_outcome(data, answered):
    """(outcome, duration, video) of a received call this client answered
    (`answered`: CallMedia.answered_here), or None. The server sends us no
    CallAccept for our own answer, so without this the end of an answered call
    looked like the caller giving up ("Canceled", shown as missed). A call
    that turned into a video call is recorded as one."""
    if not answered or data["state"] not in ("ENDED", "HANDLED_REMOTELY"):
        return None
    duration = max(0, int(round(data["observedAt"] - answered["at"])))
    return "Completed", duration, bool(data["isVideo"] or answered.get("video"))


def finished_outcome(info):
    """(outcome, duration) of the call_log of a call that ended here, from
    call_media.finished_info, or None when the end says nothing the other layer
    (record_end, for a call that rang here) does not. A call that became active
    is "Completed" whoever placed it; one placed here and not answered is
    "Missed", "Rejected" or "Canceled" (I hung up first)."""
    active = info.get("activeAt")
    if active:
        return "Completed", max(0, int(round(info["endedAt"] - active)))
    if not info.get("outgoing"):
        return None
    return {"NOT_ANSWERED": ("Missed", 0), "REJECTED": ("Rejected", 0),
            "ENDED": ("Canceled", 0)}.get(info.get("state"))


def peer_chat(db, peer, phone_of_lid):
    """The key (whatsmeow format) of the chat of a call's peer: the form that
    already has a chat (number or LID), else the number."""
    peer = peer.split(":")[0] + "@" + peer.partition("@")[2] if ":" in peer.split("@")[0] else peer
    forms = [msgmap.jid_whatsmeow(peer)]
    other = phone_of_lid(peer) if peer.endswith("@lid") and phone_of_lid else ""
    if other:
        forms.append(msgmap.jid_whatsmeow(other))
    forms.sort(key=lambda j: j.endswith("@lid"))          # the number first
    for form in forms:
        if db.chat_exists(form):
            return form
    return forms[0] if forms else ""


def record_finished(s, info, now):
    """The record of a call that ended here, for what WhatsApp Web would have
    written had it run the call: the calls this client placed, and the ones it
    answered and hung up itself. record_end writes the ones that rang; a call
    already stored (same id) is left as it is. False if nothing was recorded."""
    outcome = finished_outcome(info)
    if outcome is None or not info.get("id"):
        return False
    outcome, duration = outcome
    chat = msgmap.jid_whatsmeow(info["group"]) if info.get("group") else peer_chat(
        s.db, info.get("peer") or "", getattr(s, "_phone_of_lid", None))
    if not chat or chat.endswith("@broadcast"):
        return False
    outgoing = bool(info.get("outgoing"))
    mine = {msgmap.jid_wpp(getattr(s, "me", "") or ""), msgmap.jid_wpp(getattr(s, "lid", "") or "")} - {""}
    peer = info.get("peer", "")
    if outgoing:
        creator = msgmap.jid_wpp(getattr(s, "me", "") or "")
    elif msgmap.jid_wpp(peer) in mine:
        return False                    # a call of this account from another device
    else:
        # a group call joined here names the group as its peer, not who started it
        creator = "" if peer == info.get("group") else peer
    w = system_messages.call_record(
        call_id=info["id"], chat_w=msgmap.jid_wpp(chat), creator_w=creator,
        outcome=outcome, video=info.get("video"), ts=int(info["endedAt"] - duration),
        duration=duration, from_me=outgoing)
    return system_messages.store_and_emit(s, chat, w, now)


def record_end(s, e, data, was_ringing, now, answered=None):
    """The end of a received call becomes the `call_log` record in the chat (what
    WhatsApp Web shows as "Missed voice call"). `data` is the `end_event`;
    `was_ringing` says whether the offer was still ringing here; `answered` is
    CallMedia.answered_here for the call. Does not record the call I made
    myself (from another device): it cannot be known who it was with just
    from the event. False if nothing was recorded."""
    if data.get("outgoing"):
        return False                    # the client records the calls it placed
    here = answered_outcome(data, answered)
    if here is not None:
        outcome, duration, video = here
        data = {**data, "callDuration": duration, "isVideo": video}
    else:
        outcome = system_messages.call_outcome(data["state"], was_ringing)
    creator = data["peerJid"]
    mine = {msgmap.jid_wpp(getattr(s, "me", "")), msgmap.jid_wpp(getattr(s, "lid", ""))} - {""}
    if outcome is None or not data["id"] or not creator or creator in mine:
        return False
    chat = call_chat(s.db, e.basicCallMeta, data["groupJid"])
    if not chat:
        return False
    w = system_messages.call_record(
        call_id=data["id"], chat_w=msgmap.jid_wpp(chat), creator_w=creator,
        outcome=outcome, video=data["isVideo"],
        ts=data["timestamp"] or int(now), duration=data.get("callDuration", 0))
    return system_messages.store_and_emit(s, chat, w, now)


class Pending:
    """Calls that are ringing: id -> the JID of whoever called, EXACTLY as it came
    in the offer (RejectCall requires that)."""

    def __init__(self):
        self._rings = {}
        self._accepted = {}
        self._offers = {}       # id -> ({"groupJid", "isVideo"}, when)

    def accept(self, call_id, server_ts, now):
        """The CallAccept: someone answered on another device. Our own answer
        brings none (CallMedia.answered_here says that one)."""
        self._accepted = {i: t for i, t in self._accepted.items()
                          if now - t[1] < RING_TTL}
        self._accepted[call_id] = (server_ts, now)

    def accepted(self, call_id):
        """(server instant, local instant) of the CallAccept, or None."""
        return self._accepted.get(call_id)

    def remember(self, call_id, creator, now, offer=None):
        self._rings = {i: t for i, t in self._rings.items()
                       if now - t[1] < RING_TTL}
        self._rings[call_id] = (creator, now)
        if offer:
            self._offers = {i: t for i, t in self._offers.items() if now - t[1] < OFFER_TTL}
            self._offers[call_id] = ({"groupJid": offer.get("groupJid") or "",
                                      "isVideo": bool(offer.get("isVideo"))}, now)

    def set_group(self, call_id, group):
        """The group of a call whose offer did not name it."""
        found = self._offers.get(call_id)
        if not found:
            return False                # the call is over: nothing to update
        self._offers[call_id] = ({**found[0], "groupJid": group}, found[1])
        return True

    def offer_of(self, call_id):
        """{"groupJid", "isVideo"} of the call's offer, or None."""
        found = self._offers.get(call_id)
        return found[0] if found else None

    def forget(self, call_id):
        self._rings.pop(call_id, None)
        self._accepted.pop(call_id, None)
        self._offers.pop(call_id, None)

    def creator_of(self, call_id):
        found = self._rings.get(call_id)
        return found[0] if found else None

    def to_refuse(self, body) -> list:
        """The ids to refuse for the `call/reject` body: the requested `callId`, or
        (empty body, the app only knows a call is ringing) all that ring. Empty
        list if the request matches none."""
        requested = str((body or {}).get("callId") or "")
        if requested:
            return [requested] if requested in self._rings else []
        return list(self._rings)


def refusal_response(ids) -> dict:
    """The `response` of `call/reject`: the client only reads `via` and `call.state`."""
    return {"via": "neonize", "call": {"state": "REJECTED"}, "rejected": len(ids)}


def register_events(cli, s):
    """Wires the call offer and end of the neonize client `cli` to the session `s`
    (needs `s.name` and `s.emit`). Called on neonize's thread."""
    import neonize.events as nev

    s.ringing_calls = Pending()

    def notify(data):
        # incomingcall opens/closes the alert; callstate follows the call in progress
        # (WPPConnect emits both for each change, see createSessionUtil.ts)
        s.emit("incomingcall", {"session": s.name, "data": data})
        s.emit("callstate", {"session": s.name, "data": data})

    @cli.event(nev.CallOfferEv)
    def _offer(c, e):
        try:
            data = offer_event(e, time.time())
            if not data["id"]:
                return
            s.ringing_calls.remember(data["id"], e.basicCallMeta.callCreator, time.time(), data)
            media = getattr(s, "call_media", None)
            if media is not None:
                media.remember_offer(data)
                ongoing_calls.remember_offer(media, data["id"], e.basicCallMeta.callCreator, data)
            log.info("call received (video=%s group=%s multiparty=%s)",
                     data["isVideo"], data["isGroup"], data["isMultiparty"])
            notify(data)
            if media is not None and data["isMultiparty"] and not data["groupJid"]:
                threading.Thread(target=resolve_multiparty_group,
                                 args=(s, media, data, e.basicCallMeta.callCreator),
                                 daemon=True, name="call-group").start()
            if media is not None:
                timer = threading.Timer(ring_left(data["timestamp"], time.time()),
                                        drop_if_unanswered, (media, data["id"]))
                timer.daemon = True
                timer.start()
        except Exception:
            log.exception("call offer")

    @cli.event(nev.CallAcceptEv)
    def _answered(c, e):
        try:
            if e.basicCallMeta.callID:
                s.ringing_calls.accept(
                    e.basicCallMeta.callID, msgmap.seconds(e.basicCallMeta.timestamp), time.time())
        except Exception:
            log.exception("call answered")

    @cli.event(nev.CallTerminateEv)
    def _end(c, e):
        try:
            data = end_event(e, time.time())
            data = with_offer(data, s.ringing_calls.offer_of(data["id"]))
            placed_media = getattr(s, "call_media", None)
            mine = {msgmap.jid_wpp(getattr(s, "me", "") or ""),
                    msgmap.jid_wpp(getattr(s, "lid", "") or "")} - {""}
            data = with_own_call(data, mine, placed_media.placed_peer(data["id"])
                                 if placed_media is not None else "")
            if not data["outgoing"]:
                # "answered on another device" is about a call received here;
                # the CallAccept of a call placed here is the person answering
                data = refine_end(data, s.ringing_calls.accepted(data["id"]))
            if placed_media is not None:
                data = with_placed_group(data, placed_media.placed_group(data["id"]))
            was_ringing = s.ringing_calls.creator_of(data["id"]) is not None
            s.ringing_calls.forget(data["id"])
            media = getattr(s, "call_media", None)
            answered = media.answered_here(data["id"]) if media is not None else None
            log.info("call ended (%s%s)", data["state"], ", answered here" if answered else "")
            notify(data)
        except Exception:
            log.exception("call end")
            return
        try:
            record_end(s, e, data, was_ringing, time.time(), answered)
        except Exception:
            log.exception("call record %s", redact(data["id"]))


def diagnostics(media, connected, ringing) -> dict:
    """The body of `call/diagnostics`: the call layer as the bridge sees it."""
    return {"engine": "neonize", "callLayer": media is not None, "connected": bool(connected),
            "voice": media is not None, "video": capabilities(media)["video"],
            "groupCalls": capabilities(media)["groupCalls"],
            "liveCall": bool(media is not None and media.live_call()),
            "ringing": int(ringing)}


def capabilities(media) -> dict:
    """What this session's calls can do (send-capabilities, diagnostics)."""
    return {"video": media is not None and getattr(media, "video", None) is not None,
            "groupCalls": media is not None and bool(getattr(media, "group_calls", False))}


def register_routes(r, *, session, ok):
    """Wire the call routes to the aiohttp router `r`."""

    def error(message, status=500, reason=None):
        body = {"status": "error", "message": str(message)}
        if reason is not None:
            body["reasonCode"] = reason
        return web.json_response(body, status=status)

    async def reject(req):
        s = session(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        if s.client is None or s.status != "CONNECTED":
            return error("Disconnected", 400)
        ringing = getattr(s, "ringing_calls", None)
        ids = ringing.to_refuse(body) if ringing is not None else []
        if not ids:
            return error("call not found", 404)
        loop = asyncio.get_running_loop()
        media = getattr(s, "call_media", None)
        try:
            for call_id in ids:
                # the call layer holds the call too: its Reject also ends its own record
                if media is not None and media.knows(call_id):
                    await loop.run_in_executor(None, media.hangup, call_id)
                else:
                    await loop.run_in_executor(
                        None, s.client.reject_call, ringing.creator_of(call_id), call_id)
                ringing.forget(call_id)
        except Exception as e:
            log.exception("reject call")
            return error(e)
        return ok(status="success", response=refusal_response(ids))

    async def body_of(req):
        try:
            body = await req.json()
        except Exception:
            body = {}
        return body if isinstance(body, dict) else {}

    def media_route(action, needs_connection=True):
        """A call/* route that needs the session's call layer."""
        async def route(req):
            s = session(req)
            body = await body_of(req)
            media = getattr(s, "call_media", None)
            if media is None:
                return error(NO_MEDIA_REASON, 501)
            if needs_connection and (s.client is None or s.status != "CONNECTED"):
                return error("Disconnected", 400)
            return await action(media, body)
        return route

    async def in_thread(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    async def placed(place, *args):
        try:
            call_id = await in_thread(place, *args)
        except ValueError as e:
            return error(e, 400)
        except Exception as e:
            # the text can carry the number: only its kind goes to the log and back
            log.warning("call not placed: %s", type(e).__name__)
            status, reason = placement_failure(e)
            return error("the call could not be placed", status, reason)
        state = place.__self__.status(call_id)["state"]
        return ok(status="success", response={"id": call_id, "via": "neonize",
                                              "call": {"state": state}})

    async def offer(media, body):
        video = body.get("isVideo") is True
        if video and not capabilities(media)["video"]:
            return error(NO_VIDEO_REASON, 501)
        return await placed(media.dial, str(body.get("to") or ""), video)

    async def group_offer(media, body):
        video = body.get("isVideo") is True
        if not capabilities(media)["groupCalls"]:
            return error(NO_GROUP_REASON, 501)
        if video and not capabilities(media)["video"]:
            return error(NO_VIDEO_REASON, 501)
        if not body.get("groupJid"):
            return error("Parameter groupJid is required!", 400)
        return await placed(media.group_dial, str(body["groupJid"]), video)

    async def upgrade_video(media, body):
        if not capabilities(media)["video"]:
            return error(NO_VIDEO_REASON, 501)
        try:
            done, already = await in_thread(media.upgrade_video, str(body.get("callId") or ""))
        except LookupError as e:
            return error(e, 409)
        if not done:
            return error("the call could not be upgraded to video")
        return ok(status="success", response={"handled": True, "via": "neonize",
                                              "alreadyVideo": already,
                                              "call": {"state": "ACTIVE", "isVideo": True}})

    async def accept(media, body):
        if not await in_thread(media.answer, str(body.get("callId") or "")):
            return error("call not found", 404)
        return ok(status="success", response={"via": "neonize", "call": {"state": "ACTIVE"}})

    async def end(media, body):
        # Also reachable while the socket is down (a network drop in the middle
        # of a call): ending is then local, never a "Disconnected" the user
        # cannot act on.
        try:
            ended = await in_thread(media.hangup, str(body.get("callId") or ""))
        except Exception:
            log.warning("call end failed locally", exc_info=True)
            ended = False
        return ok(status="success", response={"via": "neonize", "ended": bool(ended),
                                              "call": {"state": "ENDED"}})

    async def status(media, body):
        return ok(status="success", response=media.status(str(body.get("callId") or "")))

    async def audio_enable(media, body):
        return ok(status="success", response={"enabled": True})

    async def call_diagnostics(req):
        s = session(req)
        ringing = getattr(s, "ringing_calls", None)
        count = len(ringing.to_refuse({})) if ringing is not None else 0
        return ok(status="success", response=diagnostics(
            getattr(s, "call_media", None), s.client is not None and s.status == "CONNECTED",
            count))

    r.add_get("/api/{tok}/call/diagnostics", call_diagnostics)
    r.add_post("/api/{tok}/call/reject", reject)
    r.add_post("/api/{tok}/reject-call", reject)
    for route, action in (("call/offer", offer), ("call/group/offer", group_offer),
                          ("call/upgrade-video", upgrade_video), ("call/accept", accept),
                          ("call/end", end), ("call/ensure-ended", end),
                          ("call/status", status), ("call/audio/enable", audio_enable)):
        r.add_post("/api/{tok}/" + route, media_route(
            action, needs_connection=action is not end))
