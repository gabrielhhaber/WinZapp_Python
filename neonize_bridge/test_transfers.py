"""Sending and downloading with progress against fake clients: one that exposes the
calls of the DLL fork and one that does not (PyPI's neonize 0.5.2, an older
install). Nothing here loads the fork's DLL."""
import asyncio
import os
import threading
from types import SimpleNamespace as N

import pytest

import transfers as t


class BaseClient:
    """The format of NewClient: send_image -> build_image_message -> upload."""

    def __init__(self):
        self.uploads, self.sent, self.downloaded = [], [], []

    def upload(self, binary, media_type=None):
        self.uploads.append(("plain", len(binary), media_type))
        return N(origin="upload")

    def build_image_message(self, file, caption=None):
        with open(file, "rb") as f:
            data = f.read()
        return N(upload=self.upload(data), caption=caption)

    def send_image(self, to, file, caption=None):
        return self.send_message(to, self.build_image_message(file, caption))

    def send_message(self, to, message):
        self.sent.append((to, message))
        return N(ID="X1", message=message)

    def download_any(self, message):
        self.downloaded.append(("memory", message))
        return b"data"


class ClientWithoutFork(BaseClient):
    """The real neonize 0.5.2: no upload_file nor transfer_*."""


class ForkClient(BaseClient):
    def __init__(self, downloaded_content=b"abcdefghij", has=True):
        super().__init__()
        self.has, self.pct = has, -1
        self.release = threading.Event()
        self.arrived = threading.Event()
        self.canceled, self.content = [], downloaded_content
        self.write = None          # callback(path) in the middle of the download

    def tem_transferencias(self):
        return self.has

    def upload_file(self, path, media_type, key):
        self.uploads.append(("file", path, media_type, key))
        self.arrived.set()
        assert self.release.wait(5)
        return N(origin="upload_file")

    def transfer_progress(self, key):
        return self.pct

    def transfer_cancel(self, key):
        self.canceled.append(key)

    def download_any_to_file(self, message, path, key):
        self.downloaded.append(("file", key))
        if self.write:
            self.write(path)
        self.arrived.set()
        assert self.release.wait(5)
        with open(path, "wb") as f:
            f.write(self.content)


def _wait(condition, timeout=5):
    end = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if condition():
            return
        end.wait(0.01)
    raise AssertionError("it did not happen")


def _png(tmp_path, length=64):
    path = tmp_path / "a.png"
    path.write_bytes(b"x" * length)
    return str(path)


def _kind(_binary):
    return "KIND_FROM_CONTENT"


# ------------------------------------------------------------ detection
def test_has_transfers_requires_the_four_calls_and_the_dll():
    assert t.has_transfers(ForkClient()) is True
    assert t.has_transfers(ForkClient(has=False)) is False      # DLL without the exports
    assert t.has_transfers(ClientWithoutFork()) is False        # the real neonize
    partial = ForkClient()
    partial.transfer_cancel = None
    assert t.has_transfers(partial) is False


def test_has_transfers_with_a_dll_that_raises_is_false():
    c = ForkClient()
    c.tem_transferencias = lambda: (_ for _ in ()).throw(OSError("dll"))
    assert t.has_transfers(c) is False


def test_expected_size_takes_the_filelength_of_the_media():
    m = N(imageMessage=N(fileLength=0), videoMessage=N(fileLength=2048))
    assert t.expected_size(m) == 2048
    assert t.expected_size(N(documentMessage=N(fileLength="x"))) == 0
    assert t.expected_size(N()) == 0


def test_expected_size_with_a_real_protobuf():
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import DocumentMessage, Message
    m = Message(documentMessage=DocumentMessage(fileLength=777))
    assert t.expected_size(m) == 777
    assert t.expected_size(Message(conversation="oi")) == 0


# ------------------------------------------------------------ client with progress
def test_the_proxy_sends_the_files_upload_through_the_disk(tmp_path):
    c = ForkClient()
    c.release.set()
    path = _png(tmp_path)
    proxy = t.ClientWithProgress(c, path, "up-k", kind_from_content=_kind)
    response = proxy.send_image("dest", path, caption="oi")
    # build_image_message and send_message ran, but the upload was the fork's
    assert c.uploads == [("file", path, "KIND_FROM_CONTENT", "up-k")]
    assert response.message.upload.origin == "upload_file"
    assert c.sent[0][0] == "dest"


