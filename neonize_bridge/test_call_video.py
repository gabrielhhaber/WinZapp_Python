"""Video and group calls: the camera out, the other side's video in, the
upgrade each way, and the routes."""
import base64
from types import SimpleNamespace as N

import pytest

import call_media as cm
import call_video as cv
import calls
from test_call_media import FakeCalls, run

JPEG = b"\xff\xd8camera\xff\xd9"


class VideoCalls(FakeCalls):
    def __init__(self, remote_video=()):
        super().__init__()
        self.remote_video = list(remote_video)
        self.sent_video, self.actions, self.group_dialed = [], [], []
        self.video_dialed = []

    def dial(self, target, video=False):
        if video:
            self.video_dialed.append(target)
        return super().dial(target)

    def group_dial(self, group, video=False):
        self.group_dialed.append((group, video))
        return {"call": "GRP1"}

    def video(self, call_id, action):
        self.actions.append((call_id, action))
        return True

    def write_video(self, call_id, unit, duration_ms=0):
        self.sent_video.append((call_id, unit, duration_ms))
        return True

    def read_video(self, call_id, timeout_ms=200):
        return self.remote_video.pop(0) if self.remote_video else None


class Pipe:
    def __init__(self, out):
        self.out, self.pushed, self.closed = out, [], False

    def push(self, data):
        self.pushed.append(data)
        self.out(data[::-1])          # "encodes" right away

    def close(self):
        self.closed = True


class Codec:
    def __init__(self):
        self.encoders, self.decoders = [], []

    def encoder(self, on_unit):
        self.encoders.append(Pipe(on_unit))
        return self.encoders[-1]

    def decoder(self, on_jpeg):
        self.decoders.append(Pipe(on_jpeg))
        return self.decoders[-1]


def video_media(fake=None):
    fake = fake or VideoCalls()
    sent = []
    emit = lambda event, data: sent.append((event, data))     # noqa: E731
    video = cv.CallVideo("s1", fake, emit, Codec())
    video.start_receiving = lambda call_id: video._pump(call_id, video._claim_receiver(call_id))  # no thread
    m = cm.CallMedia("s1", fake, emit, video=video, group_calls=True)
    m._start_audio = lambda call_id, claim: (m._pump_audio(call_id, claim), video.start_receiving(call_id))
    return m, fake, sent


def callstates(sent):
    return [d["data"] for e, d in sent if e == "callstate"]


@pytest.mark.parametrize("data,expected", [
    ({"jpeg": base64.b64encode(JPEG).decode()}, JPEG),
    ({"jpeg": JPEG}, JPEG),
    ({"jpeg": "not base64!"}, None),
    ({"jpeg": base64.b64encode(b"png").decode()}, None),
    ({"jpeg": b"\xff\xd8" + b"x" * 300_000}, None),
    ("text", None), ({}, None)])
def test_camera_jpeg(data, expected):
    assert cv.camera_jpeg(data) == expected


def test_the_camera_goes_out_as_h264_through_one_encoder_per_call():
    m, fake, sent = video_media()
    m.dial("5511999990000@c.us", video=True)
    assert fake.video_dialed == ["5511999990000"]
    assert callstates(sent)[0]["isVideo"] is True
    m.camera_frame({"jpeg": base64.b64encode(JPEG).decode()})       # still ringing: dropped
    assert m.video.codec.encoders == []
    m.handle({"ev": "accepted", "call": "OUT1"})
    for _ in range(2):
        m.camera_frame({"jpeg": base64.b64encode(JPEG).decode()})
    (encoder,) = m.video.codec.encoders
    assert encoder.pushed == [JPEG, JPEG]
    assert fake.sent_video == [("OUT1", JPEG[::-1], 100)] * 2
    m.camera_stop({"native": True})
    assert encoder.closed and fake.actions == [("OUT1", cv.VIDEO_DISABLE)]
    m.camera_start()
    assert fake.actions[-1] == ("OUT1", cv.VIDEO_ENABLE)
    m.camera_stop({})                      # a teardown: WhatsApp is not told
    assert fake.actions[-1] == ("OUT1", cv.VIDEO_ENABLE)


def test_the_other_sides_video_reaches_the_client_as_jpeg():
    fake = VideoCalls(remote_video=[b"unit1", b"", b"unit2", None])
    fake.remote = [None]
    m, fake, sent = video_media(fake)
    m.remember_offer({"id": "IN1", "peerJid": "5511@c.us", "isVideo": True})
    assert m.answer("IN1")
    (decoder,) = m.video.codec.decoders
    assert decoder.pushed == [b"unit1", b"unit2"]
    shown = [base64.b64decode(d["jpeg"]) for e, d in sent if e == "call:video:remote"]
    assert shown == [b"1tinu", b"2tinu"]
    assert callstates(sent)[-1]["isVideo"] is True
    m.handle({"ev": "end", "call": "IN1", "reason": "hangup"})
    assert decoder.closed


