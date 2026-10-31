"""Voice calls through the call layer: the audio conversion, the states the
client reads and the call/* routes, against a fake neonize.calls.Calls; the
real bindings only where they need no network."""
import array
import asyncio
import base64
import threading
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import call_media as cm
import calls


def pcm(*samples):
    return array.array("h", samples).tobytes()


# ------------------------------------------------------------------ pure
def test_the_assembler_makes_60_ms_frames_at_16_khz_from_20_ms_at_48_khz():
    assembler = cm.FrameAssembler()
    twenty_ms = pcm(*([300] * 960))                 # what the client sends
    assert assembler.push(twenty_ms) == []
    assert assembler.push(twenty_ms) == []
    frames = assembler.push(twenty_ms)
    assert len(frames) == 1 and len(frames[0]) == cm.FRAME_BYTES
    # The causal anti-alias FIR has 1 ms of initial history, not an invented
    # full-level prefix. Constant speech level is unchanged after that delay.
    assert set(array.array("h", frames[0])[32:]) == {300}
    assembler.push(twenty_ms)
    assembler.clear()
    assert assembler.push(twenty_ms) == []


@pytest.mark.parametrize("reason, expected", [
    ("rejected", ("ended", "REJECTED")), ("timeout", ("timeout", "NOT_ANSWERED")),
    ("accepted_elsewhere", ("ended", "HANDLED_REMOTELY")), ("hangup", ("ended", "ENDED")),
    ("", ("ended", "ENDED")), ("server:503", ("ended", "FAILED")),
    ("offer_failed", ("ended", "FAILED")),
])
def test_end_reasons_become_the_clients_terminal_states(reason, expected):
    assert cm.end_state(reason) == expected


def test_dial_target():
    assert cm.dial_target("5511999990000@c.us") == "5511999990000"
    assert cm.dial_target("5511999990000:3@s.whatsapp.net") == "5511999990000"
    assert cm.dial_target("123@lid", lambda lid: "5511999990000@s.whatsapp.net") == "5511999990000"
    assert cm.dial_target("123@lid", lambda lid: "") == "123@lid"
    for unreachable in ("120363@g.us", "x@newsletter", "", "abc@c.us"):
        with pytest.raises(ValueError):
            cm.dial_target(unreachable)


# ------------------------------------------------------------- CallMedia
class FakeCalls:
    def __init__(self, remote=()):
        self.dialed, self.answered, self.hung, self.written = [], [], [], []
        self.remote = list(remote)          # frames the other side "sends"
        self.events = []
        self.enabled_flag = True

    def enabled(self):
        return self.enabled_flag

    def dial(self, target):
        self.dialed.append(target)
        return {"call": "OUT1", "peer": "123@lid"}

    def answer(self, call_id):
        self.answered.append(call_id)
        return True

    def hangup(self, call_id):
        self.hung.append(call_id)
        return True

    def read_audio(self, call_id, timeout_ms):
        return self.remote.pop(0) if self.remote else None

    def write_audio(self, call_id, frame):
        self.written.append((call_id, frame))
        return True

    def next_event(self, timeout_ms):
        if self.events:
            return self.events.pop(0)
        raise cm.layer_error()("stopped")


def media(fake=None):
    sent = []
    m = cm.CallMedia("s1", fake or FakeCalls(), lambda event, data: sent.append((event, data)))
    m._start_audio = lambda call_id, claim: m._pump_audio(call_id, claim)    # no thread in a test
    return m, sent


def states(sent):
    return [d["data"]["state"] for e, d in sent if e == "callstate"]


def test_an_outgoing_call_is_active_only_once_the_other_person_answers():
    fake = FakeCalls(remote=[b"\x01\x00" * 960, b"", None])
    m, sent = media(fake)
    assert m.dial("5511999990000@c.us") == "OUT1" and fake.dialed == ["5511999990000"]
    m.handle({"ev": "state", "call": "OUT1", "phase": 2})
    m.handle({"ev": "state", "call": "OUT1", "phase": 4})     # the relay, not the answer
    assert states(sent) == ["CALLING", "RINGING"]
    m.handle({"ev": "accepted", "call": "OUT1"})
    assert states(sent) == ["CALLING", "RINGING", "ACTIVE"]
    first = [d for e, d in sent if e == "callstate"][0]["data"]
    assert first["outgoing"] is True and first["peerJid"] == "5511999990000@c.us"
    audio = [d for e, d in sent if e == "call:audio:remote"]
    assert len(audio) == 1 and audio[0]["sampleRate"] == 16000
    assert base64.b64decode(audio[0]["pcm"]) == b"\x01\x00" * 960
    m.handle({"ev": "end", "call": "OUT1", "reason": "hangup"})
    end = [d for e, d in sent if e == "callstate"][-1]["data"]
    assert (end["state"], end["event"]) == ("ENDED", "ended")
    assert m.status("OUT1") == {"id": "OUT1", "state": "ENDED", "live": False,
                                "isVideo": False, "engine": "meowcaller"}


