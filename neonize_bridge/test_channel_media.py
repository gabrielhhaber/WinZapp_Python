"""A channel's photo or video goes up unencrypted and out with its handle."""
from types import SimpleNamespace

import pytest
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ImageMessage, Message, VideoMessage

import channel_media


class FakeClient:
    def __init__(self):
        self.uploads, self.sent = [], []

    def upload_newsletter(self, binary, media_type):
        self.uploads.append((binary, media_type))
        return SimpleNamespace(url="https://mmg/u", DirectPath="/d", Handle=f"h{len(binary)}",
                               MediaKey=b"", FileEncSHA256=b"", FileSHA256=b"sha",
                               FileLength=len(binary))

    def upload(self, binary, media_type=None):
        raise AssertionError("a channel's media never takes the encrypted upload")

    def build_image_message(self, file, caption=None):
        # neonize's builder: self.upload, then every field of the answer
        upload = self.upload(file)
        return Message(imageMessage=ImageMessage(
            URL=upload.url, directPath=upload.DirectPath, caption=caption,
            mediaKey=upload.MediaKey, fileEncSHA256=upload.FileEncSHA256,
            fileSHA256=upload.FileSHA256, fileLength=upload.FileLength))

    def send_newsletter_message(self, to, message, media_handle=""):
        self.sent.append((to, message, media_handle))
        return SimpleNamespace(ID="M1", Message=message, Timestamp=0)


CHANNEL = SimpleNamespace(User="123", Server="newsletter")
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 40


def test_a_photo_goes_up_unencrypted_and_out_with_its_handle():
    client = FakeClient()
    resp = channel_media.send(client, CHANNEL, "image", PNG, "Hello")
    (binary, _kind), = client.uploads
    to, message, handle = client.sent[0]
    assert resp.ID == "M1" and binary == PNG and to is CHANNEL
    assert handle == f"h{len(PNG)}"
    image = message.imageMessage
    assert image.caption == "Hello" and image.fileSHA256 == b"sha" and image.URL == "https://mmg/u"
    assert not image.HasField("mediaKey") and not image.HasField("fileEncSHA256")


def test_a_video_keeps_the_handle_of_the_file_not_of_its_thumbnail():
    client = FakeClient()

    def video_builder(uploads, file, caption):
        uploads.upload(b"t" * 5)                 # a thumbnail
        upload = uploads.upload(file)
        uploads.upload(b"t" * 5)
        return Message(videoMessage=VideoMessage(
            URL=upload.url, mediaKey=b"k", fileEncSHA256=b"e", thumbnailEncSHA256=b"e",
            fileSHA256=upload.FileSHA256, caption=caption))
    channel_media.send(client, CHANNEL, "video", b"v" * 50, "", video_builder)
    _to, message, handle = client.sent[0]
    assert handle == "h50"
    video = message.videoMessage
    assert not video.HasField("mediaKey") and not video.HasField("thumbnailEncSHA256")
    assert not video.HasField("caption")


def test_a_document_or_voice_is_refused_before_any_upload():
    client = FakeClient()
    for kind in ("document", "audio", ""):
        with pytest.raises(channel_media.ChannelMediaError):
            channel_media.send(client, CHANNEL, kind, b"data", "")
    assert client.uploads == [] and client.sent == []


def test_an_upload_with_no_handle_is_not_sent():
    client = FakeClient()
    client.upload_newsletter = lambda binary, media_type: SimpleNamespace(
        url="u", DirectPath="/d", Handle="", MediaKey=b"", FileEncSHA256=b"",
        FileSHA256=b"s", FileLength=1)
    with pytest.raises(channel_media.ChannelMediaError):
        channel_media.send(client, CHANNEL, "image", PNG, "")
    assert client.sent == []


def test_a_build_without_the_handle_cannot_post_media():
    class Old:
        def upload_newsletter(self, binary, media_type):
            return None
    assert not channel_media.can_post_media(Old())
    assert channel_media.can_post_media(FakeClient())
    with pytest.raises(channel_media.ChannelMediaError):
        channel_media.send(Old(), CHANNEL, "image", PNG, "")


def test_winzapps_neonize_build_posts_media():
    # the build this bridge ships (neonize patch 0018), asked without a client
    assert channel_media.can_post_media()


def test_only_a_newsletter_is_a_channel():
    assert channel_media.is_channel(CHANNEL)
    assert not channel_media.is_channel(SimpleNamespace(User="1", Server="g.us"))
    assert not channel_media.is_channel(None)


class VideoClient(FakeClient):
    """What video_message.build asks of neonize's client besides the upload."""

    def _parse_mention(self, text=None, are_lids=False):
        return []

    def _parse_group_mention(self, text=None):
        return []


