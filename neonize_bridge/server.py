"""Bridge server: speaks the WPPConnect Server API (HTTP + Socket.IO) and, behind
it, uses neonize. Phase 0: login (QR and code), state, host-device, send text
and receive text. The WinZapp client points here through the "custom server"
option (wpp_custom_api).

Usage:  .venv\\Scripts\\python server.py [--port 6300] [--folder data]
"""
import argparse
import asyncio
import base64
import json
import io
import logging
import os
import threading
import time

import segno
import socketio
from aiohttp import web

import audio_message
import channels
import administration
import authentication
import call_media
import calls
import session_lifecycle
import video_message
import comments
import comment_routes
import inputs
import legacy_layout
import loop_watch
import ongoing_calls
import group_events
import contact_events
import app_state_mirror
import app_state_recovery
import labels
import communities
import contacts
import chat_listing
import chats
import chat_aliases
import chat_timers
import read_receipts
import chat_remaining
import groups
import groups_extra
import history_progress
import cleanup
import msgmap
import pairing
import commerce_routes
import profile_privacy
import presence
import pairing_repair
import group_receipts
import message_rules
import responses
import remaining
import redaction
import transfers
from redaction import redact
from store import Store

log = logging.getLogger("bridge")


QR_BORDER = 6       # white margin in modules (the standard's minimum is 4)
QR_SCALE = 8        # whole pixels per module: no smoothing, which ruins the reading


def qr_data_url(text: str) -> str:
    """Pure black on pure white, error level L (the fewest modules, so the
    largest ones for a phone camera) and a wide margin: what TeleZapp scans
    reliably. The pairing text is long, a higher level only makes it denser."""
    buf = io.BytesIO()
    segno.make_qr(text, error="l").save(
        buf, kind="png", scale=QR_SCALE, border=QR_BORDER, dark="#000000", light="#ffffff")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


LOOP_WATCH = web.AppKey("loop_watch", asyncio.Task)

HISTORY_DAYS = 180     # how much history to ask the phone for on pairing (0 = default)


def device_props(days):
    """DeviceProps: the name that shows on the phone and how much history to ask for."""
    from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
    props = DeviceProps(os="WinZapp", platformType=DeviceProps.DESKTOP)
    if days:
        props.requireFullSync = True
        props.historySyncConfig.fullSyncDaysLimit = days
        props.historySyncConfig.fullSyncSizeMbLimit = 102400
        props.historySyncConfig.storageQuotaMb = 102400
    return props


def fix_poll_vote_decryption(client):
    """neonize 0.5.2 forgot `DecryptPollVote` in the list of the functions whose
    return (a Go byte pointer) becomes `bytes`: without that, decrypting a vote
    raises TypeError. Gives it the same treatment as the others. Runs once."""
    import ctypes
    go = next((getattr(client, a) for a in dir(client) if a.endswith("__client")), None)
    func = getattr(go, "DecryptPollVote", None)
    if func is None or getattr(func, "errcheck", None) is not None:
        return False

    def consume(result, _func, arguments):
        if not result:
            return b""
        try:
            content = result.contents
            if not content.ptr or content.size == 0:
                return b""
            return ctypes.string_at(content.ptr, content.size)
        finally:
            go.FreeBytesStruct(result)
    func.errcheck = consume
    return True


def call_layer_on() -> bool:
    """Turns on the voice call layer of WinZapp's neonize build for the clients
    created from now on (call_media.py); False when this build has none."""
    try:
        import neonize.calls as layer
    except ImportError:
        return False
    if not layer.available():
        return False
    # Explicit experimental opt-in; older modules/DLLs retain the legacy path.
    from call_audio_mode import configure_call_audio
    configure_call_audio(layer, os.environ.get("WINZAPP_EXPERIMENTAL_CALL_PCM48") == "1")
    return True


def new_call_media(name, uuid, emit, phone_of_lid):
    """The session's calls: voice always; video when the DLL has it (neonize
    0008) and there is an ffmpeg to encode and decode it; groups with 0008."""
    import call_video
    import neonize.calls as layer
    import video_codec
    layer_calls = layer.Calls(uuid)
    has_video = layer.video_available()
    codec = video_codec.FfmpegCodec.available() if has_video else None
    if has_video and codec is None:
        log.warning("call video: no ffmpeg on the PATH; video calls stay off")
    video = call_video.CallVideo(name, layer_calls, emit, codec) if codec else None
    return call_media.CallMedia(name, layer_calls, emit, phone_of_lid, video=video,
                                group_calls=has_video)


def jid_text(jid) -> str:
    """neonize JID -> 'user@server' (empty if there is none)."""
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