def test_the_microphone_reaches_only_a_connected_call():
    fake = FakeCalls(remote=[None])
    m, _ = media(fake)
    sixty_ms = {"pcm": base64.b64encode(pcm(*([5] * 2880))).decode(), "sampleRate": 48000}
    m.dial("5511999990000@c.us")
    m.microphone(sixty_ms)                       # still ringing: dropped
    assert fake.written == []
    m.handle({"ev": "accepted", "call": "OUT1"})
    m.microphone(sixty_ms)
    assert len(fake.written) == 1 and fake.written[0][0] == "OUT1"
    assert len(fake.written[0][1]) == cm.FRAME_BYTES
    m.microphone({"pcm": "not base64!", "sampleRate": 48000})
    m.microphone({"pcm": sixty_ms["pcm"], "sampleRate": 16000})     # not what the client sends
    assert len(fake.written) == 1


def test_an_incoming_call_answered_here_and_hung_up():
    fake = FakeCalls(remote=[None])
    m, sent = media(fake)
    m.remember_offer({"id": "IN1", "peerJid": "5511999990000@c.us"})
    m.handle({"ev": "incoming", "call": "IN1", "peer": "123@lid"})    # the offer's peer stays
    assert m.answer("") and fake.answered == ["IN1"]
    active = [d for e, d in sent if e == "callstate"][-1]["data"]
    assert (active["state"], active["peerJid"], active["outgoing"]) == (
        "ACTIVE", "5511999990000@c.us", False)
    assert m.status("IN1")["live"] is True
    assert m.hangup("") and fake.hung == ["IN1"]


@pytest.mark.parametrize("events", [(4,), (4, "ready"), (3, 4, "ready")])
def test_native_media_events_before_answer_returns_cannot_lose_audio_reader(events):
    fake = FakeCalls()
    m, _ = media(fake)
    started = []
    m._start_audio = lambda cid, claim: started.append(cid)
    m.remember_offer({"id": "IN-RACE", "peerJid": "1@c.us"})

    def answer(call_id):
        for event in events:
            m.handle({"ev": "ready" if event == "ready" else "state",
                      "call": call_id, "phase": event if event != "ready" else 0})
        return True

    fake.answer = answer
    assert m.answer("IN-RACE")
    for event in ("ready", "accepted", "ready"):
        m.handle({"ev": event, "call": "IN-RACE"})
    assert m.status("IN-RACE")["state"] == "ACTIVE"
    assert started == ["IN-RACE"]


def test_relay_ready_does_not_authorize_microphone_before_peer_or_local_answer():
    m, _ = media()
    started = []
    m._start_audio = lambda cid, claim: started.append(cid)
    m.remember_offer({"id": "IN", "peerJid": "1@c.us"})
    m.handle({"ev": "state", "call": "IN", "phase": 4})
    m.handle({"ev": "ready", "call": "IN"})
    assert m.status("IN")["state"] == "INCOMING_RING"
    m._placed("OUT", "2@c.us", False, "")
    m.handle({"ev": "ready", "call": "OUT"})
    assert m.status("OUT")["state"] == "CALLING" and started == []
    m.handle({"ev": "accepted", "call": "OUT"})
    assert started == ["OUT"]


def test_reader_start_failure_releases_claim_for_readiness_retry():
    m, _ = media()
    m.remember_offer({"id": "IN", "peerJid": "1@c.us"})
    def broken(_, claim):
        raise RuntimeError("synthetic thread startup failure")
    m._start_audio = broken
    with pytest.raises(RuntimeError):
        m.answer("IN")
    starts = []
    m._start_audio = lambda cid, claim: starts.append(cid)
    m.handle({"ev": "ready", "call": "IN"})
    assert starts == ["IN"]


