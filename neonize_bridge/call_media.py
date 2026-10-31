"""Calls carried by the bridge: WinZapp's neonize build places, answers and
hangs up calls through meowcaller (neonize/calls.py,
neonize_patches/neonize/0003, 0005 and 0008), and the audio crosses here.
Voice and video calls, one-to-one or to a group; the video itself is
call_video.py (`video`, None without ffmpeg or on an older build, when video
stays refused).

The client talks to the bridge as it talks to WPPConnect (main_window/calls.py,
core/websocket_client.py):

- routes `call/offer` (`isVideo`), `call/group/offer` {groupJid, isVideo},
  `call/upgrade-video`, `call/accept`, `call/end`, `call/ensure-ended`,
  `call/status`, `call/audio/enable` (calls.py wires them here when a session
  has calls);
- a voice call the other side turns into a video call is accepted at once,
  as WhatsApp Web does (this side's camera stays off until the user turns it
  on), and its `callstate` then says `isVideo`;
- `callstate` events for the call's life (`ACTIVE` makes the client attach its
  audio and say "connected"; ENDED, REJECTED, NOT_ANSWERED, HANDLED_REMOTELY
  and FAILED end it). The offer of an incoming call still comes from
  whatsmeow's CallOfferEv (calls.py), which carries more than meowcaller's;
- audio: the client sends `call:audio:mic` (base64 PCM s16le mono 48 kHz,
  20 ms) and plays `call:audio:remote` (PCM with its `sampleRate`). meowcaller
  works at 16 kHz in 60 ms frames: the microphone is brought down to 16 kHz
  through a streaming anti-alias filter and regrouped here; the other side goes out at 16 kHz and the client
  resamples it.

The pure functions are at the top (tested in test_call_media.py); `CallMedia`
holds a session's calls and the threads that pump events and audio.
"""
import base64
import functools
import logging
import sys
import threading
import time

import msgmap
import ongoing_calls
from call_audio_pcm import PCM48To16
from call_video import VIDEO_ACCEPT, VIDEO_REQUEST

log = logging.getLogger("bridge")

CLIENT_RATE = 48000          # what the client's microphone frames are
CALL_RATE = 16000            # what meowcaller carries
FRAME_BYTES = 1920           # one meowcaller frame: 960 samples, 60 ms

STATE_OF_PHASE = {1: "CALLING", 2: "RINGING", 3: "CONNECTING", 4: "ACTIVE"}
LIVE_STATES = ("CALLING", "RINGING", "CONNECTING", "ACTIVE")
# how many answered calls answered_here remembers
ANSWERED_KEPT = 50
# How long an outgoing one-to-one call rings before this side gives up. WhatsApp
# does not always end it for the caller (a callee whose phone is off keeps the
# offer queued for minutes and no <terminate> ever arrives), and WhatsApp Web
# ends the call itself, so the window would otherwise ring until hung up.
OUTGOING_RING_LIMIT = 60


class FrameAssembler:
    """Regroups 48 kHz capture into 60 ms frames at the selected PCM rate."""

    def __init__(self, sample_rate=CALL_RATE):
        if sample_rate not in (16000, 48000):
            raise ValueError("unsupported call PCM rate")
        self.sample_rate = sample_rate
        self.frame_bytes = sample_rate * 60 // 1000 * 2
        self._pending = b""
        self._resampler = PCM48To16()

    def push(self, pcm48: bytes) -> list:
        pcm16 = self._resampler.push(pcm48) if self.sample_rate == CALL_RATE else pcm48
        self._pending += pcm16
        frames = []
        while len(self._pending) >= self.frame_bytes:
            frames.append(self._pending[:self.frame_bytes])
            self._pending = self._pending[self.frame_bytes:]
        return frames

    def clear(self):
        self._pending = b""
        self._resampler.clear()


def end_state(reason) -> tuple:
    """meowcaller's end reason -> (event, state) of `callstate`."""
    text = str(reason or "").lower()
    if text.startswith("server:") or text == "offer_failed":
        return "ended", "FAILED"
    if "timeout" in text:
        return "timeout", "NOT_ANSWERED"
    if "reject" in text:
        return "ended", "REJECTED"
    if "accept" in text:            # accepted_elsewhere: answered on another device
        return "ended", "HANDLED_REMOTELY"
    return "ended", "ENDED"


