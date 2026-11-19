"""send-file and get-media-by-message for real (create_app) against a fake
neonize client: the media-upload-progress and media-download-progress events
that WinZapp listens to, and the earlier path when the client is not the fork's.
Without WhatsApp and without the fork's DLL."""
import threading
from types import SimpleNamespace as N

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import DocumentMessage, ImageMessage, Message

import authentication as auth
import server

PN = "5511999990000@s.whatsapp.net"
ME = "5500000000@s.whatsapp.net"
CONTENT = b"0123456789"


class BaseClient:
    def __init__(self):
        self.uploads, self.downloaded = [], []

    def upload(self, binary, media_type=None):
        self.uploads.append(("plain", len(binary)))
        return N()

    def send_document(self, to, file, caption=None, filename=None, mimetype=None):
        with open(file, "rb") as f:
            data = f.read()
        self.upload(data, "DOC")        # build_document_message passes the type
        return self.send_message(to, Message(documentMessage=DocumentMessage(
            fileName=filename or "", fileLength=len(data))))

    def send_message(self, destination, message):
        return N(ID="NEW1", Timestamp=1_700_000_000, Message=message)

    def download_any(self, message):
        self.downloaded.append("memory")
        return CONTENT


class ClientWithoutFork(BaseClient):
    """The real neonize 0.5.2."""


class ForkClient(BaseClient):
    def __init__(self, seen):
        super().__init__()
        self.pct, self.seen, self.canceled = -1, seen, []

    def tem_transferencias(self):
        return True

    def upload_file(self, path, media_type, key):
        self.uploads.append(("file", media_type, key))
        self.pct = 40
        assert self.seen.wait(5)           # only goes on after the event went out
        self.pct = 100
        return N()

    def transfer_progress(self, key):
        return self.pct

    def transfer_cancel(self, key):
        self.canceled.append(key)

    def download_any_to_file(self, message, path, key):
        self.downloaded.append(("file", key))
        with open(path, "wb") as f:
            f.write(CONTENT[:5])
        assert self.seen.wait(5)
        with open(path, "wb") as f:
            f.write(CONTENT)


@pytest.fixture
def sessions(monkeypatch):
    created = []

    class Registered(server.Session):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            created.append(self)
    monkeypatch.setattr(server, "Session", Registered)
    return created


class Bridge:
    def __init__(self, http, token, s):
        self.http, self.token, self.s = http, token, s
        self.events = []

    def url(self, route):
        return f"/api/{self.token}/{route}"


async def _open(tmp_path, sessions, client, seen_when=None):
    http = TestClient(TestServer(server.create_app(str(tmp_path))))
    await http.start_server()
    secret = (tmp_path / auth.SECRET_FILE).read_text(encoding="utf-8")
    token = f"account1:{auth.session_token(secret, 'account1')}"
    await http.get(f"/api/{token}/status-session")
    s = sessions[0]
    s.client, s.status, s.me = client, "CONNECTED", ME
    bridge = Bridge(http, token, s)

    def emit(event, data):
        bridge.events.append((event, data))
        if seen_when and seen_when(event, data):
            client.seen.set()
    s.emit = emit
    return bridge


def _send_file(upload_id=None):
    form = FormData()
    form.add_field("phone", PN)
    form.add_field("type", "document")
    if upload_id is not None:
        form.add_field("uploadId", upload_id)
    form.add_field("file", b"x" * 64, filename="a.bin", content_type="application/octet-stream")
    return form


def _store_image(s):
    s.db.chat(PN, name="Ana")
    w = {"id": {"id": "M1", "fromMe": False, "remote": PN, "_serialized": f"false_{PN}_M1"},
         "fromMe": False, "t": 100, "timestamp": 100, "type": "image", "body": "",
         "from": PN, "to": ME}
    s.db.message(PN, w, Message(imageMessage=ImageMessage(fileLength=len(CONTENT))).SerializeToString())
    return f"false_{PN}_M1"


def _of_upload(bridge):
    return [d for e, d in bridge.events if e == "media-upload-progress"]


def _of_download(bridge):
    return [d for e, d in bridge.events if e == "media-download-progress"]