class Session(chat_aliases.ChatAlias, chat_timers.ChatTimers, read_receipts.ReadReceipts,
              group_receipts.GroupReceipts):
    """One account: the neonize client, the state in WPPConnect's vocabulary and
    the bridge between neonize's thread and the aiohttp loop."""

    def __init__(self, name, folder, sio, loop):
        self.name = name
        self.folder = folder
        self.sio = sio
        self.loop = loop
        self.client = None
        self.simulate_code = False
        self.online = False         # app window in focus (set-online-presence)
        self.open_chat = ""         # chat open in the app (the last send-seen)
        self.thread = None
        self.lifecycle = session_lifecycle.Lifecycle()   # state + generation (session_lifecycle.py)
        self._lifecycle_lock = threading.RLock()    # begin/stop one at a time
        # one request for a fresh app-state copy per collection, one app-state
        # operation at a time, and the read notices still to send (app_state_recovery)
        self._recovery_gate = app_state_recovery.RecoveryGate()
        self._app_state_lock = threading.RLock()
        self._pending_reads = app_state_recovery.PendingReads()
        self._flushing_reads = threading.Lock()
        self.on_unlink = None       # called when the phone removes the device
        self.pairing_repairs = pairing_repair.Budget()
        self.urlcode = None
        self.me = ""                # own JID (user@s.whatsapp.net)
        self.lid = ""
        self.lid_device = ""        # own LID with this device: a group call placed here
        self.call_media = None      # call_media.CallMedia when this build carries calls
        os.makedirs(folder, exist_ok=True)
        self.db = Store(os.path.join(folder, "bridge.db"))
        done = self.db.apply_revocations()
        if done:
            log.info("%d delete-for-everyone message(s) applied to the store", done)

    @property
    def status(self):
        """CLOSED INITIALIZING QRCODE CONNECTED notLogged terminalFailure"""
        return self.lifecycle.status

    @status.setter
    def status(self, value):
        self.lifecycle.status = value

    # ------------------------------------------------------------- reactions
    def target_serial(self, chat_w, group, target_id, i_reacted, key_from_me, participant):
        """Serialized id (WPPConnect format) of the target message of a reaction.
        Uses the stored message; otherwise deduces: key.fromMe comes from whoever reacted."""
        stored_message = self.db.wpp_message(target_id)
        if stored_message is not None:
            return stored_message["id"]["_serialized"]
        own = key_from_me if i_reacted else not key_from_me
        if group:
            own = bool(participant) and participant == msgmap.jid_wpp(self.me)
        serial = f"{'true' if own else 'false'}_{chat_w}_{target_id}"
        if group and not own and participant:
            serial += f"_{participant}"
        return serial

    def _reaction_received(self, chat, o, e):
        """True if the message was a reaction (stores it and tells the app)."""
        data = msgmap.reaction(e.Message)
        if data is None:
            return False
        target_id, emoji, key_from_me, participant = data
        chat_w = msgmap.jid_wpp(chat)
        group = chat_w.endswith("@g.us")
        i_reacted = bool(o.IsFromMe)
        sender_w = msgmap.jid_wpp(jid_text(o.Sender))
        who = "me" if i_reacted else sender_w
        ts = msgmap.seconds(e.Info.Timestamp)
        self.db.react(target_id, who, emoji, ts)
        reaction_serial = f"{'true' if i_reacted else 'false'}_{chat_w}_{e.Info.ID}"
        if group:
            reaction_serial += f"_{sender_w}"
        self.emit("onreactionmessage", {"session": self.name, "response": {
            "id": reaction_serial,
            "msgId": self.target_serial(chat_w, group, target_id, i_reacted,
                                        key_from_me, participant),
            "reactionText": emoji, "text": emoji, "timestamp": ts}})
        return True

    # --------------------------------------------------------------- status
    def status_wpp(self):
        """Response of GET /statuses: the statuses of the last 24 h stored,
        mine and the contacts' grouped by who posted."""
        limit = time.time() - 86400
        mine, by_author = [], {}
        for w in self.db.messages("status@broadcast", 1000):
            if w["t"] < limit or w.get("type") in ("revoked", "protocol"):
                continue
            if w.get("fromMe"):
                mine.append(w)
                continue
            author = w.get("author") or w.get("from")
            if author and author != "status@broadcast":
                by_author.setdefault(author, []).append(w)
        # myStatusReady tells the client that an empty list is WhatsApp's final
        # word (it erases the cached stories that are not in it). The store only
        # has what the bridge saw: empty does not prove there are no stories.
        return {"myStatus": mine, "myStatusReady": bool(mine),
                "contacts": [{"jid": a, "msgs": ms} for a, ms in by_author.items()]}

    def delete_status(self, status_id):
        """Deletes a status of mine for everyone."""
        if self.client is None or self.status != "CONNECTED":
            return False
        from neonize.utils import build_jid
        me_u, _, me_srv = self.me.partition("@")
        try:
            self.client.revoke_message(build_jid("status", "broadcast"),
                                       build_jid(me_u.split(":")[0], me_srv), status_id)
        except Exception:
            log.exception("delete status")
            return False
        self.db.change_message(status_id, type="revoked", body="")
        return True

    # -------------------------------------------------------------- pinned
    def _pin_received(self, chat, e):
        """True if the message was the notice of pinning or unpinning a message."""
        data = msgmap.pin(e.Message)
        if data is None:
            return False
        target, pin, duration = data
        self.db.pin_message(target, chat, pin, msgmap.seconds(e.Info.Timestamp), duration)
        return True

    def pin_message(self, serial, pin, duration=7 * 86400):
        """Pins or unpins a message in the chat (visible to everyone)."""
        from_me, conversation, mid, author = self.id_parts(serial)
        if self.client is None or self.status != "CONNECTED":
            return False
        from neonize.proto.waCommon.WACommon_pb2 import MessageKey
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
            Message, MessageContextInfo, PinInChatMessage)
        from neonize.utils import build_jid
        key = MessageKey(remoteJID=conversation, fromMe=from_me, ID=mid)
        if conversation.endswith("@g.us") and not from_me and author:
            key.participant = author
        message = Message(pinInChatMessage=PinInChatMessage(
            key=key,
            type=PinInChatMessage.PIN_FOR_ALL if pin else PinInChatMessage.UNPIN_FOR_ALL,
            senderTimestampMS=int(time.time() * 1000)))
        if pin:
            message.messageContextInfo.CopyFrom(MessageContextInfo(
                messageAddOnDurationInSecs=duration,
                messageAddOnExpiryType=MessageContextInfo.STATIC))
        u, _, srv = conversation.partition("@")
        try:
            self.client.send_message(build_jid(u, srv), message)
        except Exception:
            log.exception("pin message")
            return False
        self.db.pin_message(mid, conversation, pin, int(time.time()), duration if pin else 0)
        return True

    # -------------------------------------------------------------- edit
    def mark_edited(self, target, chat_w, edit_id, from_me, **fields):
        """Puts the new text in the stored message, with the edited mark
        (latestEditMsgKey, which the app reads), and returns the message."""
        self.db.change_message(
            target, **fields,
            latestEditMsgKey={"id": edit_id, "remote": chat_w, "fromMe": bool(from_me)},
            latestEditSenderTimestampMs=int(time.time() * 1000))
        return self.db.wpp_message(target)

    def _edit_received(self, chat, o, e):
        """True if the message was the notice that someone edited a message."""
        data = msgmap.edit(e.Message)
        if data is None:
            return False
        target, new = data
        original = self.db.wpp_message(target)
        if original is None:           # a message from before the bridge: nothing to replace
            return True
        w = msgmap.build(
            chat=chat, from_me=bool(original.get("fromMe")), id=target,
            sender=self.me if original.get("fromMe") else jid_text(o.Sender),
            ts=original["t"], message=new)
        if w is None:
            return True
        fields = {"body": w.get("body", "")} if w.get("type") == "chat" \
            else {"caption": w.get("caption", "")}
        edited = self.mark_edited(target, msgmap.jid_wpp(chat), e.Info.ID,
                                  o.IsFromMe, **fields)
        self.emit("received-message", {"session": self.name, "response": edited})
        return True

    def edit(self, serial, text, mentions=()):
        """Edits a message of mine (WhatsApp only accepts it for about 15 minutes)."""
        from_me, conversation, mid, _ = self.id_parts(serial)
        original = self.db.wpp_message(mid)
        if not from_me or original is None or self.client is None \
                or self.status != "CONNECTED":
            return False
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
            ContextInfo, ExtendedTextMessage, Message)
        from neonize.utils import build_jid
        u, _, srv = conversation.partition("@")
        record = self.db.media_record(mid)
        if record and original.get("type") != "chat":        # media caption
            new = Message.FromString(record)
            field = msgmap.media_field(new)
            if not field:
                return False
            getattr(new, field).caption = text
            fields = {"caption": text}
        else:
            if mentions:
                ctx = ContextInfo()
                ctx.mentionedJID.extend(msgmap.jid_whatsmeow(j) for j in mentions)
                new = Message(extendedTextMessage=ExtendedTextMessage(
                    text=text, contextInfo=ctx))
            else:
                new = Message(conversation=text)
            fields = {"body": text}
        try:
            self.client.edit_message(build_jid(u, srv), mid, new)
        except Exception:
            log.exception("edit message")
            return False
        self.mark_edited(mid, msgmap.jid_wpp(conversation), mid, True, **fields)
        return True

    def message_to_forward(self, serial, keep_voice=False):
        """The stored message, ready to be sent as forwarded, or None if the
        type is not supported yet."""
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
        _, _, mid, _ = self.id_parts(serial)
        original = self.db.wpp_message(mid)
        if original is None:
            return None
        record = self.db.media_record(mid)
        if record:
            msg = Message.FromString(record)
            field = msgmap.media_field(msg)
            if not field:
                return None
            sub = getattr(msg, field)
            if hasattr(sub, "caption") and original.get("caption"):
                sub.caption = original["caption"]
            if field == "audioMessage" and not keep_voice:
                sub.PTT = False
            sub.contextInfo.isForwarded = True
            sub.contextInfo.forwardingScore = 1
            return msg
        import forwarding
        return forwarding.plain_forward(original)

    # ------------------------------------------------------------ polls
    def _vote_received(self, o, e):
        """True if the message was a vote in a poll (decrypts and stores it). The
        vote comes encrypted with the poll's secret, which neonize keeps when the
        question goes through it: votes of earlier polls cannot be read."""
        target = msgmap.vote(e.Message)
        if target is None:
            return False
        original = self.db.wpp_message(target) or {}
        names = [x["name"] for x in original.get("pollOptions") or []]
        try:
            fix_poll_vote_decryption(self.client)
            datum = self.client.decrypt_poll_vote(e)
        except Exception as error:
            log.info("vote I could not decrypt (%s: %s)", type(error).__name__,
                     str(error)[:100])
            return True
        who = "me" if o.IsFromMe else msgmap.jid_wpp(jid_text(o.Sender))
        self.db.vote(target, who, msgmap.vote_indexes(names, datum.selectedOptions),
                     msgmap.seconds(e.Info.Timestamp))
        return True

    def poll_wpp(self, serial):
        """The poll and its votes, or None if the message is not a poll."""
        _, _, mid, _ = self.id_parts(serial)
        original = self.db.wpp_message(mid)
        if not original or original.get("type") != "poll_creation":
            return None
        options = [{"name": x["name"], "localId": x.get("localId", i)}
                   for i, x in enumerate(original.get("pollOptions") or [])]
        return mid, options, self.db.votes_of(mid)

    def votes_wpp(self, serial):
        """Entry of /poll-votes: {voters, options:[{name, localId, votes, mine}]}."""
        found = self.poll_wpp(serial)
        if found is None:
            return None
        _, options, vote_rows = found
        counts = {}
        my_ids = set()
        voters = 0
        for who, ids in vote_rows:
            if not ids:
                continue
            voters += 1
            for i in ids:
                counts[i] = counts.get(i, 0) + 1
                if who == "me":
                    my_ids.add(i)
        return {"voters": voters, "options": [
            {**op, "votes": counts.get(op["localId"], 0),
             "mine": op["localId"] in my_ids} for op in options]}

    def voters_wpp(self, serial):
        """Response of /poll-voters: who voted for what."""
        found = self.poll_wpp(serial)
        if found is None:
            return {"ok": False, "error": "poll_voters_unavailable"}
        _, options, vote_rows = found
        me = msgmap.jid_wpp(self.me)
        voters = []
        for who, ids in vote_rows:
            if not ids:
                continue
            sender = me if who == "me" else who
            name = self.db.contact_of(msgmap.jid_whatsmeow(sender)) if who != "me" else {}
            voters.append({"sender": sender, "mine": who == "me",
                           "name": name.get("name") or name.get("pushname") or "",
                           "localIds": ids})
        return {"ok": True, "options": options, "voters": voters}

    def vote_in_poll(self, serial, local_ids):
        """Votes (or, with no ids, removes the vote) in a poll."""
        found = self.poll_wpp(serial)
        if found is None or self.client is None or self.status != "CONNECTED":
            return False
        mid, options, _ = found
        names_by_id = {op["localId"]: op["name"] for op in options}
        if not all(i in names_by_id for i in local_ids):
            return False
        from neonize.proto.Neonize_pb2 import MessageInfo, MessageSource
        from neonize.utils import build_jid
        from_me, conversation, _, author = self.id_parts(serial)
        group = conversation.endswith("@g.us")
        owner = self.me if from_me else (author or conversation)

        def jid(text):
            u, _, srv = text.partition("@")
            return build_jid(u.split(":")[0], srv)
        empty = build_jid("", "")
        source = MessageSource(
            Chat=jid(conversation), Sender=jid(owner), IsFromMe=from_me, IsGroup=group,
            SenderAlt=empty, RecipientAlt=empty, BroadcastListOwner=empty)
        info = MessageInfo(MessageSource=source, ID=mid, ServerID=0, Type="", Pushname="",
                           Timestamp=0, Category="", Multicast=False, MediaType="", Edit="")
        try:
            self.client.send_message(jid(conversation), self.client.build_poll_vote(
                info, [names_by_id[i] for i in local_ids]))
        except Exception:
            log.exception("vote in poll")
            return False
        self.db.vote(mid, "me", sorted(local_ids), int(time.time()))
        return True

    def react(self, serial, emoji):
        """Reacts (or, with an empty emoji, removes the reaction) to a message."""
        if self.client is None or self.status != "CONNECTED":
            return False
        from neonize.utils import build_jid
        from_me, conversation, mid, author = self.id_parts(serial)
        author_jid = self.me if from_me else (author or conversation)
        cu, _, csrv = conversation.partition("@")
        au, _, asrv = author_jid.partition("@")
        try:
            msg = self.client.build_reaction(
                build_jid(cu, csrv), build_jid(au.split(":")[0], asrv), mid, emoji)
            self.client.send_message(build_jid(cu, csrv), msg)
        except Exception:
            log.exception("react")
            return False
        self.db.react(mid, "me", emoji, int(time.time()))
        return True

    def reactions_wpp(self, serial):
        """Response of GET /reactions/<id>: grouped by emoji, in WPPConnect's
        format (senders with senderUserJid and reactionText)."""
        _, _, mid, _ = self.id_parts(serial)
        me = msgmap.jid_wpp(self.me)
        groups, own = {}, ""
        for who, emoji in self.db.reactions_of(mid):
            jid = me if who == "me" else who
            if who == "me":
                own = emoji
            groups.setdefault(emoji, []).append(
                {"senderUserJid": jid, "reactionText": emoji})
        items = [{"aggregateEmoji": emoji, "hasReactionByMe": any(
                      x["senderUserJid"] == me for x in senders),
                  "senders": senders} for emoji, senders in groups.items()]
        return {"reactionByMe": own, "reactions": items}

    # ------------------------------------------------------------ events
    def emit(self, event, data):
        # only this session's socket (the connect room); without a room, every
        # connected socket would receive the events of all the accounts
        asyncio.run_coroutine_threadsafe(
            self.sio.emit(event, data, room=self.name), self.loop)

    # --------------------------------------------------------- lifecycle
    def _thread_alive(self):
        return bool(self.thread and self.thread.is_alive())

    def begin(self, phone_number=None, repairing=False):
        """Brings the session up. Blocks until the old thread finishes (only when
        there is one): call it outside the aiohttp loop. True if a new thread came up.
        A start the user asked for gives the pairing repairs back; the one a
        repair makes does not."""
        with self._lifecycle_lock:
            decision = session_lifecycle.decide_start(
                self._thread_alive(), self.status, phone_number)
            if decision == session_lifecycle.IGNORE:
                return False
            if decision == session_lifecycle.RESTART:
                # a stalled pairing, or a closed session whose thread is still
                # finishing: neonize's connect() only returns after stop()
                self.stop()
                self.thread.join(session_lifecycle.OLD_THREAD_DEADLINE)
                if self._thread_alive():
                    log.warning("the old thread of session %s did not finish; not starting another",
                                redact(self.name))
                    return False
            if not repairing:
                self.pairing_repairs.reset()
            generation = self.lifecycle.open()
            self.thread = threading.Thread(
                target=self._run, args=(phone_number, generation), daemon=True, name="neonize")
            self.thread.start()
            return True

    def _apply(self, generation, event):
        """Applies an event to the state if `generation` is still the current one (None if not)."""
        return self.lifecycle.apply(generation, event)

    def _stop_in_background(self, client):
        """stop() outside the event's thread: Go may be waiting for this handler
        to return, and Stop waits for Go."""
        def stop():
            try:
                client.stop()
            except Exception:
                log.exception("stop")
        threading.Thread(target=stop, daemon=True, name="neonize-stop").start()

    def _on_connect(self, generation, c, phone_number):
        if not self.lifecycle.current(generation):
            return
        try:
            me = c.get_me()
            self.me = jid_text(me.JID)
            self.lid = jid_text(me.LID)
            self.lid_device = ongoing_calls.device_jid(me.LID)
        except Exception:
            log.exception("get_me")
        if self._apply(generation, session_lifecycle.CONNECTED_EVENT) is None:
            return
        self.urlcode = None
        self.pairing_repairs.reset()
        log.info("connected")
        if not (phone_number and self.simulate_code):   # the simulation notifies after the code
            self.emit("session-logged", {"session": self.name, "status": True})
        threading.Thread(target=self._load_names, daemon=True).start()
        threading.Thread(target=self._check_state, daemon=True).start()
        media = getattr(self, "call_media", None)
        if media is not None:
            client = c
            media.start_pump(lambda: self.client is client and self.lifecycle.current(generation))
        threading.Thread(target=self._check_always, daemon=True).start()
        threading.Thread(target=self._copy_always, daemon=True).start()
        self.flush_pending_reads()      # read notices kept while disconnected

    def _on_unlinked(self, generation, c):
        """The phone removed the device (LoggedOutEv, or a logout ConnectFailure)."""
        changed = self._apply(generation, session_lifecycle.LEFT)
        if changed is None or changed[0] == session_lifecycle.NOT_LOGGED:
            return          # an event of an old session, or the second notice of the same logout
        log.info("account unlinked by the phone")
        self.me = self.lid = ""
        self.urlcode = None
        # without stop() Go reconnects with no credential and keeps generating QRs with nobody looking
        self._stop_in_background(c)
        if self.on_unlink is not None:
            self.on_unlink()
        for delay, event, data in session_lifecycle.logout_notices(self.name):
            if delay:
                timer = threading.Timer(delay, self.emit, args=(event, data))
                timer.daemon = True
                timer.start()
            else:
                self.emit(event, data)

    def _on_drop(self, generation, event, what):
        """A drop that whatsmeow tries to fix by itself: the client gets its
        grace (INITIALIZING) instead of reading CONNECTED with a dead socket."""
        changed = self._apply(generation, event)
        if changed is not None and changed[0] != changed[1]:
            log.info("%s: %s -> %s", what, changed[0], changed[1])
        else:
            log.info(what)

    def _on_fail(self, generation, c, reason):
        """ConnectFailureEv: logout, terminal failure (ban, outdated version) or transient."""
        kind = session_lifecycle.classify_failure(reason)
        log.warning("WhatsApp connection failure, reason %s (%s)", reason, kind)
        if kind == session_lifecycle.LEFT:
            self._on_unlinked(generation, c)
        elif kind == session_lifecycle.TERMINAL_FAILURE:
            self._on_terminal_failure(generation, c, f"connection failure, reason {reason}")
        else:
            self._on_drop(generation, session_lifecycle.DROPPED, "transient server failure")

    def _on_terminal_failure(self, generation, c, what):
        """There is no point trying again (ban, outdated version, another device
        took the connection): the session closes instead of staying in INITIALIZING forever."""
        if self._apply(generation, session_lifecycle.TERMINAL_FAILURE) is None:
            return
        log.error("session ended: %s", what)
        self._stop_in_background(c)

    def _run(self, phone_number, generation):
        import neonize.events as nev
        from neonize.client import NewClient
        os.makedirs(self.folder, exist_ok=True)
        # A new NewClient per start, never reused: begin() already waited for the
        # previous thread to finish (connect() only returns after stop()).
        # Replacing `self.client` releases the old one, and neonize calls Stop(uuid)
        # when a NewClient is collected (weakref.finalize); that is why the swap
        # happens here, before the new connect(), and never with the old thread still up.
        calls_on = call_layer_on()          # before the client: the layer installs on connect
        cli = self.client = NewClient(os.path.join(self.folder, "session.db"),
                                      props=device_props(HISTORY_DAYS))
        if calls_on and getattr(cli, "uuid", None):
            self.call_media = new_call_media(self.name, cli.uuid, self.emit, self._phone_of_lid)
            # the creator a group call placed here carries, for the lobby query
            self.call_media.own_creator = lambda: self.lid_device
        # every message to a chat with temporary messages carries its timer
        chat_timers.install(cli, self.chat_timer)
        # never two app-state operations at once (app_state_recovery.serialize)
        app_state_recovery.serialize(cli, self._app_state_lock)
        calls.register_events(cli, self)
        presence.register_events(cli, self)
        group_events.register_events(cli, self)
        contact_events.register_events(cli, self)
        app_state_mirror.register_events(cli, self)
        pairing_repair.WATCH.add(self, generation, phone_number)

        # Each lifecycle event is registered ONCE (neonize keeps one handler per
        # type; test_one_handler_per_event.py).
        @cli.event(nev.ConnectedEv)
        def _connected(c, e):
            self._on_connect(generation, c, phone_number)

        @cli.event(nev.LoggedOutEv)
        def _left(c, e):
            self._on_unlinked(generation, c)

        @cli.event(nev.DisconnectedEv)
        def _dropped(c, e):
            self._on_drop(generation, session_lifecycle.DROPPED, "disconnected")

        @cli.event(nev.ConnectFailureEv)
        def _failed(c, e):
            self._on_fail(generation, c, int(e.Reason))

        @cli.event(nev.ClientOutdatedEv)
        def _outdated(c, e):
            self._on_terminal_failure(generation, c, "client version refused by WhatsApp")

        @cli.event(nev.TemporaryBanEv)
        def _banned(c, e):
            self._on_terminal_failure(generation, c, "temporary ban")

        @cli.event(nev.StreamReplacedEv)
        def _replaced(c, e):
            self._on_terminal_failure(generation, c, "another device took the connection")

        @cli.event(nev.StreamErrorEv)
        def _stream_error(c, e):
            self._on_drop(generation, session_lifecycle.DROPPED, "stream error " + str(e.Code)[:40])

        @cli.event(nev.KeepAliveTimeoutEv)
        def _no_response(c, e):
            self._on_drop(generation, session_lifecycle.DROPPED, "keep-alive without response")

        @cli.event(nev.KeepAliveRestoredEv)
        def _responded(c, e):
            self._on_drop(generation, session_lifecycle.RESTORED, "keep-alive restored")

        @cli.event(nev.MessageEv)
        def _message(c, e):
            try:
                o = e.Info.MessageSource
                chat = jid_text(o.Chat)
                try:
                    self.observe_timer(chat, e.Message)
                except Exception:
                    log.exception("temporary messages timer")
                if self._reaction_received(chat, o, e):
                    return
                if self._vote_received(o, e):
                    return
                if comments.receive(self, chat, o, e):    # announcement comment
                    return
                if self._edit_received(chat, o, e):
                    return
                if self._pin_received(chat, e):
                    return
                w = msgmap.build(
                    chat=chat, from_me=bool(o.IsFromMe), id=e.Info.ID,
                    sender=jid_text(o.Sender), ts=e.Info.Timestamp,
                    message=e.Message, sender_name=e.Info.Pushname)
                if w is None:
                    return
                if w.get("type") == "revoked" and w.get("protocolMessageKey"):
                    # deleted for everyone by whoever sent it: the original becomes deleted too
                    self.db.change_message(w["protocolMessageKey"]["id"], type="revoked", body="")
                self.db.message(chat, w, msgmap.media_record_of(e.Message))
                self.db.on_arrival(chat, w["t"], w["fromMe"])
                if e.Info.Pushname and not o.IsFromMe:
                    self.db.contact(jid_text(o.Sender), pushname=e.Info.Pushname)
                    contact_events.announce_name(c, self, o.Sender, e.Info.Pushname)
                self.emit("received-message", {"session": self.name, "response": w})
                # WhatsApp Web marks the chat open with the window in focus as read
                if not o.IsFromMe and self.online and self.open_chat == chat:
                    threading.Thread(target=self.mark_read, args=(chat,),
                                     daemon=True).start()
            except Exception:
                log.exception("received message")

        @cli.event(nev.GroupInfoEv)
        def _group(c, e):
            try:
                if e.Name and e.Name.Name:
                    self.db.chat(jid_text(e.JID), name=e.Name.Name)
                if e.HasField("Ephemeral"):
                    self.record_timer(jid_text(e.JID), int(e.Ephemeral.DisappearingTimer)
                                      if e.Ephemeral.IsEphemeral else 0)
            except Exception:
                log.exception("group changed")
            group_events.group_changed(c, self, e)    # groups.update (see group_events)

        def local_chat(c, jid_obj):
            """The JID under which the chat is stored (the number or the LID)."""
            jid = jid_text(jid_obj)
            if not self.db.chat_exists(jid) and jid.endswith("@s.whatsapp.net"):
                try:
                    lid = jid_text(c.get_lid_from_pn(jid_obj))
                    if lid and self.db.chat_exists(lid):
                        return lid
                except Exception:
                    pass
            return jid

        def announce_read(jid, before, new):
            chat_w = msgmap.jid_wpp(jid)
            self.emit("chats-update", {"session": self.name, "data": [{
                "remoteJid": chat_w, "id": chat_w, "unreadCount": new,
                "previousUnreadCount": before}]})

        def read_on_another_device(c, e):
            """Read receipt that comes from myself: I read those messages on the phone."""
            jid = local_chat(c, e.MessageSource.Chat)
            before = self.db.unread(jid)
            ids = set(e.MessageIDs)
            last = self.db.last_received(jid)
            new = 0 if (last and last in ids) else max(0, before - len(ids))
            if new == before:
                return
            self.db.chat(jid, unread=new)
            log.info("read on the phone: %d message(s), unread %d -> %d", len(ids), before, new)
            announce_read(jid, before, new)

        def played_on_another_device(c, e):
            """I played a received voice note on the phone: the app shows 'Played' (onack 4)."""
            jid = local_chat(c, e.MessageSource.Chat)
            chat_w = msgmap.jid_wpp(jid)
            for mid in e.MessageIDs:
                self.db.change_message(mid, played=True)
                self.emit("onack", {
                    "session": self.name, "ack": 4, "to": chat_w, "from": chat_w,
                    "id": {"fromMe": False, "remote": chat_w, "id": mid,
                           "_serialized": f"false_{chat_w}_{mid}"}})
            log.info("%d voice note(s) played on the phone -> app", len(e.MessageIDs))

        @cli.event(nev.ChatMarkAsReadEv)
        def _read_on_phone(c, e):
            """Chat read (or marked as unread) on the phone: the app follows."""
            try:
                action = e.Action
                jid = jid_text(e.JID)
                if not self.db.chat_exists(jid) and jid.endswith("@s.whatsapp.net"):
                    try:        # the chat may be stored under the LID address
                        lid = jid_text(c.get_lid_from_pn(e.JID))
                        if lid and self.db.chat_exists(lid):
                            jid = lid
                    except Exception:
                        pass
                before = self.db.unread(jid)
                if action.read:
                    until = msgmap.seconds(action.messageRange.lastMessageTimestamp)                         if action.HasField("messageRange") else 0
                    last = self.db.raw_last(jid)
                    if until and last is not None and last["t"] > until + 5:
                        log.info("read on the phone, but there is a newer message: kept")
                        return
                    self.db.chat(jid, unread=0)
                    new = 0
                else:
                    self.db.chat(jid, unread=max(before, 1))
                    new = -1
                log.info("chat marked as %s on the phone -> app",
                         "read" if action.read else "unread")
                chat_w = msgmap.jid_wpp(jid)
                self.emit("chats-update", {"session": self.name, "data": [{
                    "remoteJid": chat_w, "id": chat_w, "unreadCount": new,
                    "previousUnreadCount": before}]})
            except Exception:
                log.exception("chat read on the phone")

        @cli.event(nev.ChatArchiveEv)
        def _archived_on_phone(c, e):
            try:
                jid = local_chat(c, e.JID)
                arch = bool(e.Action.archived)
                self.db.chat(jid, archived=int(arch))
                chat_w = msgmap.jid_wpp(jid)
                log.info("chat %s on the phone -> app", "archived" if arch else "unarchived")
                self.emit("chats-update", {"session": self.name, "data": [{
                    "remoteJid": chat_w, "id": chat_w, "archive": arch}]})
            except Exception:
                log.exception("archive on the phone")

        @cli.event(nev.ChatPinEv)
        def _pinned_on_phone(c, e):
            try:
                jid = local_chat(c, e.JID)
                pin = bool(e.Action.pinned)
                self.db.chat(jid, pinned=int(time.time()) if pin else 0)
                chat_w = msgmap.jid_wpp(jid)
                log.info("chat %s on the phone -> app", "pinned" if pin else "unpinned")
                self.emit("chats-update", {"session": self.name, "data": [{
                    "remoteJid": chat_w, "id": chat_w,
                    "pin": int(time.time()) if pin else False}]})
            except Exception:
                log.exception("pin on the phone")

        @cli.event(nev.MessageStarEv)
        def _starred_on_phone(c, e):
            try:
                self.db.change_message(e.MessageID, star=bool(e.Action.starred))
                log.info("message %s on the phone", "starred" if e.Action.starred else "unstarred")
            except Exception:
                log.exception("star on the phone")

        @cli.event(nev.MessageDeleteForMeEv)
        def _deleted_on_phone(c, e):
            try:
                if e.FromFullSync:
                    return
                self.db.delete_message(e.MessageID)
                log.info("message deleted on the phone")
            except Exception:
                log.exception("delete on the phone")

        @cli.event(nev.ChatLockEv)
        def _locked_on_phone(c, e):
            try:
                jid = local_chat(c, e.JID)
                self.db.chat(jid, locked=int(bool(e.Action.locked)))
                log.info("chat %s on the phone", "locked" if e.Action.locked else "unlocked")
            except Exception:
                log.exception("lock on the phone")

        @cli.event(nev.ChatMuteEv)
        def _muted_on_phone(c, e):
            try:
                jid = local_chat(c, e.JID)
                end = 0
                if e.Action.muted:
                    end = msgmap.seconds(e.Action.muteEndTimestamp)                         if e.Action.muteEndTimestamp > 0 else 2**31 - 1
                self.db.chat(jid, muted=int(end))
                log.info("chat %s on the phone", "muted" if e.Action.muted else "with sound again")
            except Exception:
                log.exception("mute on the phone")

        @cli.event(nev.ReceiptEv)
        def _receipt(c, e):
            try:
                from neonize.proto.Neonize_pb2 import Receipt
                kind = Receipt.DESCRIPTOR.fields_by_name["Type"].enum_type \
                    .values_by_number[e.Type].name
                ack = {"DELIVERED": 2, "READ": 3, "PLAYED": 4}.get(kind)
                chat = msgmap.jid_wpp(jid_text(e.MessageSource.Chat))
                is_mine = bool(e.MessageSource.IsFromMe)
                log.info("receipt %s%s (%d message(s)) in %s -> ack %s", kind,
                         " [mine]" if is_mine else "", len(e.MessageIDs), redact(chat), ack)
                # read receipt that comes from me: I read on the phone (or on another device)
                if kind == "READ_SELF" or (kind == "READ" and is_mine):
                    read_on_another_device(c, e)
                    return
                if kind == "PLAYED_SELF" or (kind == "PLAYED" and is_mine):
                    played_on_another_device(c, e)
                    return
                if ack is None:
                    return
                if not is_mine:
                    try:
                        who = msgmap.jid_wpp(jid_text(e.MessageSource.Sender))
                        stage = {"DELIVERED": "delivered", "READ": "read",
                                 "PLAYED": "played"}[kind]
                        self.db.receipts(list(e.MessageIDs), who, stage,
                                         msgmap.seconds(e.Timestamp))
                    except Exception:
                        log.exception("store receipt")
                if chat.endswith("@g.us"):
                    # group: the ack only advances when ALL the members got there
                    # (group_receipts.py); a single receipt is not enough
                    for mid in e.MessageIDs:
                        in_group = self.group_ack_to_emit(chat, mid)
                        if in_group:
                            self.emit("onack", {
                                "session": self.name, "ack": in_group, "to": chat, "from": chat,
                                "id": {"fromMe": True, "remote": chat, "id": mid,
                                       "_serialized": f"true_{chat}_{mid}"}})
                    return
                for mid in e.MessageIDs:
                    self.emit("onack", {
                        "session": self.name, "ack": ack, "to": chat, "from": chat,
                        "id": {"fromMe": True, "remote": chat, "id": mid,
                               "_serialized": f"true_{chat}_{mid}"}})
            except Exception:
                log.exception("receipt")

        @cli.event(nev.HistorySyncEv)
        def _history(c, e):
            try:
                self._store_history(e.Data)
            except Exception:
                log.exception("history")

        @cli.event.qr
        def _qr(c, data):
            code = data.decode() if isinstance(data, bytes) else str(data)
            if self._apply(generation, session_lifecycle.QR) is None:
                return
            self.urlcode = code
            self.emit("qrCode", {"session": self.name, "urlcode": code,
                                 "data": qr_data_url(code)})

        if phone_number:
            def _phone_code():
                from neonize.utils.enum import ClientName, ClientType
                # waits for the connection to decide: already paired (CONNECTED) needs no code
                for _ in range(40):
                    if self.status in ("QRCODE", "CONNECTED") or not self.lifecycle.current(generation):
                        break
                    time.sleep(0.5)
                if not self.lifecycle.current(generation):
                    return                      # the session was closed or restarted
                if self.status == "CONNECTED":
                    if self.simulate_code:      # only in test mode (--single-session)
                        self.emit("phoneCode", {"session": self.name,
                                                "data": "TEST-0000"})
                        time.sleep(2)
                        self.emit("session-logged", {"session": self.name,
                                                     "status": True})
                    return
                error_info = {"name": "LinkCodeError", "message": "failed to generate the code",
                              "rateLimited": False}
                # a Brazilian mobile in both forms (9th digit), the likelier first
                candidates = pairing.phone_candidates(phone_number)
                for attempt in range(5):
                    if not self.lifecycle.current(generation):
                        return
                    number = candidates[attempt % len(candidates)]
                    try:
                        pairing_code = cli.PairPhone(number, True, ClientName.WINDOWS,
                                                     ClientType.EDGE)
                        self.emit("phoneCode", {"session": self.name, "data": pairing_code})
                        self._watch_code(generation)
                        return
                    except Exception as error:
                        log.warning("pairing code, attempt %d (form %d of %d): %s",
                                    attempt + 1, attempt % len(candidates) + 1,
                                    len(candidates), type(error).__name__)
                        error_info = pairing.code_error(error)
                        if error_info["rateLimited"]:
                            break           # the quota is the number's: repeating keeps it alive
                        time.sleep(3)
                self.emit("phoneCodeError", {"session": self.name, **error_info})
            threading.Thread(target=_phone_code, daemon=True).start()

        try:
            cli.connect()      # blocks until stop()
        except Exception:
            log.exception("connect")
        finally:
            pairing_repair.WATCH.remove(self, generation)
            self._apply(generation, session_lifecycle.THREAD_ENDED)

    def _on_pairing_failure(self, generation, kind, phone_number):
        """Called by pairing_repair.WATCH on the log's worker thread: it only
        decides, the work goes to its own thread."""
        if not self.lifecycle.current(generation) or self.status != session_lifecycle.QRCODE:
            return                      # paired already, or an old session's line
        log.warning("WhatsApp refused the pairing (%s)", kind)
        threading.Thread(target=self._repair_pairing, args=(generation, kind, phone_number),
                         daemon=True, name="pairing-repair").start()

    def _repair_pairing(self, generation, kind, phone_number):
        with self._lifecycle_lock:
            if not self.lifecycle.current(generation):
                return
            # The thread started a moment ago and may have waited for this lock while
            # the pairing went through: a session that is no longer waiting for a scan,
            # or that already holds an identity, is paired and its keys are not ours to wipe.
            if self.status != session_lifecycle.QRCODE or self._has_identity():
                return
            if phone_number:
                kind = pairing_repair.CODE      # the client is waiting for a code, not a QR
            if not self.pairing_repairs.take():
                log.warning("pairing refused again; not restarting it more times (generation %s)",
                            generation)
                self._give_up("WhatsApp refused the pairing again")
                return
            self.stop(session_lifecycle.CLOSED)
            thread = self.thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(session_lifecycle.OLD_THREAD_DEADLINE)
            if self._thread_alive() or not pairing_repair.wipe_session_keys(self.folder):
                log.warning("could not clear the old pairing (generation %s)", generation)
                self._give_up("the old pairing could not be cleared")
                return
            if kind == pairing_repair.CODE:
                # a new code would be minted for a number nobody asked about (quota)
                self.emit("phoneCodeError", pairing_repair.give_up_notice(
                    self.name, "WhatsApp refused the pairing code"))
                return
            self.begin(repairing=True)

    def _has_identity(self) -> bool:
        """True when the client already knows its own WhatsApp id (a pairing went through)."""
        try:
            return bool(self.client.get_me().JID.User)
        except Exception:
            return False

    def _give_up(self, message):
        """The repair ends: offline for good (TERMINAL does not restart by itself)
        and the dead QR or code is announced."""
        self.stop(session_lifecycle.TERMINAL)
        self.emit("phoneCodeError", pairing_repair.give_up_notice(self.name, message))

    def _watch_code(self, generation, lifetime=session_lifecycle.CODE_LIFETIME, step=1.0):
        """After the phoneCode: if the code expires without pairing, tells the client
        (LinkCodeExpired) so the user is not left with a dead code. Does not
        generate another code: with nobody asking, that spends the per-number
        quota (docs/traps/pairing-flow.md). Runs on the code's thread."""
        end = time.monotonic() + lifetime
        while time.monotonic() < end:
            if not self.lifecycle.current(generation) or self.status != "QRCODE":
                return                  # paired, closed or restarted: nothing to warn about
            time.sleep(min(step, max(0.0, end - time.monotonic())))
        if self.lifecycle.current(generation) and self.status == "QRCODE":
            self.emit("phoneCodeError", session_lifecycle.code_expired_notice(self.name))

    def _store_history(self, data):
        progress = history_progress.progress_of(self, time.monotonic())
        progress.started(time.monotonic())
        percent = None
        try:
            percent = self._store_history_block(data)
        finally:
            progress.finished(time.monotonic(), percent)

    def _store_history_block(self, data):
        """Stores a block of the history; returns the progress (0-100) it brings.
        Everything goes in three transactions (contacts, chats, messages): a
        commit per chat and per name kept the store's lock for tens of seconds,
        the routes waited on it, the client's socket missed its pings and the
        client took the bridge for disconnected."""
        self.db.contacts_in_batch([(x.ID, None, x.pushname) for x in data.pushnames
                                   if x.ID and x.pushname])
        total = 0
        chats, rows = [], []
        for c in data.conversations:
            jid = c.ID
            if not jid or jid.endswith("@broadcast") or jid.endswith("@newsletter"):
                continue
            fields = {"name": c.name or c.displayName or None,
                      "ts": msgmap.seconds(c.conversationTimestamp or c.lastMsgTimestamp)}
            if c.HasField("unreadCount"):
                fields["unread"] = int(c.unreadCount)
            if c.HasField("archived"):
                fields["archived"] = int(bool(c.archived))
            if c.HasField("pinned"):
                # I cannot tell the unit (order or pinning time); the app only looks
                # at whether it is greater than zero. The bridge does not enforce the
                # limit of 3 pinned chats either: WhatsApp refuses the fourth
                fields["pinned"] = int(c.pinned)
            if c.HasField("muteEndTime"):
                fields["muted"] = int(c.muteEndTime)
            if c.HasField("locked"):
                fields["locked"] = int(bool(c.locked))
            if c.HasField("ephemeralExpiration"):
                fields["ephemeral"] = int(c.ephemeralExpiration)
                fields["ephemeral_ts"] = int(c.ephemeralSettingTimestamp or 0)
            chats.append((jid, {k: v for k, v in fields.items() if v is not None}))
            count = len(rows)
            for h in c.messages:
                m = h.message
                key = m.key
                w = msgmap.build(
                    chat=jid, from_me=bool(key.fromMe), id=key.ID,
                    sender=key.participant or m.participant
                    or ("" if key.fromMe else jid),
                    ts=m.messageTimestamp, message=m.message,
                    sender_name=m.pushName)
                if w is not None and w["id"]["id"]:
                    if m.starred:
                        w["star"] = True
                    rows.append((jid, w, msgmap.media_record_of(m.message)))
            total += len(rows) - count
        self.db.chats_in_batch(chats)
        self.db.messages_in_batch(rows)
        log.info("history: %d chats, %d messages (%s%%)",
                 len(data.conversations), total, data.progress)
        return data.progress

    COLLECTIONS = ("regular_high", "regular_low", "regular",
                   "critical_unblock_low", "critical_block")

    def _check_state(self):
        """Checks the app state collections (read/unread, pin, mute...).
        A collection out of sync ("mismatching LTHash") makes the phone's changes
        stop arriving and the ones from here be refused."""
        time.sleep(5)       # lets the initial sync finish
        for name in self.COLLECTIONS:
            self.repair_collection(name, force=False)
        self.reload_names()     # critical_unblock_low carries the saved names

    def _check_always(self):
        """Every minute fetches the app state changes that the phone made and the
        live notice let slip (reading a chat, for example)."""
        if getattr(self, "_fetch_active", False):     # a single loop, even when reconnecting
            return
        self._fetch_active = True
        try:
            while self.status == "CONNECTED":
                time.sleep(60)
                self.fetch_changes()
                # names saved on the phone (or synced late after pairing) land in
                # neonize's store on their own; this brings them to all-contacts
                self.reload_names()
        finally:
            self._fetch_active = False

    COPY_INTERVAL = 300        # seconds between two copy requests to the phone

    def request_state_copy(self, names=("regular_low", "regular_high")):
        """Asks the phone for the copy of the state. Changes it made but did not
        upload as an update (it happens with archiving) only arrive this way. The
        iPhone only answers with WhatsApp open on the screen; with no answer, it
        tries again in the next round. Returns how many new changes arrived."""
        if self.client is None or self.status != "CONNECTED":
            return 0
        before = self.db.state_counts()
        for name in names:
            try:
                self.client.request_app_state_recovery(name)
            except Exception as error:
                log.info("state copy request %s: %s", name, error)
                continue
            for _ in range(8):          # waits up to ~40 s for the answer
                time.sleep(5)
                try:
                    self.client.fetch_app_state(name, full_sync=False)
                except Exception as error:
                    if "LTHash" not in str(error):
                        log.info("state %s: %s", name, error)
                        break
        after = self.db.state_counts()
        log.info("state copy requested: archived %d -> %d, pinned %d -> %d",
                 before[0], after[0], before[1], after[1])
        return abs(after[0] - before[0]) + abs(after[1] - before[1])

    def _copy_always(self):
        if getattr(self, "_copy_active", False):
            return
        self._copy_active = True
        try:
            while self.status == "CONNECTED":
                time.sleep(self.COPY_INTERVAL)
                self.request_state_copy()
        finally:
            self._copy_active = False

    def check_settings(self):
        """Reads from whatsmeow's store (which the phone's state notices keep up to
        date) whether each chat is archived, pinned and muted, and follows what
        changed, the same way as the live notices. Changes the phone made that did
        not arrive as a notice (or that arrived before the app looked) show up here."""
        c = self.client
        if c is None or self.status != "CONNECTED" or not hasattr(c, "chat_settings"):
            return 0
        from neonize.utils import build_jid
        changed = 0
        for conv in self.db.chats():
            jid = conv["jid"]
            user, _, server = jid.partition("@")
            if server in ("broadcast", "newsletter") or not user:
                continue
            candidates = [build_jid(user, server)]
            if server == "s.whatsapp.net":
                try:
                    lid = c.get_lid_from_pn(candidates[0])
                    if lid.User:
                        candidates.append(lid)
                except Exception:
                    pass
            data = None
            for target in candidates:
                try:
                    found = c.chat_settings.get_chat_settings(target)
                except Exception:
                    continue
                if found.Found:
                    data = found
                    break
            if data is None:
                continue
            is_archived, is_pinned = bool(data.Archived), bool(data.Pinned)
            fields, notices = {}, {}
            if is_archived != bool(conv["archived"]):
                fields["archived"] = int(is_archived)
                notices["archive"] = is_archived
            if is_pinned != (conv["pinned"] > 0):
                fields["pinned"] = int(time.time()) if is_pinned else 0
                notices["pin"] = fields["pinned"] if is_pinned else False
            end = int(data.MutedUntil) if data.MutedUntil > 0 else 0
            if end > 4e9:
                end = 2**31 - 1
            if (end > 0) != (conv["muted"] > 0):
                fields["muted"] = end
            if not fields:
                continue
            self.db.chat(jid, **fields)
            changed += 1
            if notices:
                chat_w = msgmap.jid_wpp(jid)
                self.emit("chats-update", {"session": self.name, "data": [{
                    "remoteJid": chat_w, "id": chat_w, **notices}]})
        if changed:
            log.info("settings read from the store: %d chat(s) changed", changed)
        return changed

    def fetch_changes(self):
        """fetch_app_state of regular_low/high: the new notices become events."""
        now = time.monotonic()
        if now - getattr(self, "_last_fetch", 0) < 15 or self.status != "CONNECTED":
            return
        self._last_fetch = now
        try:
            self.check_settings()
        except Exception:
            log.exception("check settings")
        for name in ("regular_low", "regular_high"):
            try:
                self.client.fetch_app_state(name, full_sync=False)
            except Exception as error:
                if "LTHash" in str(error):
                    self.repair_collection(name, force=False)
                else:
                    log.info("state fetch %s: %s", name, error)

    def repair_collection(self, name, force=True):
        c = self.client
        if c is None or self.status != "CONNECTED":
            return False
        try:
            c.fetch_app_state(name, full_sync=force)
            log.info("state %s checked%s", name, " (downloaded again)" if force else "")
            return True
        except Exception as error:
            if "LTHash" not in str(error):
                log.warning("state %s: %s", name, error)
                return False
            if not force:
                log.warning("state %s out of sync; downloading again", name)
                return self.repair_collection(name, force=True)
            return self._recover_collection(name)

    def _recover_collection(self, name):
        """Asks the phone for a fresh copy of the state (the iPhone only answers with
        WhatsApp open on the screen). One request per collection at a time, and
        none for a while after one went unanswered (app_state_recovery.py)."""
        recovered = self._recovery_gate.run(name, lambda: self._ask_phone_for_collection(name))
        if recovered:
            self.flush_pending_reads()
        return recovered

    def _ask_phone_for_collection(self, name):
        log.warning("state %s: asking the phone for a fresh copy", name)
        try:
            self.client.request_app_state_recovery(name)
        except Exception as error:
            log.warning("could not request the state copy: %s", error)
            return False
        for _ in range(18):
            time.sleep(5)
            try:
                self.client.fetch_app_state(name)
                log.info("state %s recovered with the phone's copy", name)
                return True
            except Exception as error:
                if "LTHash" not in str(error):
                    log.warning("state %s: %s", name, error)
                    return False
        log.warning("the phone did not answer the request for state %s: open WhatsApp on the phone", name)
        return False

    def reload_names(self):
        """Copies neonize's contact store (saved names and nicknames) into the
        bridge's. The saved names come with the app state (critical_unblock_low),
        which right after pairing lands well after the connection: read only at
        connect, a newly paired account had no saved name at all, and every
        group sender showed as an unsaved contact. Returns how many saved names
        were learned."""
        c = self.client
        if c is None:
            return 0
        try:
            rows = contacts.address_book_rows(c.contact.get_all_contacts(), jid_text)
        except Exception:
            log.exception("contacts")
            return 0
        learned = self.db.contacts_in_batch(rows)
        if learned:
            log.info("contacts: %d saved name(s) learned", learned)
        return learned

    def _phone_of_lid(self, lid):
        """'123@lid' -> the number's JID text, or '' when whatsmeow does not know it."""
        from neonize.utils import build_jid
        try:
            return jid_text(self.client.get_pn_from_lid(build_jid(lid.split("@")[0], "lid")))
        except Exception:
            return ""

    def _lid_of_phone(self, phone):
        """'5511@s.whatsapp.net' -> the LID's JID text, or '' when whatsmeow does not know it."""
        from neonize.utils import build_jid
        try:
            return jid_text(self.client.get_lid_from_pn(build_jid(phone.split("@")[0], "s.whatsapp.net")))
        except Exception:
            return ""

    def _load_names(self):
        self.reload_names()
        try:
            for g in self.client.get_joined_groups():
                self.db.chat(jid_text(g.JID), name=g.GroupName.Name)
                self.roster_register(g)    # members, for the group ack (group_receipts)
        except Exception:
            log.exception("groups")

    def state_read(self, conversation, read_flag):
        """Marks the chat as read (or unread) on the phone and the other devices,
        through the markChatAsRead state notice. One the phone does not take is
        kept and sent again once the collection works (flush_pending_reads)."""
        if self._send_read_notice(conversation, read_flag):
            self._pending_reads.discard(conversation)
            self.flush_pending_reads()
            return True
        self._pending_reads.put(conversation, read_flag)
        return False

    def _send_read_notice(self, conversation, read_flag):
        """The markChatAsRead notice itself: True if WhatsApp accepted it."""
        from neonize.proto.Neonize_pb2 import MutationInfo, PatchInfo
        from neonize.proto.waCommon.WACommon_pb2 import MessageKey
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            MarkChatAsReadAction, SyncActionMessage, SyncActionMessageRange,
            SyncActionValue)
        last = self.db.raw_last(conversation)
        range_ = SyncActionMessageRange()
        if last is not None:
            range_.lastMessageTimestamp = int(last["t"])
            key = MessageKey(remoteJID=conversation, fromMe=bool(last.get("fromMe")),
                             ID=last["id"]["id"])
            if conversation.endswith("@g.us") and not last.get("fromMe") and last.get("author"):
                key.participant = msgmap.jid_whatsmeow(last["author"])
            range_.messages.append(SyncActionMessage(key=key, timestamp=int(last["t"])))
        patch = PatchInfo(
            Timestamp=time.time_ns(), Type=PatchInfo.REGULAR_LOW,
            Mutations=[MutationInfo(
                Index=["markChatAsRead", conversation], Version=3,
                Value=SyncActionValue(markChatAsReadAction=MarkChatAsReadAction(
                    read=bool(read_flag), messageRange=range_)))])
        try:
            try:
                self.client.send_app_state(patch)
            except Exception as error:
                if "LTHash" not in str(error) and "409" not in str(error):
                    raise
                # collection out of sync: download it again and try once more
                if not self.repair_collection("regular_low"):
                    raise
                self.client.send_app_state(patch)
            log.info("chat marked as %s on the phone", "read" if read_flag else "unread")
            return True
        except Exception:
            log.exception("state notice (read) not accepted")
            return False

    def flush_pending_reads(self):
        """Send, on a thread, the read notices the phone did not take before.
        One flush at a time; a notice that fails again goes back in the queue."""
        if not len(self._pending_reads) or not self._flushing_reads.acquire(blocking=False):
            return

        def flush():
            try:
                pending = self._pending_reads.take()
                for index, (conversation, read_flag) in enumerate(pending):
                    connected = self.client is not None and self.status == "CONNECTED"
                    if connected and self._send_read_notice(conversation, read_flag):
                        continue
                    # still broken: this one and the rest wait for the next success,
                    # behind any newer wish for the same chat
                    for later, flag in pending[index:]:
                        self._pending_reads.restore(later, flag)
                    break
            finally:
                self._flushing_reads.release()

        threading.Thread(target=flush, daemon=True, name="pending-reads").start()

    def send_state(self, index, version, kind, value, conversation, extra=()):
        """Generic state notice (archive, pin...): True if WhatsApp accepted."""
        from neonize.proto.Neonize_pb2 import MutationInfo, PatchInfo
        if self.client is None or self.status != "CONNECTED":
            return False
        patch = PatchInfo(Timestamp=time.time_ns(), Type=kind, Mutations=[
            MutationInfo(Index=[index, conversation, *extra], Version=version, Value=value)])
        collection = {PatchInfo.REGULAR_LOW: "regular_low",
                      PatchInfo.REGULAR_HIGH: "regular_high"}.get(kind, "regular_low")
        try:
            try:
                self.client.send_app_state(patch)
            except Exception as error:
                if "LTHash" not in str(error) and "409" not in str(error):
                    raise
                if not self.repair_collection(collection):
                    raise
                self.client.send_app_state(patch)
            return True
        except Exception:
            log.exception("state notice %s not accepted", index)
            return False

    def archive(self, conversation, archive):
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            ArchiveChatAction, SyncActionMessageRange, SyncActionValue)
        last = self.db.raw_last(conversation)
        when = int(last["t"]) if last else int(time.time())
        ok_ = self.send_state(
            "archive", 3, PatchInfo.REGULAR_LOW,
            SyncActionValue(archiveChatAction=ArchiveChatAction(
                archived=bool(archive),
                messageRange=SyncActionMessageRange(lastMessageTimestamp=when))),
            conversation)
        if ok_:
            self.db.chat(conversation, archived=int(bool(archive)))
        return ok_

    def pin(self, conversation, pin):
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            PinAction, SyncActionValue)
        ok_ = self.send_state(
            "pin_v1", 5, PatchInfo.REGULAR_LOW,
            SyncActionValue(pinAction=PinAction(pinned=bool(pin))), conversation)
        if ok_:
            self.db.chat(conversation, pinned=int(time.time()) if pin else 0)
        return ok_

    @staticmethod
    def id_parts(serial):
        """'true_<chat>_<ID>[_author]' -> (from_me, whatsmeow chat, ID, whatsmeow author or '')."""
        p = serial.split("_")
        if len(p) < 3:
            return False, "", serial, ""
        return p[0] == "true", msgmap.jid_whatsmeow(p[1]), p[2], \
            msgmap.jid_whatsmeow(p[3]) if len(p) > 3 else ""

    def _message_index(self, serial):
        from_me, conversation, mid, author = self.id_parts(serial)
        participant = author if (conversation.endswith("@g.us") and not from_me and author) else "0"
        return conversation, mid, ("1" if from_me else "0", participant)

    def star(self, serial, starred):
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import StarAction, SyncActionValue
        conversation, mid, (own, participant) = self._message_index(serial)
        ok_ = self.send_state(
            "star", 2, PatchInfo.REGULAR_HIGH,
            SyncActionValue(starAction=StarAction(starred=bool(starred))),
            conversation, extra=(mid, own, participant))
        if ok_:
            self.db.change_message(mid, star=bool(starred))
        return ok_

    def mute(self, conversation, seconds):
        """seconds: 0 = remove the mute; -1 = forever."""
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import MuteAction, SyncActionValue
        action = MuteAction(muted=seconds != 0)
        if seconds != 0:
            action.muteEndTimestamp = -1 if seconds < 0 else int((time.time() + seconds) * 1000)
        ok_ = self.send_state("mute", 2, PatchInfo.REGULAR_HIGH,
                              SyncActionValue(muteAction=action), conversation)
        if ok_:
            end = 0 if seconds == 0 else (2**31 - 1 if seconds < 0 else int(time.time() + seconds))
            self.db.chat(conversation, muted=end)
        return ok_

    def delete_for_me(self, serial, delete_media=True):
        """delete_media is WhatsApp's "Delete file from your phone" box: the
        phone removes the photo, video, audio or document from its storage
        only when the synced action says so."""
        from neonize.proto.Neonize_pb2 import PatchInfo
        from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
            DeleteMessageForMeAction, SyncActionValue)
        conversation, mid, (own, participant) = self._message_index(serial)
        original = self.db.wpp_message(mid) or {}
        ok_ = self.send_state(
            "deleteMessageForMe", 3, PatchInfo.REGULAR_HIGH,
            SyncActionValue(deleteMessageForMeAction=DeleteMessageForMeAction(
                deleteMedia=bool(delete_media),
                messageTimestamp=int(original.get("t", time.time())))),
            conversation, extra=(mid, own, participant))
        if ok_:
            self.db.delete_message(mid)
        return ok_

    def delete_for_everyone(self, serial):
        from_me, conversation, mid, author = self.id_parts(serial)
        if self.client is None or self.status != "CONNECTED":
            return False
        who = message_rules.revocation_author(
            from_me, conversation.endswith("@g.us"), self.me, author,
            self.db.wpp_message(mid) or {})
        if not who:
            return False
        from neonize.utils import build_jid
        u, _, srv = conversation.partition("@")
        try:
            me_u, _, me_srv = who.partition("@")
            self.client.revoke_message(build_jid(u, srv), build_jid(me_u, me_srv), mid)
        except Exception:
            log.exception("revoke")
            return False
        self.db.change_message(mid, type="revoked", body="")
        return True

    def mark_played(self, serial):
        from neonize.utils import build_jid
        from_me, conversation, mid, author = self.id_parts(serial)
        if from_me or self.client is None or self.status != "CONNECTED":
            return False
        original = self.db.wpp_message(mid) or {}
        who = author or msgmap.jid_whatsmeow(original.get("author") or original.get("from") or conversation)
        cu, _, csrv = conversation.partition("@")
        qu, _, qsrv = who.partition("@")
        try:
            self.client.mark_read(mid, chat=build_jid(cu, csrv), sender=build_jid(qu, qsrv),
                                  receipt=self.receipt_for(conversation, "PLAYED"))
        except Exception:
            log.exception("mark_played")
            return False
        self.db.change_message(mid, played=True)
        return True

    def mark_unread(self, conversation):
        self.db.chat(conversation, unread=1)
        if self.client is not None and self.status == "CONNECTED":
            self.state_read(conversation, False)

    def mark_read(self, conversation, force=False):
        """How many messages received the read receipt (see read_and_confirm)."""
        return self.read_and_confirm(conversation, force)[0]

    def read_and_confirm(self, conversation, force=False):
        """Sends the read receipt of the received messages not yet read and marks
        the chat as read on the phone too. Returns (how many, ok): ok is False if
        some receipt did not go out (send-seen only confirms what went out).
        A refused state notice (the chat stays unread on the phone) is left in the
        log: the receipts, which the contact sees, are what count here."""
        if self.client is None or self.status != "CONNECTED":
            return 0, False
        had_unread = self.db.unread(conversation) > 0
        to_read = self.db.received_to_read(conversation)
        if had_unread or force:     # force: the app asked (the phone may still have the counter)
            self.state_read(conversation, True)
        if not to_read:
            return 0, True
        from neonize.utils import build_jid

        def jid_of(text):
            u, _, srv = msgmap.jid_whatsmeow(text).partition("@")
            return build_jid(u, srv)

        by_author = {}
        for mid, author, ts in to_read:
            by_author.setdefault(author, []).append(mid)
        sent = True
        # read-self when the account hides its read receipts (read_receipts.py)
        receipt = self.receipt_for(conversation, "READ")
        for author, ids in by_author.items():
            try:
                self.client.mark_read(*ids, chat=jid_of(conversation), sender=jid_of(author),
                                      receipt=receipt)
            except Exception:
                log.exception("mark_read")
                sent = False
        log.info("read %d message(s) in %s", len(to_read), redact(conversation))
        return len(to_read), sent

    def stop(self, status=None):
        """Closes the session: disconnect() closes the socket and stop() cancels
        Go's context, the only way for the thread's connect() to return. The
        thread finishes on its own; begin() waits for it. Idempotent."""
        with self._lifecycle_lock:
            self.lifecycle.end(status)
            c = self.client
            if c is not None:
                for action in (c.disconnect, c.stop):
                    try:
                        action()
                    except Exception:
                        log.exception("stop session")

    def leave(self):
        c = self.client
        if c is not None:
            try:
                c.logout()
            except Exception:
                log.exception("logout")
        with self._lifecycle_lock:
            self.me = self.lid = ""
            self.urlcode = None
            self.stop(session_lifecycle.CLOSED)