def test_pcm_returning_after_end_and_same_id_rejoin_cannot_reach_new_call():
    fake = FakeCalls()
    m, sent = media(fake)
    m._start_audio = lambda _, claim: None
    m.remember_offer({"id": "IN", "peerJid": "1@c.us"})
    m.answer("IN")
    old_claim = m._audio_readers["IN"]

    def read(call_id, timeout):
        m.handle({"ev": "end", "call": call_id})
        m.remember_offer({"id": call_id, "peerJid": "2@c.us"})
        m.answer(call_id)
        return pcm(*([123] * 960))

    fake.read_audio = read
    m._pump_audio("IN", old_claim)
    assert not any(event == "call:audio:remote" for event, _ in sent)
    assert m._audio_readers["IN"] is not old_claim


def test_microphone_filter_and_partial_frame_do_not_cross_call_owners():
    fake = FakeCalls()
    m, _ = media(fake)
    m._start_audio = lambda _, claim: None
    m._placed("OUT1", "1@c.us", False, "")
    m.handle({"ev": "accepted", "call": "OUT1"})
    m.microphone({"pcm": base64.b64encode(pcm(*([20000] * 960))).decode()})
    m.handle({"ev": "end", "call": "OUT1"})
    m._placed("OUT2", "2@c.us", False, "")
    m.handle({"ev": "accepted", "call": "OUT2"})
    silence = {"pcm": base64.b64encode(pcm(*([0] * 960))).decode()}
    for _ in range(3):
        m.microphone(silence)
    assert fake.written == [("OUT2", b"\0" * 1920)]


def test_answer_returning_after_end_cannot_answer_a_new_offer_with_same_id():
    fake = FakeCalls()
    m, sent = media(fake)
    m.remember_offer({"id": "IN", "peerJid": "1@c.us"})
    def answer(cid):
        m.handle({"ev": "end", "call": cid})
        m.remember_offer({"id": cid, "peerJid": "2@c.us"})
        return True
    fake.answer = answer
    assert m.answer("IN") is False
    assert m.status("IN")["state"] == "INCOMING_RING"
    assert "IN" not in m._audio_readers and m.answered_here("IN") is None


def test_terminal_emit_can_rejoin_same_id_without_removing_replacement():
    m, sent = media()
    m._start_audio = lambda cid, claim: None
    m.remember_offer({"id": "IN", "peerJid": "1@c.us"})
    m.answer("IN")
    original_emit = m.emit
    def emit(event, data):
        original_emit(event, data)
        if event == "callstate" and data["data"]["state"] == "ENDED":
            m.join_ongoing("IN", "2@lid", "1203@g.us")
    m.calls.join_ongoing = lambda *a, **kw: {"call": "IN"}
    m.emit = emit
    m.handle({"ev": "end", "call": "IN"})
    assert m.status("IN")["state"] == "ACTIVE" and "IN" in m._audio_readers


def test_replacement_after_answer_identity_validation_is_never_activated(monkeypatch):
    m, _ = media()
    m.remember_offer({"id": "IN", "peerJid": "1@c.us"})
    original = m._note_answered
    def note(cid):
        original(cid)
        m.handle({"ev": "end", "call": cid})
        m.remember_offer({"id": cid, "peerJid": "2@c.us"})
    monkeypatch.setattr(m, "_note_answered", note)
    m.answer("IN")
    assert m.status("IN")["state"] == "INCOMING_RING" and not m._audio_readers


def test_microphone_decode_cannot_retarget_old_pcm_to_replacement(monkeypatch):
    fake = FakeCalls()
    m, _ = media(fake)
    m._start_audio = lambda cid, claim: None
    m._placed("IN", "1@c.us", False, "")
    m.handle({"ev": "accepted", "call": "IN"})
    original = base64.b64decode
    def decode(*args, **kw):
        m.handle({"ev": "end", "call": "IN"})
        m.remember_offer({"id": "IN", "peerJid": "2@c.us"})
        return original(*args, **kw)
    monkeypatch.setattr(base64, "b64decode", decode)
    m.microphone({"pcm": base64.b64encode(pcm(*([123] * 2880))).decode()})
    assert fake.written == [] and m._mic_owner is None
    assert m._mic._pending == b""


def test_a_ringing_call_rejected_and_unknown_ids():
    m, sent = media()
    m.remember_offer({"id": "IN2", "peerJid": "5511@c.us"})
    assert m.knows("IN2") and not m.knows("nope")
    m.handle({"ev": "end", "call": "IN2", "reason": "rejected"})
    assert states(sent) == ["REJECTED"] and not m.knows("IN2")
    m.handle({"ev": "end", "call": "IN2", "reason": "rejected"})    # once only
    assert states(sent) == ["REJECTED"]
    m.handle({"ev": "state", "call": "", "phase": 4})              # no id: nothing
    assert m.answer("") is False