def test_the_other_side_upgrading_is_accepted_and_said_once():
    m, fake, sent = video_media()
    m.remember_offer({"id": "IN2", "peerJid": "5511@c.us"})
    m.answer("IN2")
    assert callstates(sent)[-1]["isVideo"] is False
    m.handle({"ev": "video", "call": "IN2", "upgrade": True, "active": False})
    assert fake.actions == [("IN2", cv.VIDEO_ACCEPT)]
    last = callstates(sent)[-1]
    assert (last["state"], last["isVideo"]) == ("ACTIVE", True)
    m.handle({"ev": "video", "call": "IN2", "active": True})
    assert len(callstates(sent)) == 2 and m.status("IN2")["isVideo"] is True


def test_without_video_an_upgrade_is_not_accepted():
    fake = VideoCalls()
    m = cm.CallMedia("s1", fake, lambda e, d: None)
    m.remember_offer({"id": "IN3", "peerJid": "5511@c.us"})
    m.handle({"ev": "video", "call": "IN3", "upgrade": True})
    assert fake.actions == [] and m.status("IN3")["isVideo"] is False
    with pytest.raises(ValueError):
        m.dial("5511999990000@c.us", video=True)
    with pytest.raises(ValueError):
        m.group_dial("120363-1@g.us")


def test_upgrading_our_call():
    m, fake, sent = video_media()
    with pytest.raises(LookupError):
        m.upgrade_video("")
    m.dial("5511999990000@c.us")
    with pytest.raises(LookupError):
        m.upgrade_video("OUT1")                 # still ringing
    m.handle({"ev": "accepted", "call": "OUT1"})
    assert m.upgrade_video("") == (True, False)
    assert fake.actions == [("OUT1", cv.VIDEO_REQUEST)] and callstates(sent)[-1]["isVideo"]
    assert m.upgrade_video("OUT1") == (True, True)


def test_a_group_call():
    m, fake, sent = video_media()
    assert m.group_dial("120363-1@g.us", video=True) == "GRP1"
    assert fake.group_dialed == [("120363-1@g.us", True)]
    first = callstates(sent)[0]
    assert (first["isGroup"], first["groupJid"], first["isVideo"]) == (True, "120363-1@g.us", True)
    for bad in ("5511@c.us", "", "abc@g.us"):
        with pytest.raises(ValueError):
            cm.group_target(bad)


def test_the_routes_with_video_and_groups():
    m, fake, _ = video_media()
    s = N(client=object(), status="CONNECTED", call_media=m, ringing_calls=calls.Pending())
    out = run(s, [
        ("/call/upgrade-video", {}),
        ("/call/offer", {"to": "5511999990000@c.us", "isVideo": True}),
        ("/call/group/offer", {"groupJid": "120363-1@g.us", "isVideo": False}),
        ("/call/group/offer", {}),
        ("/call/group/offer", {"groupJid": "5511@c.us"}),
    ])
    assert [status for status, _ in out] == [409, 200, 200, 400, 400]
    assert out[2][1]["response"]["id"] == "GRP1"
    m.handle({"ev": "accepted", "call": "OUT1"})
    (status, body), = run(s, [("/call/upgrade-video", {"callId": "OUT1"})])
    assert status == 200 and body["response"]["call"]["isVideo"] is True
    assert calls.capabilities(m) == {"video": True, "groupCalls": True}
    assert calls.capabilities(None) == {"video": False, "groupCalls": False}


def test_the_real_video_bindings_answer_without_a_connected_client():
    layer = pytest.importorskip("neonize.calls")
    if not layer.available() or not layer.video_available():
        pytest.skip("neonize without WinZapp's video calls (build_neonize.py builds them)")
    nobody = layer.Calls(b"no-such-client")
    assert nobody.video("x", layer.VIDEO_REQUEST) is False
    assert nobody.write_video("x", b"\0\0\0\1\x65") is False
    assert nobody.read_video("x", 10) is None
    assert nobody.info("x") is None
    with pytest.raises(layer.CallError):
        nobody.group_dial("120363-1@g.us")
    with pytest.raises(layer.CallError):
        nobody.dial("5511999990000", video=True)


def test_a_session_gets_video_only_with_the_dll_and_an_ffmpeg(monkeypatch):
    layer = pytest.importorskip("neonize.calls")
    if not layer.available():
        pytest.skip("neonize without WinZapp's call layer")
    import server
    import video_codec
    monkeypatch.setattr(layer, "video_available", lambda: True)
    monkeypatch.setattr(video_codec, "find_ffmpeg", lambda: "ffmpeg.exe")
    media = server.new_call_media("s1", b"uuid", lambda e, d: None, None)
    assert media.video is not None and media.group_calls is True
    assert media.video.codec.ffmpeg == "ffmpeg.exe"
    monkeypatch.setattr(video_codec, "find_ffmpeg", lambda: None)
    media = server.new_call_media("s1", b"uuid", lambda e, d: None, None)
    assert media.video is None and media.group_calls is True
    monkeypatch.setattr(layer, "video_available", lambda: False)
    media = server.new_call_media("s1", b"uuid", lambda e, d: None, None)
    assert media.video is None and media.group_calls is False