# ---------------------------------------------------------------- HTTP
# No access_log: it records every URL, and in them go the bridge's secret, each
# session's token and phones (/api/<token>/get-messages/5511...@c.us). The
# bridge.log is the file the user sends in a bug report.
SERVER_OPTIONS = {"host": "127.0.0.1", "print": None, "access_log": None}


def create_app(folder, fixed=""):
    # Without cors_allowed_origins="*": same origin only (the WinZapp client sends no Origin).
    sio = socketio.AsyncServer(async_mode="aiohttp", logger=False, engineio_logger=False)
    legacy_layout.migrate(folder)           # files of a folder written under the old names
    secret = authentication.load_secret(folder)
    app = web.Application(client_max_size=authentication.MAX_BODY_SIZE,
                          middlewares=[authentication.create_middleware(secret)])
    sio.attach(app)
    app["sio"] = sio                        # the tests look at who is connected
    state = {"sessions": {}, "fixed": fixed}
    active_file = os.path.join(folder, "active.json")
    # send-seen gets threads of its own: marking a chat read can wait on the
    # phone for a fresh app-state copy, and on the executor every other route
    # uses it starved the sends (a message past the client's 25 s was reported
    # as not confirmed and went out a moment later).
    from concurrent.futures import ThreadPoolExecutor
    marking = ThreadPoolExecutor(max_workers=2, thread_name_prefix="send-seen")

    def read_active():
        try:
            with open(active_file, encoding="utf-8") as f:
                return [str(n) for n in json.load(f)]
        except (OSError, ValueError):
            return []

    def mark_active(name, active):
        """Remembers which accounts were on, so the bridge resumes on its own
        when it restarts (the app only calls start-session when it opens)."""
        names = [n for n in read_active() if n != name]
        if active:
            names.append(name)
        os.makedirs(folder, exist_ok=True)
        with open(active_file, "w", encoding="utf-8") as f:
            json.dump(names, f)

    def new_session(name):
        """The Session of `name`, kept in the state. If the phone unlinks it, it
        stops being resumed when the bridge restarts: with no credential, it would
        only generate QRs with nobody looking."""
        s = state["sessions"][name] = Session(
            name, os.path.join(folder, name), sio, asyncio.get_running_loop())
        s.on_unlink = lambda: mark_active(name, False)
        return s

    async def resume(_app):
        if state["fixed"]:
            return
        for name in read_active():
            if os.path.exists(os.path.join(folder, name, "session.db")):
                s = new_session(name)
                await asyncio.get_running_loop().run_in_executor(None, s.begin)
                log.info("session resumed on start: %s", redact(name))
    app.on_startup.append(resume)

    async def start_loop_watch(_app):
        _app[LOOP_WATCH] = asyncio.get_running_loop().create_task(loop_watch.watch())

    async def stop_loop_watch(_app):
        task = _app.get(LOOP_WATCH)
        if task is not None:
            task.cancel()
    app.on_startup.append(start_loop_watch)
    app.on_cleanup.append(stop_loop_watch)

    def key_of(req):
        return state["fixed"] or req.match_info["tok"].split(":", 1)[0]

    def session(req):
        tok = req.match_info["tok"]
        name = state["fixed"] or tok.split(":", 1)[0]
        if not inputs.valid_session_name(name):
            raise web.HTTPBadRequest(reason="invalid session name")
        s = state["sessions"].get(name)
        if s is None:
            s = new_session(name)
        if state["fixed"]:      # the events carry the name the client uses
            s.name = tok.split(":", 1)[0]
            s.simulate_code = True
        return s

    def ok(**kw):
        return web.json_response(kw)

    @sio.event
    async def connect(sid, environ, auth):
        # Any error here refuses the connection: python-socketio already registered
        # the socket before calling this handler, and any exception would leave
        # it connected without a credential.
        try:
            token = auth.get("token") if isinstance(auth, dict) else None
            cred = token if isinstance(token, str) and token else environ.get("HTTP_APIKEY", "")
            if not authentication.valid_credential(secret, cred):
                raise socketio.exceptions.ConnectionRefusedError("no credential")
            # the room is the name of the already checked token, never `auth.session`,
            # which the client writes by itself; Session.name is the same name (also in --single-session)
            name = authentication.credential_name(cred)
            await sio.enter_room(sid, name)
            await sio.save_session(sid, {"name": name})     # whose microphone a frame is
        except socketio.exceptions.ConnectionRefusedError:
            raise
        except Exception:
            raise socketio.exceptions.ConnectionRefusedError("no credential")
        log.info("socket connected (%s)", redact(name))

    @sio.event
    async def disconnect(sid, reason=None):
        # python-socketio says why (ping timeout, transport closed, the client
        # leaving): a client that keeps resynchronizing is diagnosed from this.
        log.info("socket disconnected (%s)", reason or "no reason given")

    async def generate_token(req):
        name = req.match_info["session"]
        if not inputs.valid_session_name(name):
            return web.json_response({"status": "error", "message": "invalid session name"},
                                     status=400)
        tok = authentication.session_token(secret, name)
        return web.json_response({"status": "success", "session": name,
                                  "token": tok, "full": f"{name}:{tok}"})

    async def start_session(req):
        s = session(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        # begin() waits for the old thread to finish: never inside the aiohttp loop
        await asyncio.get_running_loop().run_in_executor(None, s.begin, body.get("phone"))
        mark_active(key_of(req), True)
        if s.status == "CONNECTED":     # already logged in: the client expects the notices
            if body.get("phone") and s.simulate_code:
                await sio.emit("phoneCode", {"session": s.name, "data": "TEST-0000"},
                               room=s.name)
                await asyncio.sleep(2)
            await sio.emit("session-logged", {"session": s.name, "status": True},
                           room=s.name)
        return ok(status=s.status, qrcode=None, urlcode=s.urlcode)

    async def status_session(req):
        s = session(req)
        return ok(status=s.status, qrcode=qr_data_url(s.urlcode) if s.urlcode else None,
                  urlcode=s.urlcode, session=s.name)

    async def check_connection(req):
        s = session(req)
        is_up = False
        if s.status == "CONNECTED" and s.client is not None:
            # the cached state is not enough: after a network drop it stays CONNECTED
            try:
                is_up = bool(await asyncio.get_running_loop().run_in_executor(
                    None, lambda: s.client.is_connected))
            except Exception:
                log.exception("check-connection-session")
        return ok(status=is_up, message="Connected" if is_up else "Disconnected")

    async def close_session(req):
        await asyncio.get_running_loop().run_in_executor(None, session(req).stop)
        mark_active(key_of(req), False)
        return ok(status=True, message="Session successfully closed")

    async def logout_session(req):
        await asyncio.get_running_loop().run_in_executor(None, session(req).leave)
        mark_active(key_of(req), False)
        return ok(status=True, message="Session successfully closed")

    async def host_device(req):
        s = session(req)
        http, body = session_lifecycle.host_device_response(s.status, s.me, s.lid)
        return web.json_response(body, status=http)

    async def empty_ok(req):
        return ok(status="success", response={})

    def wpp_id(jid):
        user, _, server = msgmap.jid_wpp(jid).partition("@")
        return {"server": server, "user": user,
                "_serialized": f"{user}@{server}"}

    async def list_chats(req):
        s = session(req)
        threading.Thread(target=s.fetch_changes, daemon=True).start()

        def listing():
            # All of it off the event loop: the store's reads wait on its lock
            # (held by history blocks and receipts), and the number behind an
            # LID comes from neonize's store. Read on the loop, they stood it
            # still for seconds on every resync, and sends waited behind it.
            chat_rows = s.db.chats()
            names, nicknames = chat_listing.listing_identities(
                chat_rows, s.db.contacts(), s._phone_of_lid)
            return [chat_listing.chat_to_wpp(c, names, wpp_id, nicknames=nicknames)
                    for c in chat_rows]

        return web.json_response(await in_thread(listing))

    async def all_contacts(req):
        s = session(req)
        mine = {msgmap.jid_wpp(j) for j in (s.me, s.lid) if j}
        # the number behind an LID comes from neonize's store: off the event loop
        return ok(status="success", response=await in_thread(
            contacts.address_book_records, s.db.contacts(), wpp_id, mine,
            s._phone_of_lid, s._lid_of_phone))

    async def get_messages(req):
        s = session(req)
        q = req.query
        before = q.get("id") if q.get("direction") == "before" else None
        if before:   # it comes serialized: true_<chat>_<ID>[_author]
            parts = before.split("_")
            before = parts[2] if len(parts) > 2 else before
        jid = req.match_info["phone"]
        if "@" not in jid:
            jid += "@c.us"
        # the chat may be stored by the number or by the LID (or both)
        keys = await in_thread(s.chat_keys, jid) or [msgmap.jid_whatsmeow(jid)]
        count = int(q.get("count", 20))

        def read():
            # off the loop: up to hundreds of messages, each with its group ack
            # read from the store, used to hold the loop (and the client's
            # socket) for as long as the store took
            return s.db.for_response(s.db.messages(keys, count, before), s.message_ack)
        return ok(status="success", response=await in_thread(read))

    async def send_message(req):
        s = session(req)
        body = await req.json()
        if s.status != "CONNECTED" or s.client is None:
            return responses.disconnected()
        from neonize.utils import build_jid
        phone = body["phone"]
        if isinstance(phone, list):      # WinZapp sends a list of destinations
            phone = phone[0]
        if "@" in phone:
            u, _, srv = msgmap.jid_whatsmeow(phone).partition("@")
            destination = build_jid(u, srv)
        else:
            destination = build_jid(phone)
        content = body["message"]
        preview = msgmap.link_preview(body.get("options"))
        if preview:      # the link card the client already built
            from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ExtendedTextMessage, Message
            content = Message(extendedTextMessage=ExtendedTextMessage(text=content, **preview))
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, lambda: s.client.send_message(destination, content))
        except Exception as error:
            log.exception("send-message")
            return web.json_response({"status": "error", "message": str(error)}, status=500)
        return store_sent(s, destination, resp)

    async def health(req):
        # the client (wpp_update_health.wait_for_api) expects {"message": "OK"}, like Node
        return web.json_response({"message": "OK"})

    r = app.router
    r.add_post("/api/{session}/{key}/generate-token", generate_token)
    r.add_post("/api/{tok}/start-session", start_session)
    r.add_get("/api/{tok}/status-session", status_session)
    r.add_get("/api/{tok}/check-connection-session", check_connection)
    r.add_post("/api/{tok}/close-session", close_session)
    r.add_post("/api/{tok}/logout-session", logout_session)
    r.add_get("/api/{tok}/host-device", host_device)
    async def pn_lid(req):
        """Number <-> LID mapping (neonize keeps it in the device's storage)."""
        s = session(req)
        jid = msgmap.jid_whatsmeow(req.match_info["jid"].replace("~", ""))
        if s.client is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=503)
        from neonize.utils import build_jid
        user, _, server = jid.partition("@")
        target = build_jid(user, server)

        def query():
            try:
                if server == "lid":
                    return jid_text(s.client.get_pn_from_lid(target)), jid
                return jid, jid_text(s.client.get_lid_from_pn(target))
            except Exception:
                return "", ""
        phone, lid = await asyncio.get_running_loop().run_in_executor(None, query)
        if not phone or not lid:
            return web.json_response({"status": "error", "message": "mapping not found"}, status=404)
        found = s.db.contact_of(lid, *contacts.equivalent_jids(phone))
        return web.json_response(contacts.pn_lid_response(phone, lid, found))

    async def blocklist(req):
        """The blocked ones, in WPPConnect's format. A failure does NOT become an empty list:
        the client replaces the list it kept with the response and stores it."""
        s = session(req)
        if s.client is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=503)

        def query():
            items = []
            for j in s.client.get_blocklist().JIDs:
                phone = ""
                if j.Server == "lid":      # the client reads `phone` as a phone
                    try:
                        phone = jid_text(s.client.get_pn_from_lid(j))
                    except Exception:
                        phone = ""
                items.append(contacts.blocklist_entry(jid_text(j), phone))
            return items
        try:
            items = await asyncio.get_running_loop().run_in_executor(None, query)
        except Exception as error:
            log.warning("blocklist: %s", type(error).__name__)
            return web.json_response({"status": "error", "message": "blocklist unavailable"}, status=502)
        return ok(status="success", response=items)

    async def history_status(req):
        """history-sync-status and unblock-history-sync: the history is only
        called complete when the progress reaches 100 or a while passes with no
        new block (history_progress.py)."""
        s = session(req)
        counts = s.db.counts()
        state = history_progress.progress_of(s, time.monotonic()).state(
            time.monotonic(), counts["chat"])
        return ok(status="success", response={
            "backendWorkerBridgeReady": True, **state,
            "unprocessed": state["unprocessedChunks"], "restarted": False, "removed": [],
            "onDemandEnabled": False, "chunkStatus": {}, "storeCounts": counts})

    async def send_seen(req):
        s = session(req)
        body = await req.json()
        phone = body.get("phone")
        phone = phone[0] if isinstance(phone, list) else phone
        error = without_connection(s)
        if error is not None:
            return error
        # the app only accepts the confirmation in this format: response.data = [true], and
        # only undoes the local read when it is missing. It tries the chat's addresses
        # (number, LID) one by one: what the bridge does not know is [false]
        keys = await in_thread(s.chat_keys, phone) if phone else []
        if not keys:
            return ok(status="success", response={"data": [False]})
        loop = asyncio.get_running_loop()
        if body.get("unread"):        # "mark as unread"
            await loop.run_in_executor(marking, s.mark_unread, keys[0])
            confirmed = True
        else:
            s.open_chat = keys[0]      # the app only sends send-seen for the chat the user opened
            confirmed = True
            for key in keys:
                _, sent = await loop.run_in_executor(marking, s.read_and_confirm, key, True)
                confirmed = confirmed and sent
        return ok(status="success", response={"data": [confirmed]})

    async def subscribe_presence(req):
        """Subscribes to a person's presence. Only confirms what WhatsApp accepted: the
        client marks the target as subscribed when it sees `success`. Does not write `s.open_chat`
        (the client subscribes to many contacts, not just the open chat)."""
        s = session(req)
        body = await req.json()
        phone = body.get("phone")
        phone = phone[0] if isinstance(phone, list) else phone
        target = contacts.subscription_target(phone)
        if not target:
            return web.json_response({"status": "error", "message": "invalid contact"}, status=400)
        if s.client is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=503)
        from neonize.utils import build_jid
        user, _, server = target.partition("@")
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, s.client.subscribe_presence, build_jid(user, server))
        except Exception as error:
            log.info("subscribe_presence refused (%s) for %s", type(error).__name__, redact(target))
            return web.json_response({"status": "error", "message": "subscribe refused"}, status=502)
        return ok(status="success", response={})

    async def set_online(req):
        s = session(req)
        body = await req.json()
        s.online = bool(body.get("isOnline"))
        if s.client is not None and s.status == "CONNECTED":
            from neonize.utils.enum import Presence
            presence = Presence.AVAILABLE if s.online else Presence.UNAVAILABLE
            try:    # without this the contacts never see the account online/offline
                await asyncio.get_running_loop().run_in_executor(
                    None, s.client.send_presence, presence)
            except Exception as error:
                log.info("set-online-presence refused (%s)", type(error).__name__)
                return web.json_response({"status": "error", "message": "presence refused"}, status=502)
        if s.online and s.open_chat:      # back in focus: reads what arrived in the meantime
            await asyncio.get_running_loop().run_in_executor(None, s.mark_read, s.open_chat)
        return empty_ok_resp()

    def empty_ok_resp():
        return ok(status="success", response={})

    async def get_media(req):
        """Downloads and decrypts the media of a stored message (serialized id
        'false_<chat>_<ID>[_author]'); raw bytes or JSON with base64."""
        s = session(req)
        parts = req.match_info["msgid"].split("_")
        msg_id = parts[2] if len(parts) > 2 else parts[-1]
        record = s.db.media_record(msg_id)
        if record is None:
            return web.json_response({"status": "error", "message": "media not found"}, status=404)
        if s.client is None or s.status != "CONNECTED":
            return responses.disconnected()
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
        message = Message.FromString(record)
        try:
            body = await req.json()
        except Exception:
            body = None            # the body is optional: it only carries the progressId
        try:
            data = await transfers.download_with_progress_async(
                s.client, message, body, s.emit, s.name)
        except Exception as error:
            log.exception("download")
            text = str(error).lower()
            # media that left WhatsApp's server: the app treats 410 as "expired"
            status = 410 if ("404" in text or "410" in text or "not found" in text) else 500
            return web.json_response({"status": "error", "message": str(error)}, status=status)
        if "octet-stream" in req.headers.get("Accept", ""):
            return web.Response(body=data, content_type="application/octet-stream")
        import base64 as b64
        return ok(base64=b64.b64encode(data).decode())

    def destination_of(phone):
        from neonize.utils import build_jid
        phone = phone[0] if isinstance(phone, list) else phone
        if "@" in phone:
            u, _, srv = msgmap.jid_whatsmeow(phone).partition("@")
            return build_jid(u, srv)
        return build_jid(phone)

    def store_sent(s, destination, resp, text_or_msg=None):
        """Stores the message just sent and returns WPPConnect's response."""
        chat = jid_text(destination)
        w = msgmap.build(chat=chat, from_me=True, id=resp.ID, sender=s.me,
                         ts=resp.Timestamp or time.time(), message=resp.Message)
        if w is not None:
            s.db.message(chat, w, msgmap.media_record_of(resp.Message))
            s.db.on_arrival(chat, w["t"], True)
        serial = w["id"]["_serialized"] if w else f"true_{msgmap.jid_wpp(chat)}_{resp.ID}"
        chat_w = msgmap.jid_wpp(chat)
        # WPPConnect says "sent" (ack 1) right after the send; the app only
        # registers the message after the HTTP response, hence the small delay
        asyncio.get_running_loop().call_later(0.6, lambda: asyncio.ensure_future(
            sio.emit("onack", {"session": s.name, "ack": 1, "to": chat_w, "from": chat_w,
                               "id": {"fromMe": True, "remote": chat_w, "id": resp.ID,
                                      "_serialized": serial}}, room=s.name)))
        return ok(status="success", response=[{"id": serial, "ack": 1}])

    def quoted_context(s, quoted, ctx=None):
        """ContextInfo that quotes the message `quoted` (serialized id), or None if
        it is not stored."""
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ContextInfo
        parts = quoted.split("_")
        quoted_id = parts[2] if len(parts) > 2 else parts[-1]
        original = s.db.wpp_message(quoted_id)
        if original is None:
            return None
        ctx = ctx if ctx is not None else ContextInfo()
        ctx.stanzaID = quoted_id
        ctx.participant = msgmap.quoted_participant(original, s.me)
        ctx.quotedMessage.CopyFrom(msgmap.quoted(original))
        return ctx

    def send_media(s, destination, kind, file, caption, name, mime, ptt, ctx=None,
                   client=None):
        c = s.client if client is None else client
        # audio and video: not neonize's build_*_message, which need ffprobe
        # (WinZapp does not ship it)
        if kind in ("audio", "video"):
            if kind == "audio":
                m, field = audio_message.build(c, file, ptt), "audioMessage"
            else:
                m, field = video_message.build(c, file, caption or None), "videoMessage"
            if ctx is not None:
                getattr(m, field).contextInfo.CopyFrom(ctx)
            return c.send_message(destination, m)
        if ctx is not None:     # with a quote: builds the message, puts the context and sends
            if kind == "image":
                m, field = c.build_image_message(file, caption=caption or None), "imageMessage"
            else:
                m, field = c.build_document_message(
                    file, caption=caption or None, filename=name or None,
                    mimetype=mime or None), "documentMessage"
            getattr(m, field).contextInfo.CopyFrom(ctx)
            return c.send_message(destination, m)
        if kind == "image":
            return c.send_image(destination, file, caption=caption or None)
        return c.send_document(destination, file, caption=caption or None,
                               filename=name or None, mimetype=mime or None)

    async def send_file(req):
        """Multipart send (image, video, audio, document) like WPPConnect."""
        import tempfile
        s = session(req)
        if s.status != "CONNECTED" or s.client is None:
            return responses.disconnected()
        fields, path, mime = {}, None, ""
        reader = await req.multipart()
        try:
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.filename is not None:      # the file, straight to disk
                    suffix = os.path.splitext(part.filename)[1]
                    mime = part.headers.get("Content-Type", "")
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
                        path = f.name
                        while True:
                            chunk = await part.read_chunk(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                else:
                    fields.setdefault(part.name, []).append((await part.text()))
            if path is None:
                return web.json_response({"status": "error", "message": "file missing"}, status=400)

            def one(name, default=""):
                return (fields.get(name) or [default])[0]
            destination = destination_of(fields.get("phone") or fields.get("phone[]") or [""])
            ctx = quoted_context(s, one("quotedMessageId")) if one("quotedMessageId") else None
            if one("quotedMessageId") and ctx is None:
                log.info("quoted message not stored: send refused")
                return responses.missing_quote()
            resp = await transfers.send_with_progress_async(
                s.client, path, one("uploadId"), s.emit, s.name,
                lambda c: send_media(s, destination, one("type", "document"), path,
                                     one("caption"), one("filename"), mime,
                                     one("isPtt") == "true", ctx, client=c))
            return store_sent(s, destination, resp)
        except Exception as error:
            log.exception("send-file")
            return web.json_response({"status": "error", "message": str(error)}, status=500)
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    async def send_voice_base64(req):
        import base64 as b64
        s = session(req)
        body = await req.json()
        if s.status != "CONNECTED" or s.client is None:
            return responses.disconnected()
        data = b64.b64decode(body["base64Ptt"].split(",", 1)[-1])
        destination = destination_of(body["phone"])
        ctx = quoted_context(s, body["quotedMessageId"]) if body.get("quotedMessageId") else None
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, send_media, s, destination, "audio", data, "", "", "", True, ctx)
        except Exception as error:
            log.exception("send-voice-base64")
            return web.json_response({"status": "error", "message": str(error)}, status=500)
        return store_sent(s, destination, resp)

    async def publish_status(req, build_and_send):
        """Posts a status: `build_and_send(s, destination, body)` returns the send."""
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        destination = destination_of("status@broadcast")
        try:
            resp = await in_thread(build_and_send, s, destination, body)
        except Exception as failure:
            log.exception("publish status")
            return web.json_response({"status": "error", "message": str(failure)}, status=500)
        chat = jid_text(destination)
        w = msgmap.build(chat=chat, from_me=True, id=resp.ID, sender=s.me,
                         ts=resp.Timestamp or time.time(), message=resp.Message)
        serial = f"true_status@broadcast_{resp.ID}"
        if w is not None:
            s.db.message(chat, w, msgmap.media_record_of(resp.Message))
            serial = w["id"]["_serialized"]
        # the app checks the sendMsgResult.messageSendResult of each item
        return ok(status="success", response=[{
            "id": serial, "ack": 1, "sendMsgResult": {"messageSendResult": "OK"}}])

    def file_from_body(body):
        """Bytes of the path field (base64, never a disk path)."""
        return inputs.body_bytes(body)

    def text_status(s, destination, body):
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ExtendedTextMessage, Message
        options = body.get("options") or {}
        return s.client.send_message(destination, Message(extendedTextMessage=ExtendedTextMessage(
            text=body["text"], textArgb=0xFFFFFFFF,
            backgroundArgb=msgmap.argb_color(options.get("backgroundColor")),
            font=int(options.get("font") or 0))))

    def image_status(s, destination, body):
        return send_media(s, destination, "image", file_from_body(body),
                          body.get("caption") or "", "", "", False)

    def video_status(s, destination, body):
        return send_media(s, destination, "video", file_from_body(body),
                          body.get("caption") or "", "", "", False)

    def voice_status(s, destination, body):
        import base64 as b64
        data = b64.b64decode(body["base64Ptt"].split(",", 1)[-1])
        return send_media(s, destination, "audio", data, "", "", "", True)

    async def send_text_storie(req):
        return await publish_status(req, text_status)

    async def send_image_storie(req):
        return await publish_status(req, image_status)

    async def send_video_storie(req):
        return await publish_status(req, video_status)

    async def send_status_voice(req):
        return await publish_status(req, voice_status)

    async def statuses(req):
        return ok(status="success", response=session(req).status_wpp())

    async def delete_status(req):
        s = session(req)
        if not session_lifecycle.deletable_status(s.db.wpp_message(req.match_info["id"])):
            # expired, already deleted or never mine: nothing to delete (the client reads the `code`)
            return web.json_response({"status": "error", "code": "not_found",
                                      "retrySafe": False}, status=404)
        error = without_connection(s)
        if error is not None:
            return error
        done = await in_thread(s.delete_status, req.match_info["id"])
        if not done:
            return web.json_response({"status": "error", "code": "status_delete_failed"},
                                     status=500)
        return ok(status="success")

    def jid_from_text(text):
        from neonize.utils import build_jid
        u, _, srv = msgmap.jid_whatsmeow(text).partition("@")
        return build_jid(u, srv)

    def group_to_wpp(g):
        """neonize GroupInfo -> the dictionary WinZapp reads in group-info."""
        participants = []
        for p in g.Participants:
            phone = jid_text(p.PhoneNumber)
            jid = phone or jid_text(p.JID) or jid_text(p.LID)
            participants.append({
                "id": jid, "lid": jid_text(p.LID),
                "admin": "superadmin" if p.IsSuperAdmin else ("admin" if p.IsAdmin else None),
                "isAdmin": bool(p.IsAdmin or p.IsSuperAdmin),
                "isSuperAdmin": bool(p.IsSuperAdmin)})
        owner = jid_text(g.OwnerPN) or jid_text(g.OwnerJID)
        return {
            "id": jid_text(g.JID), "name": g.GroupName.Name, "subject": g.GroupName.Name,
            "desc": g.GroupTopic.Topic, "announce": bool(g.GroupAnnounce.IsAnnounce),
            "restrict": bool(g.GroupLocked.isLocked), "owner": owner,
            "creation": msgmap.seconds(g.GroupCreated),
            # a community's announcement group (the client offers replies there)
            "defaultSubgroup": bool(g.GroupIsDefaultSub.IsDefaultSubGroup),
            "participants": participants}

    async def group_info(req):
        s = session(req)
        if s.client is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=400)
        jid = jid_from_text(req.match_info["jid"])
        try:
            g = await asyncio.get_running_loop().run_in_executor(
                None, s.client.get_group_info, jid)
        except Exception as error:
            refusal = groups.info_refusal(error)
            log.info("group-info failed: %s%s", type(error).__name__,
                     f" ({refusal})" if refusal else "")
            return web.json_response({"status": "error", "message": str(error),
                                      "code": refusal or None}, status=404)
        info = group_to_wpp(g)
        community = communities.community_counted(g)
        if community:
            try:
                members = await asyncio.get_running_loop().run_in_executor(
                    None, s.client.get_linked_group_participants, jid_from_text(community))
                info["size"] = communities.member_total(members) or None
            except Exception as error:
                log.info("group-info: community members failed: %s", type(error).__name__)
        if info["name"]:
            s.db.chat(info["id"], name=info["name"])
        return ok(status="success", response=info)

    async def group_action(req, func):
        s = session(req)
        body = await req.json()
        if s.client is None or s.status != "CONNECTED":
            return web.json_response({"status": "error", "message": "Disconnected"}, status=400)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, func, s.client, jid_from_text(body["groupId"]), body)
        except Exception as error:
            log.exception("group")
            return web.json_response({"status": "error", "message": str(error)}, status=500)
        return ok(status="success", response={})

    async def leave_group(req):
        return await group_action(req, lambda c, jid, body: c.leave_group(jid))

    async def group_subject(req):
        return await group_action(req, lambda c, jid, body: c.set_group_name(jid, body["title"]))

    async def contact_profile(req):
        """A contact's profile (name, nickname, business account and, for an LID, the number)."""
        s = session(req)
        jid = msgmap.jid_whatsmeow(req.match_info["jid"].replace("~", ""))
        user, _, server = jid.partition("@")
        lid = jid if server == "lid" else ""
        phone = "" if server == "lid" else jid
        verified = ""
        if s.client is not None and s.status == "CONNECTED" and server in ("lid", "s.whatsapp.net"):
            from neonize.utils import build_jid
            loop = asyncio.get_running_loop()
            if lid:
                try:
                    pn = await loop.run_in_executor(
                        None, s.client.get_pn_from_lid, build_jid(user, "lid"))
                    phone = jid_text(pn)
                except Exception:
                    phone = ""
            else:   # the nickname may have been stored only under the LID
                try:
                    lid = jid_text(await loop.run_in_executor(
                        None, s.client.get_lid_from_pn, build_jid(user, server)))
                except Exception:
                    lid = ""
            if phone:    # verified name of a business account, from neonize's local store
                try:
                    u, _, srv = phone.partition("@")
                    info = await loop.run_in_executor(
                        None, s.client.contact.get_contact, build_jid(u, srv))
                    verified = (info.BusinessName or "").strip()
                except Exception:
                    verified = ""
        found = s.db.contact_of(jid, lid, *contacts.equivalent_jids(phone))
        return ok(status="success", response=contacts.contact_body(jid, phone, found, verified))

    r.add_get("/api/{tok}/contact/{jid}", contact_profile)
    r.add_get("/api/{tok}/group-info/{jid}", group_info)
    r.add_post("/api/{tok}/leave-group", leave_group)
    r.add_post("/api/{tok}/group-subject", group_subject)
    groups.register_routes(r, session=session, ok=ok, jid_from_text=jid_from_text,
                           jid_text=jid_text, qr_data_url=qr_data_url)
    commerce_routes.register_routes(r, session=session, ok=ok, jid_from_text=jid_from_text,
                                    jid_text=jid_text, destination_of=destination_of,
                                    store_sent=store_sent, quoted_context=quoted_context)
    channels.register_routes(r, session=session, ok=ok, jid_from_text=jid_from_text,
                             jid_text=jid_text)
    administration.register_routes(r, session=session, ok=ok, state=state, folder=folder,
                                   mark_active=mark_active, qr_data_url=qr_data_url)
    cleanup.register_routes(r, ok=ok, state=state, folder=folder, mark_active=mark_active)
    calls.register_routes(r, session=session, ok=ok)
    ongoing_calls.register_routes(r, session=session, ok=ok)
    presence.register_routes(r, session=session, ok=ok)
    labels.register_routes(r, session=session, ok=ok, jid_from_text=jid_from_text,
                           jid_text=jid_text)
    communities.register_routes(r, session=session, ok=ok, jid_from_text=jid_from_text)
    comment_routes.register_routes(r, session=session, ok=ok, jid_from_text=jid_from_text)
    groups_extra.register_routes(r, session=session, ok=ok, jid_from_text=jid_from_text,
                                 jid_text=jid_text, qr_data_url=qr_data_url)
    profile_privacy.register_routes(r, session=session, ok=ok, jid_from_text=jid_from_text,
                                    jid_text=jid_text)
    contacts.register_routes(r, session=session, ok=ok, jid_from_text=jid_from_text,
                             jid_text=jid_text, destination_of=destination_of,
                             store_sent=store_sent, quoted_context=quoted_context)
    remaining.register_routes(r, session=session, ok=ok, jid_from_text=jid_from_text,
                              jid_text=jid_text, destination_of=destination_of,
                              store_sent=store_sent, send_media=send_media)
    chats.register_routes(r, session=session, ok=ok, wpp_id=wpp_id, destination_of=destination_of,
                          store_sent=store_sent, send_media=send_media,
                          quoted_context=quoted_context)
    chat_remaining.register_routes(r, session=session, ok=ok, destination_of=destination_of,
                                   quoted_context=quoted_context,
                                   publish_status=publish_status,
                                   text_status=text_status)
    async def text_with_context(req, with_mentions):
        """send-reply and send-mentioned: text quoting a message and/or mentioning people."""
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
            ContextInfo, ExtendedTextMessage, Message)
        s = session(req)
        body = await req.json()
        if s.status != "CONNECTED" or s.client is None:
            return responses.disconnected()
        destination = destination_of(body["phone"])
        ctx = ContextInfo()
        quoted = body.get("messageId")
        if quoted and quoted_context(s, quoted, ctx) is None:
            return responses.missing_quote()
        if with_mentions:
            ctx.mentionedJID.extend(msgmap.jid_whatsmeow(j) for j in body.get("mentioned", []))
        message = Message(extendedTextMessage=ExtendedTextMessage(
            text=body["message"], contextInfo=ctx,
            **(msgmap.link_preview(body.get("options")) or {})))
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, s.client.send_message, destination, message)
        except Exception as error:
            log.exception("send-reply")
            return web.json_response({"status": "error", "message": str(error)}, status=500)
        return store_sent(s, destination, resp)

    async def send_reply(req):
        return await text_with_context(req, False)

    async def send_mentioned(req):
        return await text_with_context(req, True)

    r.add_post("/api/{tok}/send-text-storie", send_text_storie)
    r.add_post("/api/{tok}/send-image-storie", send_image_storie)
    r.add_post("/api/{tok}/send-video-storie", send_video_storie)
    r.add_post("/api/{tok}/send-status-voice-base64", send_status_voice)
    r.add_get("/api/{tok}/statuses", statuses)
    r.add_post("/api/{tok}/status/{id}/delete", delete_status)
    r.add_post("/api/{tok}/send-reply", send_reply)
    r.add_post("/api/{tok}/send-mentioned", send_mentioned)
    r.add_post("/api/{tok}/send-file", send_file)
    r.add_post("/api/{tok}/send-voice-base64", send_voice_base64)
    r.add_get("/api/{tok}/get-media-by-message/{msgid}", get_media)
    r.add_post("/api/{tok}/get-media-by-message/{msgid}", get_media)
    async def archive_chat(req):
        s = session(req)
        body = await req.json()
        phone = body.get("phone")
        phone = phone[0] if isinstance(phone, list) else phone
        if s.status != "CONNECTED" or s.client is None:
            return responses.disconnected()
        done = await on_chat_state(s, phone, s.archive, bool(body.get("value", True)))
        if not done:
            return web.json_response({"status": "error", "message": "not accepted"}, status=500)
        return ok(status="success", response={})

    async def pin_chat(req):
        s = session(req)
        body = await req.json()
        phone = body.get("phone")
        phone = phone[0] if isinstance(phone, list) else phone
        if s.status != "CONNECTED" or s.client is None:
            return responses.disconnected()
        pin = str(body.get("state", "true")).lower() == "true"
        done = await on_chat_state(s, phone, s.pin, pin)
        if not done:
            return web.json_response({"status": "error", "message": "not accepted"}, status=500)
        return ok(status="success", response={})

    async def in_thread(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def without_connection(s):
        return responses.without_connection(s)

    async def on_chat_state(s, phone, action, value):
        """Archive, pin or mute: `action(key, value)` on the key under which the
        chat is stored (the number or the LID; with no row, the given one) and, if
        WhatsApp accepted, the same state on its other keys. Without that the
        action created an empty row under the wrong address and the next
        list-chats came back as it was."""
        jid = msgmap.jid_whatsmeow(phone)
        keys = await in_thread(s.chat_keys, jid)
        target = keys[0] if keys else jid
        done = await in_thread(action, target, value)
        if done:
            for other in keys[1:]:
                s.db.copy_state(target, other)
        return done
    async def star_message(req):
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        done = await in_thread(s.star, body["messageId"], bool(body.get("star", True)))
        if not done:
            return web.json_response({"status": "error", "message": "not accepted"}, status=500)
        return ok(status="success", response={"star": bool(body.get("star", True))})

    async def message_by_id(req):
        s = session(req)
        serial = req.match_info["msgid"]
        _, _, mid, _ = s.id_parts(serial)
        original = s.db.wpp_message(mid)
        if original is None:
            return web.json_response({"status": "error", "message": "message not found"}, status=404)
        # {data: message}, like the real server (main_window/calls.py only reads .data)
        return ok(status="success", response={"data": s.db.for_response([original], s.message_ack)[0]})

    async def send_mute(req):
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        amount = float(body.get("time") or 0)
        unit = {"minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800,
                "months": 2592000, "years": 31536000}.get(str(body.get("type", "hours")), 3600)
        seconds = int(amount * unit)
        if seconds >= 8766 * 3600:
            seconds = -1                       # "forever"
        phone = body.get("phone")
        phone = phone[0] if isinstance(phone, list) else phone
        done = await on_chat_state(s, phone, s.mute, seconds)
        if not done:
            return web.json_response({"status": "error", "message": "not accepted"}, status=500)
        return ok(status="success", response={"isMuted": seconds != 0})

    async def send_poll_message(req):
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        from neonize.utils.enum import VoteType
        phone = body["phone"]
        phone = phone[0] if isinstance(phone, list) else phone
        if body.get("isGroup") and "@" not in phone:
            phone += "@g.us"
        destination = destination_of(phone)
        multiple = int((body.get("options") or {}).get("selectableCount", 1)) != 1

        def send():
            created = s.client.build_poll_vote_creation(
                body["name"], list(body["choices"]),
                VoteType.MULTIPLE if multiple else VoteType.SINGLE)
            return s.client.send_message(destination, created)
        try:
            resp = await in_thread(send)
        except Exception as send_error:
            log.exception("send-poll-message")
            return web.json_response({"status": "error", "message": str(send_error)},
                                     status=500)
        return store_sent(s, destination, resp)

    async def poll_votes(req):
        s = session(req)
        body = await req.json()
        response = {}
        for serial in body.get("messageIds") or []:
            vote_rows = s.votes_wpp(serial)
            if vote_rows is not None:
                response[serial] = vote_rows
        return ok(status="success", response=response)

    async def poll_voters(req):
        s = session(req)
        body = await req.json()
        response = s.voters_wpp(body.get("messageId") or "")
        if not response.get("ok"):
            return web.json_response({"status": "error", "code": response["error"]}, status=503)
        return ok(status="success", response=response)

    async def vote_poll(req):
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        done = await in_thread(s.vote_in_poll, body["messageId"],
                               [int(i) for i in body.get("localIds") or []])
        if not done:
            return web.json_response({"status": "error", "code": "poll_vote_failed"}, status=503)
        return ok(status="success")

    async def edit_message(req):
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        # refusals that happen before anything is sent: the client only undoes the
        # optimistic text when the body says so (responses.edit_refused)
        from_me, _, mid, _ = s.id_parts(body["id"])
        reason = message_rules.refusal_reason(from_me, s.db.wpp_message(mid), time.time())
        if reason:
            return responses.edit_refused(reason)
        mentions = (body.get("options") or {}).get("mentionedJidList") or []
        done = await in_thread(s.edit, body["id"], body["newText"], mentions)
        if not done:
            return web.json_response({"status": "error", "message": "not accepted"}, status=500)
        return ok(status="success", response={"edited": True})

    async def forward_messages(req):
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        phone = body["phone"]
        phone = phone[0] if isinstance(phone, list) else phone
        destination = destination_of(phone)
        ids = body["messageId"]
        ids = ids if isinstance(ids, list) else [ids]
        sent, new_ids = [], []
        for serial in ids:
            msg = await in_thread(s.message_to_forward, serial,
                                  bool(body.get("keepVoice")))
            if msg is None:
                return web.json_response(
                    {"status": "error", "message": "message type not yet forwardable"},
                    status=501)
            try:
                resp = await in_thread(s.client.send_message, destination, msg)
            except Exception as send_error:
                log.exception("forward")
                return web.json_response({"status": "error", "message": str(send_error)},
                                         status=500)
            sent.append(store_sent(s, destination, resp))
            new_ids.append(resp.ID)
        # No echo: the real server announces the forwarding through received-message and
        # the client counts on that to update the destination chat, but here
        # the notice is risky (the client matches the echo by type and may swap the id of
        # a pending send of the same type in the same chat). Without it, the destination
        # chat and the preview only change at the client's next list-chats; the new ids
        # go in the response so the client can update the chat when it
        # receives the success (it does not do that yet).
        return ok(status="success", response=[{"forwarded": len(sent),
                                               "messageIds": new_ids}])

    async def older_history(req):
        """neonize has no on-demand history request (the one of the 'click
        here to see older messages'): says the rest only exists on the
        phone, and the app stops asking. The history that arrives is the pairing's."""
        return ok(status="success", response={
            "requested": False, "primaryHasMore": True, "phoneOnly": True})

    async def react_message(req):
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        done = await in_thread(s.react, body["msgId"], body.get("reaction") or "")
        if not done:
            return web.json_response({"status": "error", "message": "not accepted"}, status=500)
        return ok(status="success", response={"sendMsgResult": "ok"})

    async def reactions(req):
        s = session(req)
        return ok(status="success", response=s.reactions_wpp(req.match_info["id"]))

    async def send_capabilities(req):
        # `calls`: this session places, answers and carries voice calls;
        # `videoCalls` and `groupCalls`: video and group calls too (the client
        # only offers each then)
        media = getattr(session(req), "call_media", None)
        calls_ready = media is not None and media.calls.enabled()
        can = calls.capabilities(media)
        return ok(status="success", response={
            "compatible": True, "calls": calls_ready,
            "videoCalls": calls_ready and can["video"],
            "groupCalls": calls_ready and can["groupCalls"]})

    async def media_of_socket(sid):
        try:
            name = (await sio.get_session(sid)).get("name")
        except KeyError:
            return None
        s = state["sessions"].get(name) if name else None
        return getattr(s, "call_media", None) if s is not None else None

    @sio.on("call:audio:mic")
    async def call_audio_mic(sid, data):
        media = await media_of_socket(sid)
        if media is not None:
            media.microphone(data)

    @sio.on("call:audio:stop")
    async def call_audio_stop(sid, data=None):
        media = await media_of_socket(sid)
        if media is not None:
            media.stop_microphone()

    @sio.on("call:video:camera")
    async def call_video_camera(sid, data):
        media = await media_of_socket(sid)
        if media is not None:
            media.camera_frame(data)

    @sio.on("call:video:camera:stop")
    async def call_video_camera_stop(sid, data=None):
        media = await media_of_socket(sid)
        if media is not None:
            await asyncio.get_running_loop().run_in_executor(None, media.camera_stop, data)

    @sio.on("call:video:camera:start")
    async def call_video_camera_start(sid, data=None):
        media = await media_of_socket(sid)
        if media is not None:
            await asyncio.get_running_loop().run_in_executor(None, media.camera_start)

    async def pinned_messages(req):
        """Messages pinned in the requested chats (the bridge keeps the pin and
        unpin notices that go through it). The list is PARTIAL: only what the bridge
        saw go by (the pin notice that arrived or that it sent itself); what was
        pinned before the pairing or with the bridge off does not show up, and the
        client (_apply_pinned_message_flags) treats the list as complete and
        clears the pinInChat of the rest. There is no cheap fix: neonize does not list
        a chat's pinned messages."""
        s = session(req)
        body = await req.json()
        chats = [msgmap.jid_whatsmeow(c) for c in body.get("chatIds") or []]
        pinned = s.db.pinned_of(chats, time.time())
        return ok(status="success", response=[{**w, "pinInChat": True} for w in pinned])

    async def pin_message(req):
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        pin = bool(body.get("pin", True))
        done = await in_thread(s.pin_message, body["messageId"], pin)
        if not done:
            return web.json_response({"status": "error", "message": "not accepted"}, status=500)
        return ok(status="success", response={"messageId": body["messageId"], "pinned": pin})

    async def mark_played(req):
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        done = await in_thread(s.mark_played, body["messageId"])
        return ok(status="success", response={"played": bool(done)})

    async def delete_message(req):
        s = session(req)
        body = await req.json()
        error = without_connection(s)
        if error is not None:
            return error
        ids = body["messageId"]
        ids = ids if isinstance(ids, list) else [ids]
        if body.get("onlyLocal"):
            # WPPConnect's deleteMediaInDevice, true when left out as there
            keep = body.get("deleteMediaInDevice", True) is False
            dones = [await in_thread(s.delete_for_me, i, not keep) for i in ids]
        else:
            # A revoke carries no such choice to the other devices: WhatsApp
            # Web's clearMedia stays on its own copy of the protocol message.
            dones = [await in_thread(s.delete_for_everyone, i) for i in ids]
        if not all(dones):
            return web.json_response({"status": "error", "message": "not accepted"}, status=500)
        return ok(status="success", response={"deleted": len(dones)})

    r.add_post("/api/{tok}/star-message", star_message)
    r.add_get("/api/{tok}/message-by-id/{msgid}", message_by_id)
    r.add_post("/api/{tok}/send-mute", send_mute)
    r.add_post("/api/{tok}/mark-played", mark_played)
    r.add_post("/api/{tok}/react-message", react_message)
    r.add_post("/api/{tok}/edit-message", edit_message)
    r.add_post("/api/{tok}/forward-messages", forward_messages)
    r.add_post("/api/{tok}/request-older-messages/{jid}", older_history)
    r.add_get("/api/{tok}/older-history-state/{jid}", older_history)
    r.add_post("/api/{tok}/send-poll-message", send_poll_message)
    r.add_post("/api/{tok}/poll-votes", poll_votes)
    r.add_post("/api/{tok}/poll-voters", poll_voters)
    r.add_post("/api/{tok}/vote-poll", vote_poll)
    r.add_get("/api/{tok}/send-capabilities", send_capabilities)
    r.add_post("/api/{tok}/pinned-messages", pinned_messages)
    r.add_post("/api/{tok}/pin-message", pin_message)
    r.add_get("/api/{tok}/reactions/{id}", reactions)
    r.add_post("/api/{tok}/delete-message", delete_message)
    async def recover_state(req):
        s = session(req)
        changed = await in_thread(s.request_state_copy)
        return ok(status="success", response={"changes": changed})

    r.add_post("/api/{tok}/recover-state", recover_state)
    r.add_post("/api/{tok}/archive-chat", archive_chat)
    r.add_post("/api/{tok}/pin-chat", pin_chat)
    r.add_post("/api/{tok}/send-seen", send_seen)
    r.add_post("/api/{tok}/set-limit", empty_ok)
    r.add_get("/api/{tok}/contact/pn-lid/{jid}", pn_lid)
    r.add_get("/api/{tok}/blocklist", blocklist)
    r.add_get("/api/{tok}/history-sync-status", history_status)
    r.add_post("/api/{tok}/unblock-history-sync", history_status)
    r.add_post("/api/{tok}/set-online-presence", set_online)
    r.add_post("/api/{tok}/subscribe-presence", subscribe_presence)
    r.add_post("/api/{tok}/list-chats", list_chats)
    r.add_get("/api/{tok}/list-chats", list_chats)
    r.add_get("/api/{tok}/all-contacts", all_contacts)
    r.add_get("/api/{tok}/get-messages/{phone}", get_messages)
    r.add_post("/api/{tok}/send-message", send_message)
    r.add_get("/healthz", health)
    r.add_get("/health", health)
    return app


def turn_off_bot_tag():
    """neonize adds `<bot biz_bot="1"/>` to every private message, and the
    recipient then sees the AI badge with rating buttons, even from an account
    that is not Business. The DLL reads this variable at each send, so it must
    be set in the bridge's own process, whoever launched it."""
    os.environ["NEONIZE_BOT_TAG"] = "off"


def main():
    turn_off_bot_tag()
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=6300)
    ap.add_argument("--single-session", default="",
                    help="all the requested sessions use this account (tests)")
    ap.add_argument("--history-days", type=int, default=180,
                    help="history to ask for on pairing (0 = WhatsApp's default)")
    ap.add_argument("--log-state", action="store_true",
                    help="turns on the technical log of the Go side, filtered for the app state")
    ap.add_argument("--folder", default=os.path.join(os.path.dirname(__file__), "data"))
    a = ap.parse_args()
    global HISTORY_DAYS
    HISTORY_DAYS = a.history_days
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for h in logging.getLogger().handlers:      # the bridge.log gets passed around in bug reports
        h.addFilter(redaction.RedactionFilter())
    if a.log_state:
        import re
        from neonize.utils import log as neonize_log
        neonize_log.setLevel(logging.DEBUG)
        for name in ("whatsmeow", "Whatsmeow"):
            logging.getLogger(name).setLevel(logging.DEBUG)
        pattern = re.compile(r"app ?state|patch|snapshot|key ?share|sync ?key|lthash|mutation|appstate|<receipt|<notification|<message|read-self", re.I)

        class StateOnly(logging.Filter):
            def filter(self, rec):
                return rec.levelno >= logging.WARNING or rec.name == "bridge"                     or bool(pattern.search(rec.getMessage()))
        for h in logging.getLogger().handlers:
            h.addFilter(StateOnly())
    web.run_app(create_app(a.folder, a.single_session), port=a.port, **SERVER_OPTIONS)


if __name__ == "__main__":
    main()