def test_one_event_pump_per_session(monkeypatch):
    monkeypatch.setattr(cm, "layer_error", lambda: LookupError)
    fake = FakeCalls()
    fake.events = [{"ev": "incoming", "call": "IN3", "peer": "1@lid"}]
    m, _ = media(fake)
    started = []
    # only call_media's view of threading: the pump runs right here
    monkeypatch.setattr(cm, "threading", N(Lock=threading.Lock, Thread=lambda target, args, **kw: N(
        start=lambda: started.append(target(*args)))))
    assert m.start_pump(lambda: True) is True
    assert m.knows("IN3") and not m._running        # the fake stopped: the pump ended
    m._running = True
    assert m.start_pump(lambda: True) is False


# ------------------------------------------------------------- the routes
def run(s, requests):
    async def main():
        app = web.Application()
        calls.register_routes(app.router, session=lambda req: s,
                              ok=lambda **kw: web.json_response(kw))
        async with TestClient(TestServer(app)) as http:
            out = []
            for path, body in requests:
                response = await http.post("/api/tok" + path, json=body)
                out.append((response.status, await response.json()))
            return out
    return asyncio.run(main())


def test_the_routes_with_the_call_layer():
    fake = FakeCalls(remote=[None])
    m, _ = media(fake)
    s = N(client=object(), status="CONNECTED", call_media=m, ringing_calls=calls.Pending())
    (st, offer), (st2, status), (st3, end), (st4, video), (st5, group), (st6, bad) = run(s, [
        ("/call/offer", {"to": "5511999990000@c.us", "isVideo": False}),
        ("/call/status", {"callId": "OUT1"}),
        ("/call/end", {"callId": "OUT1"}),
        ("/call/offer", {"to": "5511999990000@c.us", "isVideo": True}),
        ("/call/group/offer", {"groupJid": "1@g.us"}),
        ("/call/offer", {"to": "120363@g.us"}),
    ])
    assert st == 200 and offer["response"]["id"] == "OUT1"
    assert st2 == 200 and status["response"]["live"] is True
    assert st3 == 200 and fake.hung == ["OUT1"]
    assert (st4, st5, st6) == (501, 501, 400)
    s.status = "CLOSED"
    (st7, _), = run(s, [("/call/accept", {"callId": "X"})])
    assert st7 == 400


def test_reject_goes_through_the_call_layer_when_it_holds_the_call():
    fake = FakeCalls()
    m, _ = media(fake)
    rejected = []
    s = N(client=N(reject_call=lambda who, call_id: rejected.append(call_id)),
          status="CONNECTED", call_media=m, ringing_calls=calls.Pending())
    s.ringing_calls.remember("IN4", "creator", 100)
    s.ringing_calls.remember("IN5", "creator", 100)
    m.remember_offer({"id": "IN4", "peerJid": "5511@c.us"})
    (status, _), = run(s, [("/call/reject", {})])
    assert status == 200 and fake.hung == ["IN4"] and rejected == ["IN5"]


# ------------------------------------------------------------- real bindings
def test_the_real_bindings_answer_without_a_connected_client():
    layer = pytest.importorskip("neonize.calls")
    if not layer.available():
        pytest.skip("neonize without WinZapp's call layer (build_neonize.py builds it)")
    calls_of_nobody = layer.Calls(b"no-such-client")
    assert calls_of_nobody.enabled() is False
    with pytest.raises(layer.CallError):
        calls_of_nobody.next_event(10)
    with pytest.raises(layer.CallError):
        calls_of_nobody.dial("5511999990000")
    assert calls_of_nobody.read_audio("x", 10) is None
    assert calls_of_nobody.write_audio("x", b"\0" * layer.FRAME_BYTES) is False
    with pytest.raises(ValueError):
        calls_of_nobody.write_audio("x", b"\0")


def test_a_call_answered_here_is_remembered_past_its_end_with_its_video():
    m, _sent = media()
    m.remember_offer({"id": "IN1", "peerJid": "5511999990000@c.us"})
    assert m.answered_here("IN1") is None
    assert m.answer("IN1")
    answered = m.answered_here("IN1")
    assert answered["video"] is False and answered["at"] > 0
    m._mark_video("IN1")                                    # upgraded to video mid-call
    m.handle({"ev": "end", "call": "IN1", "reason": ""})
    assert m.knows("IN1") is False
    assert m.answered_here("IN1")["video"] is True