def dial_target(to: str, phone_of_lid=None) -> str:
    """What CallDial takes: the number's digits, or a LID address when no number
    is known for it. ValueError for what cannot be called (a group, a channel)."""
    jid = msgmap.jid_whatsmeow(str(to or "").strip())
    user, _, server = jid.partition("@")
    user = user.split(":")[0]
    if server == "lid" and phone_of_lid is not None:
        number = phone_of_lid(f"{user}@lid")
        if number:
            return number.split("@")[0].split(":")[0]
        return f"{user}@lid"
    if server in ("s.whatsapp.net", "") and user.isdigit():
        return user
    raise ValueError("only a person can be called")


def layer_error():
    """What neonize.calls raises once a client has no call layer (stopped)."""
    try:
        from neonize.calls import CallError
    except ImportError:             # a build without the layer never gets this far
        return LookupError
    return CallError


def state_event(call, *, state, event="state", now=None) -> dict:
    """The `data` of `callstate` for one of our calls (`call`: a dict with
    id, peer, outgoing, and video and group when it has them)."""
    group = call.get("group") or ""
    return {"id": call["id"], "peerJid": call["peer"], "state": state, "event": event,
            "outgoing": call["outgoing"], "isVideo": bool(call.get("video")),
            "isGroup": bool(group), "groupJid": group,
            "observedAt": int(now if now is not None else time.time())}


def group_target(group_jid) -> str:
    """The group's address as CallGroupDial takes it; ValueError when it is
    not a group."""
    jid = msgmap.jid_whatsmeow(str(group_jid or "").strip())
    user, _, server = jid.partition("@")
    if server != "g.us" or not user.replace("-", "").isdigit():
        raise ValueError("groupJid is not a group")
    return jid


def _serialized(method):
    """Fence lifecycle, PCM writes and emissions across native blocking reads.

    The model mutex stays short-lived; this reentrant gate also orders callbacks
    outside it. Native event callbacks enqueue JSON, never wait for this gate.
    """
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        with self._lifecycle:
            return method(self, *args, **kwargs)
    return run