def test_the_proxy_lets_through_an_upload_of_another_size_and_the_given_type(tmp_path):
    c = ForkClient()
    path = _png(tmp_path, 64)
    proxy = t.ClientWithProgress(c, path, "k", kind_from_content=_kind)
    proxy.upload(b"thumbnail", "KIND")          # it is not the file: plain path
    assert c.uploads == [("plain", 9, "KIND")]
    c.release.set()
    proxy.upload(b"x" * 64, "DOC")               # it is the file, with the given type
    assert c.uploads[1] == ("file", path, "DOC", "k")


def test_the_proxy_with_a_file_that_vanished_uses_the_plain_upload(tmp_path):
    c = ForkClient()
    proxy = t.ClientWithProgress(c, str(tmp_path / "does-not-exist"), "k", kind_from_content=_kind)
    proxy.upload(b"abc")
    assert c.uploads == [("plain", 3, None)]


def test_the_proxy_passes_attributes_to_the_real_client(tmp_path):
    c = ForkClient()
    c.uuid = "u-1"
    proxy = t.ClientWithProgress(c, _png(tmp_path), "k")
    assert proxy.uuid == "u-1"


# ------------------------------------------------------------ upload
def test_upload_emits_the_real_progress_and_ends_at_100(tmp_path):
    c = ForkClient()
    events = []
    path = _png(tmp_path)
    result = []

    def send(client):
        return client.send_image("dest", path)

    def run():
        result.append(t.send_with_progress(
            c, path, "UP1", lambda e, d: events.append((e, d)), "account1", send,
            interval=0.01, minimum=0.0))
    thread = threading.Thread(target=run)
    thread.start()
    assert c.arrived.wait(5)
    c.pct = 10
    _wait(lambda: any(d["progress"] == 0.1 for _, d in events))
    c.pct = 55
    _wait(lambda: any(d["progress"] == 0.55 for _, d in events))
    c.release.set()
    thread.join(5)
    assert result and result[0].ID == "X1"
    assert all(e == "media-upload-progress" for e, _ in events)
    assert all(d["uploadId"] == "UP1" and d["session"] == "account1" for _, d in events)
    progresses = [d["progress"] for _, d in events]
    assert progresses == sorted(progresses) and progresses[-1] == 1.0


def test_upload_without_the_forks_calls_sends_as_before_without_events(tmp_path):
    c = ClientWithoutFork()
    events = []
    path = _png(tmp_path)
    response = t.send_with_progress(
        c, path, "UP1", lambda e, d: events.append(e), "account1",
        lambda client: client.send_image("dest", path))
    assert response.ID == "X1"
    assert c.uploads == [("plain", 64, None)] and events == []


def test_upload_without_an_id_or_with_bytes_does_not_use_the_fork(tmp_path):
    c = ForkClient()
    used = []
    for upload_id, source in (("", _png(tmp_path)), ("UP1", b"bytes")):
        t.send_with_progress(c, source, upload_id, lambda *a: None, "s",
                             lambda client: used.append(client))
    assert used == [c, c]          # the real client, without a proxy
    assert c.uploads == []


def test_upload_that_fails_does_not_emit_100_and_propagates(tmp_path):
    c = ForkClient()
    events = []
    path = _png(tmp_path)

    def send(client):
        raise RuntimeError("network dropped")
    with pytest.raises(RuntimeError):
        t.send_with_progress(c, path, "UP1", lambda e, d: events.append(d), "s",
                             send, interval=0.01, minimum=0.0)
    assert events == []