def test_only_the_latest_answered_calls_are_kept():
    m, _sent = media()
    for n in range(cm.ANSWERED_KEPT + 5):
        m.remember_offer({"id": f"C{n}", "peerJid": "1@c.us"})
        m.answer(f"C{n}")
    assert m.answered_here("C0") is None and m.answered_here(f"C{cm.ANSWERED_KEPT + 4}")


class OngoingCalls(FakeCalls):
    """A call layer that also finds and joins group calls going on."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.joined = []

    def lobby(self, call_id, creator):
        return {"call": call_id, "participants": [{"jid": "1@lid", "state": "connected"}]}

    def join_ongoing(self, call_id, creator, video=False):
        self.joined.append((call_id, creator, video))
        return {"call": call_id}

    def roster(self, call_id):
        return {"group": True, "participants": []}


def test_joining_an_ongoing_call_makes_it_live_and_answered_here():
    fake = OngoingCalls(remote=[None])
    m, sent = media(fake)
    assert not media(FakeCalls())[0].ongoing_available() and m.ongoing_available()
    assert m.join_ongoing("G1", "100:27@lid", "1203@g.us") == "G1"
    assert fake.joined == [("G1", "100:27@lid", False)]
    assert states(sent)[-1] == "ACTIVE" and m.in_call("G1")
    assert m.live_call() == "G1" and m.answered_here("G1") is not None
    assert m.status("G1")["live"] is True


def test_a_group_call_placed_here_can_be_found_again():
    import ongoing_calls
    fake = OngoingCalls()
    fake.group_dial = lambda target, video=False: {"call": "GP1"}
    m, _sent = media(fake)
    m.group_calls = True
    m.own_creator = lambda: "100:27@lid"
    assert m.group_dial("1203@g.us") == "GP1"
    found = ongoing_calls.known(m).get("GP1")
    assert (found["creator"], found["chat"]) == ("100:27@lid", "1203@g.us")


# ------------------------------------------------- an outgoing call nobody answers
def test_an_unanswered_dial_is_hung_up_and_reported_as_not_answered():
    fake = FakeCalls()
    m, sent = media(fake)
    m.ring_limit = 0                              # the test fires the timer itself
    m.dial("5511999990000@c.us")
    m.handle({"ev": "state", "call": "OUT1", "phase": 2})
    m._ring_expired("OUT1", m._calls["OUT1"])
    assert fake.hung == ["OUT1"]
    # the call layer answers a hangup with its own end event
    m.handle({"ev": "end", "call": "OUT1", "reason": "hangup"})
    end = [d for e, d in sent if e == "callstate"][-1]["data"]
    assert (end["state"], end["event"]) == ("NOT_ANSWERED", "timeout")
    assert m.status("OUT1")["live"] is False


def test_an_unanswered_dial_ends_even_when_the_call_layer_cannot_hang_up():
    fake = FakeCalls()
    fake.hangup = lambda call_id: False
    m, sent = media(fake)
    m.ring_limit = 0
    m.dial("5511999990000@c.us")
    m._ring_expired("OUT1", m._calls["OUT1"])
    assert states(sent)[-1] == "NOT_ANSWERED" and m.status("OUT1")["live"] is False


def test_the_ring_limit_leaves_an_answered_call_alone():
    fake = FakeCalls(remote=[None])
    m, sent = media(fake)
    m.ring_limit = 0
    m.dial("5511999990000@c.us")
    m.handle({"ev": "accepted", "call": "OUT1"})
    m._ring_expired("OUT1", m._calls["OUT1"])
    assert fake.hung == [] and m.status("OUT1")["state"] == "ACTIVE"


def test_the_ring_limit_does_not_touch_a_later_call_with_the_same_id():
    fake = FakeCalls()
    m, _ = media(fake)
    m.ring_limit = 0
    m.dial("5511999990000@c.us")
    old = m._calls["OUT1"]
    m.handle({"ev": "end", "call": "OUT1", "reason": "hangup"})
    m.dial("5511999990000@c.us")
    m._ring_expired("OUT1", old)
    assert fake.hung == []


def test_dialing_arms_a_timer_that_ends_with_the_call():
    m, _ = media()
    m.dial("5511999990000@c.us")
    assert "OUT1" in m._ring_timers
    m.handle({"ev": "end", "call": "OUT1", "reason": "hangup"})
    assert "OUT1" not in m._ring_timers