# -------------------------------------------------------------- send-file
@pytest.mark.asyncio
async def test_send_file_emits_media_upload_progress_with_the_clients_upload_id(tmp_path, sessions):
    seen = threading.Event()
    client = ForkClient(seen)
    bridge = await _open(tmp_path, sessions, client,
                         lambda e, d: e == "media-upload-progress" and d["progress"] == 0.4)
    try:
        response = await bridge.http.post(bridge.url("send-file"), data=_send_file("LOCAL-7"))
        assert response.status == 200
        events = _of_upload(bridge)
        assert events[0] == {"uploadId": "LOCAL-7", "progress": 0.4, "session": "account1"}
        assert events[-1] == {"uploadId": "LOCAL-7", "progress": 1.0, "session": "account1"}
        assert client.uploads[0][0] == "file"       # went up through the disk, not the bytes
        assert not any(u[0] == "plain" for u in client.uploads)
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_send_file_without_the_fork_sends_as_before_without_events(tmp_path, sessions):
    client = ClientWithoutFork()
    bridge = await _open(tmp_path, sessions, client)
    try:
        response = await bridge.http.post(bridge.url("send-file"), data=_send_file("LOCAL-7"))
        assert response.status == 200
        assert client.uploads == [("plain", 64)] and bridge.events == []
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_send_file_without_an_upload_id_emits_nothing(tmp_path, sessions):
    client = ForkClient(threading.Event())
    bridge = await _open(tmp_path, sessions, client)
    try:
        response = await bridge.http.post(bridge.url("send-file"), data=_send_file())
        assert response.status == 200
        assert client.uploads == [("plain", 64)] and bridge.events == []
    finally:
        await bridge.http.close()


# -------------------------------------------------------------- get-media
@pytest.mark.asyncio
async def test_get_media_emits_media_download_progress_with_the_progress_id(tmp_path, sessions):
    seen = threading.Event()
    client = ForkClient(seen)
    bridge = await _open(tmp_path, sessions, client,
                         lambda e, d: e == "media-download-progress" and d["progress"] == 0.5)
    try:
        serial = _store_image(bridge.s)
        response = await bridge.http.post(
            bridge.url(f"get-media-by-message/{serial}"), json={"progressId": "M1"},
            headers={"Accept": "application/octet-stream"})
        assert response.status == 200 and await response.read() == CONTENT
        events = _of_download(bridge)
        assert events[0] == {"progressId": "M1", "progress": 0.5, "session": "account1"}
        assert events[-1] == {"progressId": "M1", "progress": 1.0, "session": "account1"}
        assert client.downloaded[0][0] == "file"
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("client_of,body", [
    (lambda: ClientWithoutFork(), {"progressId": "M1"}),          # without the fork
    (lambda: ForkClient(threading.Event()), None),            # without a body
    (lambda: ForkClient(threading.Event()), {"x": 1}),        # without progressId
])
async def test_get_media_falls_back_to_the_earlier_download_without_events(tmp_path, sessions, client_of, body):
    client = client_of()
    bridge = await _open(tmp_path, sessions, client)
    try:
        serial = _store_image(bridge.s)
        response = await bridge.http.post(
            bridge.url(f"get-media-by-message/{serial}"), json=body,
            headers={"Accept": "application/octet-stream"})
        assert response.status == 200 and await response.read() == CONTENT
        assert client.downloaded == ["memory"] and bridge.events == []
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_get_media_with_a_download_error_still_answers_410(tmp_path, sessions):
    seen = threading.Event()
    client = ForkClient(seen)

    def fails(*a):
        raise RuntimeError("404 not found")
    client.download_any_to_file = fails
    bridge = await _open(tmp_path, sessions, client)
    try:
        serial = _store_image(bridge.s)
        response = await bridge.http.post(
            bridge.url(f"get-media-by-message/{serial}"), json={"progressId": "M1"})
        assert response.status == 410
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "status", "code"), [
    ("no_answer", 504, "media_retry_no_answer"),
    ("unreadable", 410, "media_retry_unreadable"),
    ("gone", 410, "media_gone_from_phone"),
])
async def test_get_media_says_why_the_phones_copy_failed(tmp_path, sessions, monkeypatch,
                                                        error, status, code):
    """Issue #766: a phone that did not answer is worth another try (504, not
    remembered as expired); each failure carries the code the client words."""
    import media_retry
    raised = {"no_answer": media_retry.PhoneDidNotAnswer("the phone did not answer"),
              "unreadable": media_retry.RetriedUnreadable("invalid media hmac"),
              "gone": media_retry.MediaGone("404 not found")}[error]

    async def fails(*a, **k):
        raise raised
    monkeypatch.setattr(media_retry, "download_async", fails)
    bridge = await _open(tmp_path, sessions, ClientWithoutFork())
    try:
        serial = _store_image(bridge.s)
        response = await bridge.http.post(
            bridge.url(f"get-media-by-message/{serial}"), json={"progressId": "M1"})
        assert response.status == status and (await response.json())["code"] == code
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_get_media_also_answers_a_get_as_upstream_does(tmp_path, sessions):
    client = ClientWithoutFork()
    bridge = await _open(tmp_path, sessions, client)
    try:
        serial = _store_image(bridge.s)
        response = await bridge.http.get(
            bridge.url(f"get-media-by-message/{serial}"),
            headers={"Accept": "application/octet-stream"})
        assert response.status == 200 and await response.read() == CONTENT
    finally:
        await bridge.http.close()