@pytest.mark.asyncio
async def test_canceled_request_cancels_the_upload_in_the_dll(tmp_path):
    c = ForkClient()
    path = _png(tmp_path)
    task = asyncio.ensure_future(t.send_with_progress_async(
        c, path, "UP1", lambda *a: None, "s",
        lambda client: client.send_image("dest", path)))
    await asyncio.get_running_loop().run_in_executor(None, c.arrived.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(c.canceled) == 1 and c.canceled[0].startswith("up-")
    c.release.set()         # releases the fake upload's thread


# ------------------------------------------------------------ download
def test_download_measures_the_file_against_the_filelength(tmp_path):
    c = ForkClient(downloaded_content=b"0123456789")
    events = []
    message = N(imageMessage=N(fileLength=10))
    written = threading.Event()

    def middle(path):
        with open(path, "wb") as f:
            f.write(b"01234")             # half
        written.set()
    c.write = middle
    result = []
    thread = threading.Thread(target=lambda: result.append(t.download_with_progress(
        c, message, "DL1", lambda e, d: events.append((e, d)), "account1",
        interval=0.01, minimum=0.0)))
    thread.start()
    assert written.wait(5)
    _wait(lambda: any(d["progress"] == 0.5 for _, d in events))
    c.release.set()
    thread.join(5)
    assert result == [b"0123456789"]
    assert all(e == "media-download-progress" for e, _ in events)
    assert all(d["progressId"] == "DL1" and d["session"] == "account1" for _, d in events)
    assert events[-1][1]["progress"] == 1.0
    assert c.downloaded[0][0] == "file"


def test_download_deletes_the_temporary_file(tmp_path, monkeypatch):
    c = ForkClient(downloaded_content=b"a" * 10)
    c.release.set()
    created = []
    original = t.tempfile.mkstemp

    def mkstemp(*a, **kw):
        d, path = original(*a, dir=str(tmp_path), **kw)
        created.append(path)
        return d, path
    monkeypatch.setattr(t.tempfile, "mkstemp", mkstemp)
    t.download_with_progress(c, N(imageMessage=N(fileLength=10)), "DL1", lambda *a: None, "s")
    assert len(created) == 1 and not os.path.exists(created[0])


def test_download_that_fails_deletes_the_temporary_and_propagates(tmp_path, monkeypatch):
    c = ForkClient()
    created = []
    original = t.tempfile.mkstemp

    def mkstemp(*a, **kw):
        d, path = original(*a, dir=str(tmp_path), **kw)
        created.append(path)
        return d, path
    monkeypatch.setattr(t.tempfile, "mkstemp", mkstemp)

    def fails(*a):
        raise RuntimeError("404 not found")
    c.download_any_to_file = fails
    with pytest.raises(RuntimeError, match="404"):
        t.download_with_progress(c, N(imageMessage=N(fileLength=10)), "DL1", lambda *a: None, "s")
    assert created and not os.path.exists(created[0])


@pytest.mark.parametrize("client,message,progress_id", [
    (ClientWithoutFork(), N(imageMessage=N(fileLength=10)), "DL1"),       # without the fork
    (ForkClient(has=False), N(imageMessage=N(fileLength=10)), "DL1"),  # DLL without exports
    (ForkClient(), N(imageMessage=N(fileLength=10)), ""),             # without progressId
    (ForkClient(), N(imageMessage=N(fileLength=0)), "DL1"),           # unknown size
])
def test_download_falls_back_to_the_earlier_download_any_without_events(client, message, progress_id):
    events = []
    data = t.download_with_progress(client, message, progress_id,
                                    lambda e, d: events.append(e), "s")
    assert data == b"data" and events == []
    assert client.downloaded == [("memory", message)]


@pytest.mark.asyncio
async def test_async_download_reads_the_progress_id_from_the_body_and_cancels_in_the_dll():
    c = ForkClient()
    events = []
    message = N(imageMessage=N(fileLength=10))
    task = asyncio.ensure_future(t.download_with_progress_async(
        c, message, {"progressId": "DL9"}, lambda e, d: events.append(d), "s"))
    await asyncio.get_running_loop().run_in_executor(None, c.arrived.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(c.canceled) == 1 and c.canceled[0].startswith("down-")
    c.release.set()


@pytest.mark.asyncio
async def test_async_download_without_a_body_uses_the_old_path():
    c = ForkClient()
    data = await t.download_with_progress_async(
        c, N(imageMessage=N(fileLength=10)), None, lambda *a: None, "s")
    assert data == b"data"


def test_the_proxy_keeps_what_the_session_wrapped_on_the_client(tmp_path):
    """chat_timers.install wraps the client's own send_message; the proxy
    rebinding the class's skipped it, and a file sent with progress went out
    without the chat's disappearing-messages timer."""
    c = ForkClient()
    c.release.set()
    wrapped = []
    original = c.send_message

    def send_message(to, message, *args, **kwargs):
        wrapped.append(to)
        return original(to, message, *args, **kwargs)

    c.send_message = send_message
    path = _png(tmp_path)
    proxy = t.ClientWithProgress(c, path, "k", kind_from_content=_kind)
    proxy.send_image("dest", path)
    assert wrapped == ["dest"]
    # the upload still goes through the proxy (the disk, with progress)
    assert c.uploads[0][0] == "file"


def test_a_timer_reaches_an_audio_sent_with_progress(tmp_path):
    from types import SimpleNamespace
    import chat_timers
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import AudioMessage, Message
    c = ForkClient()
    chat_timers.install(c, lambda jid: (86400, 7))
    proxy = t.ClientWithProgress(c, _png(tmp_path), "k", kind_from_content=_kind)
    proxy.send_message(SimpleNamespace(User="55", Server="s.whatsapp.net"),
                       Message(audioMessage=AudioMessage(seconds=3)))
    sent = c.sent[0][1]
    assert sent.audioMessage.contextInfo.expiration == 86400