@pytest.fixture
def sessions(monkeypatch):
    import server
    created = []

    class Registered(server.Session):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            created.append(self)
    monkeypatch.setattr(server, "Session", Registered)
    return created


@pytest.mark.asyncio
async def test_send_file_of_a_video_to_a_channel_goes_through_the_newsletter_upload(
        tmp_path, sessions, monkeypatch):
    from aiohttp import FormData
    from aiohttp.test_utils import TestClient, TestServer

    import authentication as auth
    import media_probe
    import server
    from test_media_probe import mp4
    monkeypatch.setattr(media_probe, "mimetype", lambda data: "video/mp4")
    monkeypatch.setattr(media_probe, "thumbnail_and_seconds", lambda file: (b"JPEG", None))
    http = TestClient(TestServer(server.create_app(str(tmp_path))))
    await http.start_server()
    try:
        secret = (tmp_path / auth.SECRET_FILE).read_text(encoding="utf-8")
        token = f"account1:{auth.session_token(secret, 'account1')}"
        await http.get(f"/api/{token}/status-session")
        client = VideoClient()
        s = sessions[0]
        s.client, s.status, s.me = client, "CONNECTED", "5500000000@s.whatsapp.net"
        capabilities = (await (await http.get(f"/api/{token}/send-capabilities")).json())
        assert capabilities["response"]["channelMedia"] is True
        video = mp4(1000, 8000)
        form = FormData()
        form.add_field("phone", "120363000000000001@newsletter")
        form.add_field("type", "video")
        form.add_field("caption", "olha")
        form.add_field("file", video, filename="v.mp4", content_type="video/mp4")
        response = await http.post(f"/api/{token}/send-file", data=form)
        assert response.status == 200
        assert (await response.json())["status"] == "success"
        assert [binary for binary, _kind in client.uploads] == [video]
        to, message, handle = client.sent[0]
        assert to.Server == "newsletter" and handle == f"h{len(video)}"
        sent = message.videoMessage
        assert sent.caption == "olha" and sent.seconds == 8 and not sent.HasField("mediaKey")
    finally:
        await http.close()


def test_a_single_answer_poll_goes_as_v3_without_a_secret():
    client = FakeClient()
    channel_media.send_poll(client, CHANNEL, "Lunch?", ["Yes", "No", " "], False)
    _to, message, handle = client.sent[0]
    poll = message.pollCreationMessageV3
    assert handle == "" and poll.name == "Lunch?" and poll.selectableOptionsCount == 1
    assert [o.optionName for o in poll.options] == ["Yes", "No"]
    assert not message.HasField("pollCreationMessage")
    assert not message.HasField("messageContextInfo")


def test_a_poll_with_several_answers_goes_as_the_first_version():
    message = channel_media.poll_message("Days?", ["Mon", "Tue", "Wed"], True)
    assert message.pollCreationMessage.selectableOptionsCount == 0
    assert not message.HasField("pollCreationMessageV3")


def test_a_poll_without_two_options_is_refused():
    client = FakeClient()
    for name, choices in (("Q", ["only"]), ("", ["a", "b"]), ("Q", ["a", ""])):
        with pytest.raises(channel_media.ChannelMediaError):
            channel_media.send_poll(client, CHANNEL, name, choices, False)
    assert client.sent == []


@pytest.mark.asyncio
async def test_send_poll_message_to_a_channel_skips_the_chat_poll(tmp_path, sessions):
    from aiohttp.test_utils import TestClient, TestServer

    import authentication as auth
    import server
    http = TestClient(TestServer(server.create_app(str(tmp_path))))
    await http.start_server()
    try:
        secret = (tmp_path / auth.SECRET_FILE).read_text(encoding="utf-8")
        token = f"account1:{auth.session_token(secret, 'account1')}"
        await http.get(f"/api/{token}/status-session")
        client = FakeClient()
        client.build_poll_vote_creation = lambda *args: pytest.fail("a chat poll has a secret")
        s = sessions[0]
        s.client, s.status, s.me = client, "CONNECTED", "5500000000@s.whatsapp.net"
        capabilities = (await (await http.get(f"/api/{token}/send-capabilities")).json())
        assert capabilities["response"]["channelPolls"] is True
        response = await http.post(f"/api/{token}/send-poll-message", json={
            "phone": "120363000000000001@newsletter", "name": "Lunch?",
            "choices": ["Yes", "No"], "options": {"selectableCount": 1}})
        assert response.status == 200
        _to, message, _handle = client.sent[0]
        assert message.pollCreationMessageV3.name == "Lunch?"
    finally:
        await http.close()