class CallMedia:
    """The calls of one session. `calls` is a neonize.calls.Calls (or a fake);
    `emit(event, data)` sends to the session's clients."""

    def __init__(self, name, calls, emit, phone_of_lid=None, video=None, group_calls=False):
        self.name, self.calls, self.emit = name, calls, emit
        self.phone_of_lid = phone_of_lid
        self.video = video                # call_video.CallVideo, or None
        self.group_calls = group_calls
        self._lock = threading.Lock()
        self._lifecycle = threading.RLock()
        self._calls = {}              # id -> {"id", "peer", "outgoing", "state"}
        # id -> {"at": when this client answered, "video": whether it had
        # video}: outlives the call, so its call_log says it was answered
        # (calls.record_end) whatever order the end reaches the two layers
        self._answered = {}
        self._mic = FrameAssembler()
        self._mic_lock = threading.Lock()
        self._mic_owner = None
        self._running = False
        self._audio_readers = {}       # call id -> (unique claim, call record)
        self.ring_limit = OUTGOING_RING_LIMIT
        self._ring_timers = {}         # call id -> the timer that ends an unanswered dial

    # ---------------------------------------------------------------- state
    def _known(self, call_id):
        with self._lock:
            return dict(self._calls[call_id]) if call_id in self._calls else None

    @_serialized
    def _set_state(self, call_id, state, event="state", expected=None):
        """Records the call's state and tells the client, once per change."""
        with self._lock:
            call = self._calls.get(call_id)
            if call is None or (expected is not None and call is not expected) or call["state"] == state:
                return False
            call["state"] = state
            data = state_event(call, state=state, event=event)
        self.emit("callstate", {"session": self.name, "data": data})
        return True

    @_serialized
    def remember_offer(self, data):
        """calls.py hands over each incoming offer (whatsmeow's view of it: the
        peer by number), so this call's later states carry the same peer."""
        with self._lock:
            self._calls.setdefault(data["id"], {
                "id": data["id"], "peer": data.get("peerJid", ""), "outgoing": False,
                "state": "INCOMING_RING", "video": bool(data.get("isVideo")),
                "group": data.get("groupJid") or ""})

    def live_call(self):
        """The id of the call whose audio flows (the newest live one)."""
        with self._lock:
            live = [c for c in self._calls.values() if c["state"] in LIVE_STATES]
        return live[-1]["id"] if live else ""

    # ---------------------------------------------------------------- events
    @_serialized
    def handle(self, ev):
        """One event of the call layer."""
        kind, call_id = ev.get("ev"), str(ev.get("call") or "")
        if not call_id:
            return
        generation = ev.get("generation")
        if generation is not None and kind != "incoming":
            with self._lock:
                current = self._calls.get(call_id)
            if current is not None:
                expected = current.get("native_generation")
                if expected is None:
                    self._format_for(current)
                    expected = current.get("native_generation")
                if expected is not None and generation != expected:
                    return
        if kind == "incoming":
            with self._lock:
                previous = self._calls.get(call_id)
                if generation is not None and previous is not None and previous.get("native_generation") not in (None, generation):
                    self._calls.pop(call_id, None)
                    self._audio_readers.pop(call_id, None)
                call = self._calls.setdefault(call_id, {
                    "id": call_id, "peer": msgmap.jid_wpp(str(ev.get("peer") or "")),
                    "outgoing": False, "state": "INCOMING_RING", "group": ""})
                call["video"] = bool(call.get("video") or ev.get("video"))
                if generation is not None:
                    call["native_generation"] = generation
            return
        if kind == "video":
            self._peer_video(call_id, ev)
            return
        if kind == "state":
            state = STATE_OF_PHASE.get(int(ev.get("phase") or 0))
            call = self._known(call_id)
            # an outgoing call is only ACTIVE once the other person answered
            # ("accepted"); meowcaller's Active can come with the relay alone
            if state and not (call and (
                    (state == "ACTIVE" and not call.get("connected"))
                    or (call.get("connected") and state in ("CALLING", "RINGING", "CONNECTING")))):
                self._set_state(call_id, state)
            return
        if kind in ("accepted", "ready"):
            with self._lock:
                call = self._calls.get(call_id)
                if call is None:
                    return
                if kind == "accepted" and call["outgoing"]:
                    call["connected"] = True
                if not call.get("connected"):
                    return             # relay media is not the user's answer
            self._set_state(call_id, "ACTIVE", expected=call)
            self._ensure_audio(call_id, call)
            return
        if kind == "end":
            event, state = end_state(ev.get("reason"))
            self._stop_ring_timer(call_id)
            with self._lock:
                call = self._calls.pop(call_id, None)
                if call and call.get("unanswered"):
                    event, state = "timeout", "NOT_ANSWERED"
                self._audio_readers.pop(call_id, None)
                data = state_event(call, state=state, event=event) if call else None
            if self.video is not None:
                self.video.end(call_id)
            with self._mic_lock:
                if self._mic_owner is call:
                    self._mic.clear()
                    self._mic_owner = None
            if data:
                self.emit("callstate", {"session": self.name, "data": data})

    def start_pump(self, keep_going):
        """Starts pump_events on its own thread, unless it is already going (a
        reconnection starts no second one). True if it started one."""
        with self._lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self.pump_events, args=(keep_going,), daemon=True,
                         name="call-events").start()
        return True

    def pump_events(self, keep_going):
        """The call layer's events, while `keep_going()` (start_pump runs it)."""
        stopped = layer_error()
        try:
            while keep_going():
                try:
                    ev = self.calls.next_event(500)
                except stopped:
                    return                  # the client stopped
                if ev:
                    try:
                        self.handle(ev)
                    except Exception:
                        log.exception("call event")
        finally:
            self._running = False

    # ---------------------------------------------------------------- audio
    def _format_for(self, call):
        """Bind one immutable native format to this bridge record."""
        with self._lock:
            if self._calls.get(call["id"]) is not call:
                return None
            cached = call.get("pcm_format")
        if cached is not None:
            return cached
        getter = getattr(self.calls, "audio_format", None)
        value = getter(call["id"]) if callable(getter) else {"sampleRate": CALL_RATE, "generation": 0}
        if value is None:
            return None
        if (not isinstance(value, dict) or type(value.get("sampleRate")) is not int
                or value["sampleRate"] not in (16000, 48000)
                or type(value.get("generation")) is not int or not 0 <= value["generation"] < 2**64):
            raise ValueError("invalid native call PCM format")
        result = {"sampleRate": value["sampleRate"], "generation": value["generation"]}
        with self._lock:
            if self._calls.get(call["id"]) is not call:
                return None
            expected = call.get("native_generation")
            if expected is not None and expected != result["generation"]:
                return None
            call["pcm_format"] = result
            if result["generation"]:
                call["native_generation"] = result["generation"]
        return result

    @_serialized
    def _ensure_audio(self, call_id, expected=None):
        """Claim once independently of state changes; answer/ready may race."""
        with self._lock:
            call = self._calls.get(call_id)
            if (call is None or (expected is not None and call is not expected)
                    or call["state"] != "ACTIVE" or call_id in self._audio_readers):
                return
            claim = (object(), call)
            self._audio_readers[call_id] = claim
        try:
            self._start_audio(call_id, claim)
        except Exception:
            with self._lock:
                if self._audio_readers.get(call_id) is claim:
                    self._audio_readers.pop(call_id, None)
            raise

    def _start_audio(self, call_id, claim):
        threading.Thread(target=self._pump_audio, args=(call_id, claim), daemon=True,
                         name="call-audio").start()
        if self.video is not None:
            try:
                self.video.start_receiving(call_id)
            except Exception:
                # The audio thread already owns the queue; a camera failure
                # must not release its claim and create a second reader.
                log.exception("call video receive startup")

    def _pump_audio(self, call_id, claim=None):
        """The other side's frames, to the client, until the call ends."""
        with self._lock:
            claim = claim or self._audio_readers.get(call_id)
        try:
            while self._owns_audio(call_id, claim):
                reader = getattr(self.calls, "read_audio_frame", None)
                packet = reader(call_id, 200) if callable(reader) else None
                if callable(reader):
                    if packet is None:
                        return
                    frame = packet["pcm"]
                else:
                    frame = self.calls.read_audio(call_id, 200)
                if frame is None:
                    return
                # Native reads may return after end/rejoin of the same id.
                if frame:
                    if callable(reader):
                        self._emit_audio(call_id, claim, frame, packet["sampleRate"], packet["generation"])
                    else:
                        self._emit_audio(call_id, claim, frame)
        except (ValueError, layer_error()):
            log.warning("native call audio format rejected; reader released")
        finally:
            with self._lock:
                if self._audio_readers.get(call_id) is claim:
                    self._audio_readers.pop(call_id, None)

    def _owns_audio(self, call_id, claim):
        with self._lock:
            return (claim is not None and self._audio_readers.get(call_id) is claim
                    and self._calls.get(call_id) is claim[1])

    @_serialized
    def _emit_audio(self, call_id, claim, frame, sample_rate=CALL_RATE, generation=0):
        if self._owns_audio(call_id, claim):
            pcm_format = self._format_for(claim[1])
            if (pcm_format is None or sample_rate != pcm_format["sampleRate"]
                    or generation != pcm_format["generation"] or len(frame) % 2):
                return
            self.emit("call:audio:remote", {
                "session": self.name, "encoding": "base64", "sampleRate": sample_rate,
                "pcm": base64.b64encode(frame).decode("ascii")})

    def _connected_call(self):
        with self._lock:
            active = [c["id"] for c in self._calls.values() if c["state"] == "ACTIVE"]
        return active[-1] if active else ""

    @_serialized
    def microphone(self, data):
        """A `call:audio:mic` from the client, to the connected call. The client
        opens its microphone when it dials; what it says before the answer is
        dropped, or meowcaller's 3 s queue would send it late."""
        with self._lock:
            active = [c for c in self._calls.values() if c["state"] == "ACTIVE" and c.get("connected")]
            call = active[-1] if active else None
        if call is None or not isinstance(data, dict):
            return
        call_id = call["id"]
        encoded = data.get("pcm") or ""
        if not isinstance(encoded, (str, bytes)) or len(encoded) > 12800:
            return                      # at most 100 ms, before allocating decoded PCM
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return
        try:
            source_rate = int(data.get("sampleRate") or CLIENT_RATE)
        except (TypeError, ValueError, OverflowError):
            return
        if source_rate != CLIENT_RATE or len(pcm) > CLIENT_RATE // 5 or len(pcm) % 2:
            return                      # the client always sends 48 kHz
        with self._lock:
            if self._calls.get(call_id) is not call or call["state"] != "ACTIVE" or not call.get("connected"):
                return
        with self._mic_lock:
            pcm_format = self._format_for(call)
            if pcm_format is None:
                return
            if self._mic_owner is not call:
                self._mic = FrameAssembler(pcm_format["sampleRate"])
                self._mic_owner = call
            if call is not None:
                for frame in self._mic.push(pcm):
                    writer = getattr(self.calls, "write_audio_frame", None)
                    if callable(writer):
                        written = writer(call_id, frame, pcm_format["sampleRate"], pcm_format["generation"])
                    else:
                        written = self.calls.write_audio(call_id, frame)
                    if not written:
                        self._mic.clear()
                        break

    @_serialized
    def stop_microphone(self):
        with self._mic_lock:
            self._mic.clear()
            self._mic_owner = None

    # ---------------------------------------------------------------- video
    @_serialized
    def _mark_video(self, call_id):
        """The call became a video call: the client is told once."""
        with self._lock:
            call = self._calls.get(call_id)
            if call is None or call.get("video"):
                return False
            call["video"] = True
            if call_id in self._answered:
                self._answered[call_id]["video"] = True
            data = state_event(call, state=call["state"] or "CALLING")
        self.emit("callstate", {"session": self.name, "data": data})
        return True

    def _peer_video(self, call_id, ev):
        """A <video> of the other side: an upgrade is accepted at once (the
        camera here stays off), and an active camera makes it a video call."""
        if ev.get("upgrade"):
            if self.video is None or not self.calls.video(call_id, VIDEO_ACCEPT):
                log.warning("call video: the other side's upgrade was not accepted")
                return
        if ev.get("upgrade") or ev.get("active"):
            self._mark_video(call_id)

    def upgrade_video(self, call_id=""):
        """Asks the other side to turn the connected call into a video call.
        Returns (ok, already_video); LookupError when no call is connected."""
        call_id = call_id or self._connected_call()
        call = self._known(call_id) if call_id else None
        if call is None or call["state"] != "ACTIVE":
            raise LookupError("there is no ongoing call to upgrade to video")
        if call.get("video"):
            return True, True
        if self.video is None or not self.calls.video(call_id, VIDEO_REQUEST):
            return False, False
        self._mark_video(call_id)
        return True, False

    def camera_frame(self, data):
        if self.video is not None:
            self.video.camera_frame(self._connected_call(), data)

    def camera_stop(self, data=None):
        if self.video is not None:
            native = isinstance(data, dict) and data.get("native") is True
            self.video.camera_stop(self._connected_call(), native)

    def camera_start(self):
        if self.video is not None:
            self.video.camera_start(self._connected_call())

    # ---------------------------------------------------------------- actions
    def dial(self, to, video=False):
        """Places a call; returns its id (CallError / ValueError on failure)."""
        if video and self.video is None:
            raise ValueError("video calls are not available here")
        target = dial_target(to, self.phone_of_lid)
        result = self.calls.dial(target, video=True) if video else self.calls.dial(target)
        peer = msgmap.jid_wpp(msgmap.jid_whatsmeow(to))
        call_id = self._placed(str(result["call"]), peer, video, "")
        self._arm_ring_timer(call_id)
        return call_id

    def group_dial(self, group_jid, video=False):
        """Calls every member of the group; returns the call's id."""
        if not self.group_calls or (video and self.video is None):
            raise ValueError("group calls are not available here")
        target = group_target(group_jid)
        result = self.calls.group_dial(target, video=video)
        group = msgmap.jid_wpp(target)
        call_id = self._placed(str(result["call"]), group, video, group)
        # left later, the call can still be found and joined again
        creator = getattr(self, "own_creator", None)
        ongoing_calls.known(self).remember(call_id, creator() if creator else "", group, video)
        return call_id

    def _arm_ring_timer(self, call_id):
        """Ends the dialed call if nobody has answered after `ring_limit`."""
        with self._lock:
            expected = self._calls.get(call_id)
            if expected is None or not self.ring_limit:
                return
            timer = threading.Timer(self.ring_limit, self._ring_expired, (call_id, expected))
            timer.daemon = True
            self._ring_timers[call_id] = timer
        timer.start()

    def _stop_ring_timer(self, call_id):
        with self._lock:
            timer = self._ring_timers.pop(call_id, None)
        if timer is not None:
            timer.cancel()

    @_serialized
    def _ring_expired(self, call_id, expected):
        """Nobody answered: hangs up the call and tells the client nobody did.
        A call that was answered, or ended meanwhile, is left alone."""
        with self._lock:
            self._ring_timers.pop(call_id, None)
            call = self._calls.get(call_id)
            if call is None or call is not expected or call.get("connected"):
                return
            call["unanswered"] = True
        log.info("outgoing call not answered after %ss; ending it", self.ring_limit)
        try:
            hung = self.calls.hangup(call_id)
        except Exception:
            log.exception("hang up an unanswered call")
            hung = False
        if hung:
            return            # the call layer's end event tells the client
        self.handle({"ev": "end", "call": call_id, "reason": "timeout"})

    @_serialized
    def _placed(self, call_id, peer, video, group):
        with self._lock:
            self._calls[call_id] = {"id": call_id, "peer": peer, "outgoing": True, "state": "",
                                    "video": bool(video), "group": group}
        self._set_state(call_id, "CALLING")
        return call_id

    @_serialized
    def answer(self, call_id):
        call_id = call_id or self._ringing()
        with self._lock:
            expected = self._calls.get(call_id)
        if expected is None or not self.calls.answer(call_id):
            return False
        with self._lock:
            call = self._calls.get(call_id)
            if call is not expected:
                return False
            call["connected"] = True
        self._note_answered(call_id)
        self._set_state(call_id, "ACTIVE", expected=expected)
        self._ensure_audio(call_id, expected)
        return True

    # ---------------------------------------------------------- ongoing calls
    def ongoing_available(self):
        """Whether this build finds and joins group calls already going on."""
        if not callable(getattr(self.calls, "lobby", None)):
            return False
        built = getattr(sys.modules.get(type(self.calls).__module__), "ongoing_available", None)
        return built() if callable(built) else True

    def lobby(self, call_id, creator):
        return self.calls.lobby(call_id, creator)

    def roster(self, call_id):
        return self.calls.roster(call_id)

    def in_call(self, call_id):
        call = self._known(call_id)
        return bool(call) and call["state"] in LIVE_STATES

    @_serialized
    def join_ongoing(self, call_id, creator, chat, video=False):
        """Joins a group call already going on (ongoing_calls.py); its id.
        Like an answer: the call counts as answered here and its audio starts
        at once, flowing when the relay's media arrives."""
        if self.in_call(call_id):
            return call_id
        joined = str(self.calls.join_ongoing(call_id, creator, video=video)["call"])
        with self._lock:
            self._calls[joined] = {"id": joined, "peer": chat, "outgoing": False, "state": "",
                                   "video": bool(video), "connected": True,
                                   "group": chat if chat.endswith("@g.us") else ""}
        self._note_answered(joined)
        self._set_state(joined, "ACTIVE")
        self._ensure_audio(joined)
        return joined

    def answered_here(self, call_id):
        """{"at", "video"} of a call this client answered, or None."""
        with self._lock:
            found = self._answered.get(call_id)
            return dict(found) if found else None

    def _note_answered(self, call_id):
        with self._lock:
            call = self._calls.get(call_id) or {}
            self._answered[call_id] = {"at": time.time(), "video": bool(call.get("video"))}
            while len(self._answered) > ANSWERED_KEPT:
                self._answered.pop(next(iter(self._answered)))

    def _ringing(self):
        with self._lock:
            ringing = [c["id"] for c in self._calls.values() if c["state"] == "INCOMING_RING"]
        return ringing[-1] if ringing else ""

    def hangup(self, call_id=""):
        """Ends (or rejects while ringing) the call; with no id, the live one."""
        call_id = call_id or self.live_call() or self._ringing()
        return bool(call_id) and self.calls.hangup(call_id)

    def status(self, call_id):
        call = self._known(call_id)
        state = call["state"] if call else "ENDED"
        return {"id": call_id, "state": state, "live": state in LIVE_STATES,
                "isVideo": bool(call and call.get("video")), "engine": "meowcaller"}

    def knows(self, call_id):
        return self._known(call_id) is not None
