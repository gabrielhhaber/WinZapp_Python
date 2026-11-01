"""Audio messages without ffprobe: the message built, and send-voice-base64 for
real (create_app) against a fake client with no build_audio_message. Runs no
ffmpeg."""
import base64
import struct
from types import SimpleNamespace as N

import pytest
from aiohttp.test_utils import TestClient, TestServer

import audio_message as am
import authentication as auth
import media_probe
import server
from test_media_probe import opus

UPLOAD = N(url="https://mmg/x", DirectPath="/v/x", MediaKey=b"k", FileEncSHA256=b"e",
           FileSHA256=b"s", FileLength=5)


class Client:
    """Only upload and send_message: neonize's build_audio_message (and its
    ffprobe) is not there to be called."""

    def __init__(self):
        self.uploads, self.sent, self.upload_types = [], [], []

    def upload(self, binary, media_type=None):
        self.uploads.append(binary)
        self.upload_types.append(media_type)
        return UPLOAD

    def send_message(self, destination, message):
        self.sent.append((destination, message))
        return N(ID="NEW1", Timestamp=1_700_000_000, Message=message)


@pytest.mark.parametrize("detected", ["audio/x-m4a", "audio/m4a", "audio/mp4"])
def test_a_stereo_voice_note_is_labelled_audio_mp4(detected):
    """What libmagic says of an M4A is not what WhatsApp expects (issue #664)."""
    m = am.build(Client(), b"abcde", True, mimetype_of=lambda d: detected,
                 seconds_of=lambda d: 3, waveform_of=lambda d: b"")
    assert m.audioMessage.mimetype == "audio/mp4"


def test_build_a_voice_note_from_bytes():
    client = Client()
    m = am.build(client, b"abcde", True, mimetype_of=lambda d: "audio/ogg",
                 seconds_of=lambda d: 7, waveform_of=lambda d: b"\x05" * 64,
                 now=lambda: 1_700_000_123.9)
    a = m.audioMessage
    assert a.waveform == b"\x05" * 64 and a.mediaKeyTimestamp == 1_700_000_123
    assert client.uploads == [b"abcde"]
    assert (a.URL, a.directPath, a.fileLength) == ("https://mmg/x", "/v/x", 5)
    assert a.PTT is True and a.seconds == 7
    assert a.mimetype == "audio/ogg; codecs=opus"


def test_the_upload_says_it_is_audio_so_libmagic_is_never_asked():
    from neonize.utils.enum import MediaType
    client = Client()
    am.build(client, b"abcde", True, mimetype_of=lambda d: "audio/ogg",
             seconds_of=lambda d: 1, waveform_of=lambda d: b"")
    assert client.upload_types == [MediaType.MediaAudio]


def test_build_an_audio_from_a_path_keeps_its_mimetype(tmp_path):
    path = tmp_path / "a.m4a"
    path.write_bytes(b"m4a!!")
    client = Client()
    m = am.build(client, str(path), False, mimetype_of=lambda d: "audio/mp4",
                 seconds_of=lambda d: 3, waveform_of=lambda d: pytest.fail("waveform asked"))
    assert client.uploads == [b"m4a!!"]
    assert m.audioMessage.PTT is False and m.audioMessage.mimetype == "audio/mp4"


def test_waveform_scales_the_loudest_bar_to_100():
    quiet, loud = struct.pack("<h", 100), struct.pack("<h", 1000)
    pcm = quiet * 640 + loud * 640 + quiet * 2560
    bars = am.waveform_of_pcm(pcm)
    assert len(bars) == 64 and max(bars) == 100
    assert bars[0] == 10 and bars[-1] == 10


def test_waveform_is_empty_for_silence_or_too_little_audio():
    assert am.waveform_of_pcm(b"\0\0" * 6400) == b""
    assert am.waveform_of_pcm(b"\1\0" * 10) == b""
    assert am.waveform_of_pcm(b"") == b""


def test_waveform_survives_a_failing_decoder():
    def broken(data):
        raise OSError("no ffmpeg")
    assert am.waveform(b"x", pcm_of=broken) == b""


def test_a_voice_note_without_a_waveform_still_goes_out():
    m = am.build(Client(), b"abcde", True, mimetype_of=lambda d: "audio/ogg",
                 seconds_of=lambda d: 1, waveform_of=lambda d: b"")
    assert m.audioMessage.waveform == b"" and m.audioMessage.PTT is True


@pytest.fixture
def sessions(monkeypatch):
    created = []

    class Registered(server.Session):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            created.append(self)
    monkeypatch.setattr(server, "Session", Registered)
    return created


@pytest.mark.asyncio
async def test_send_voice_base64_goes_out_without_ffprobe(tmp_path, sessions, monkeypatch):
    monkeypatch.setattr(media_probe, "mimetype", lambda data: "audio/ogg")
    monkeypatch.setattr(am, "_decoded_pcm", lambda data: struct.pack("<h", 900) * 6400)
    monkeypatch.setattr(media_probe, "_ffmpeg_seconds", lambda data: pytest.fail("ffmpeg asked"))
    http = TestClient(TestServer(server.create_app(str(tmp_path))))
    await http.start_server()
    try:
        secret = (tmp_path / auth.SECRET_FILE).read_text(encoding="utf-8")
        token = f"account1:{auth.session_token(secret, 'account1')}"
        await http.get(f"/api/{token}/status-session")
        client = Client()
        s = sessions[0]
        s.client, s.status, s.me = client, "CONNECTED", "5500000000@s.whatsapp.net"
        voice = opus(48000 * 5 + 312)
        response = await http.post(f"/api/{token}/send-voice-base64", json={
            "phone": "5511999990000@c.us",
            "base64Ptt": "data:audio/ogg;base64," + base64.b64encode(voice).decode()})
        assert response.status == 200
        assert (await response.json())["status"] == "success"
        assert client.uploads == [voice]
        sent = client.sent[0][1].audioMessage
        assert sent.PTT is True and sent.seconds == 5
        assert sent.mimetype == "audio/ogg; codecs=opus"
        assert sent.waveform == b"\x64" * 64 and sent.mediaKeyTimestamp > 0
    finally:
        await http.close()
