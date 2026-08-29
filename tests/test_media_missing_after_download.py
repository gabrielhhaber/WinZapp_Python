"""A media download that fails must not be followed by opening the file.

Reported live from the group Media tab: pressing Abrir on an old document
announced "baixando...", the server answered HTTP 500 ("Failed to decrypt
file" — the WhatsApp media link had expired), and the app then opened the
file anyway. The FileNotFoundError that followed was shown verbatim in an
error box, naming an internal path:

    [Errno 2] No such file or directory:
    '...\\data\\accounts\\<id>\\media\\3EB04CF29E22FDFEAD0144.wzmedia'

The tab only made it easy to reach — it lists a group's whole history from
the database, so it routinely offers media older than anything WhatsApp
still holds. The same Open on the same message in the conversation list
behaved identically.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so _ensure_media_on_disk() is exercised as an unbound
method against a stub — same approach as tests/test_media_failed_ids.py.
"""

import types

import pytest

import ui.conversations as conversations_module
from ui.conversations import ConversationsPanel


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self, connected=True, downloads_to=None):
        self.i18n = _FakeI18n()
        self.app_name = "WinZapp"
        self._wa_connected = connected
        self.spoken = []
        self.media_calls = []
        self.audio_calls = []
        self._downloads_to = downloads_to
        self.raise_on_download = False

    def output(self, text, **kwargs):
        self.spoken.append(text)

    def _do_download(self, msg):
        if self.raise_on_download:
            raise RuntimeError("connection reset")
        if self._downloads_to is not None:
            self._downloads_to.write_bytes(b"conteudo")

    def handle_media_message(self, msg, **kwargs):
        self.media_calls.append(msg)
        self._do_download(msg)

    def handle_audio_message(self, msg, **kwargs):
        self.audio_calls.append(msg)
        self._do_download(msg)


@pytest.fixture
def direct_callafter(monkeypatch):
    """_ensure_media_on_disk() runs on a worker thread and bounces every UI
    call through wx.CallAfter; run them inline so the test can see them."""
    boxes = []

    def _call_after(fn, *args, **kwargs):
        if fn is conversations_module.wx.MessageBox:
            boxes.append(args)
            return
        fn(*args, **kwargs)

    monkeypatch.setattr(conversations_module.wx, "CallAfter", _call_after)
    return boxes


def _stub(main_window):
    stub = types.SimpleNamespace(main_window=main_window)
    stub._ensure_media_on_disk = types.MethodType(
        ConversationsPanel._ensure_media_on_disk, stub
    )
    return stub


def _msg(msg_type="documentMessage", msg_id="3EB04CF29E22FDFEAD0144"):
    return {"key": {"id": msg_id}, "messageType": msg_type}


class TestMediaAlreadyOnDisk:
    def test_no_download_is_attempted(self, tmp_path, direct_callafter):
        path = tmp_path / "cached.wzmedia"
        path.write_bytes(b"ja baixado")
        mw = _FakeMainWindow()

        assert _stub(mw)._ensure_media_on_disk(_msg(), str(path)) is True
        assert mw.media_calls == []
        assert mw.spoken == []


class TestDownloadSucceeds:
    def test_the_caller_is_allowed_to_proceed(self, tmp_path, direct_callafter):
        path = tmp_path / "novo.wzmedia"
        mw = _FakeMainWindow(downloads_to=path)

        assert _stub(mw)._ensure_media_on_disk(_msg(), str(path)) is True
        assert mw.spoken == ["downloading"]
        assert direct_callafter == []

    def test_a_voice_message_goes_through_the_audio_path(self, tmp_path, direct_callafter):
        path = tmp_path / "voz.msv"
        mw = _FakeMainWindow(downloads_to=path)

        _stub(mw)._ensure_media_on_disk(_msg("audioMessage"), str(path))
        assert mw.audio_calls and mw.media_calls == []


class TestDownloadFails:
    """The reported bug. handle_media_message() does not raise when the server
    refuses the file — it logs the 500 and returns — so "it did not raise" was
    never evidence that the file exists."""

    def test_the_caller_is_stopped(self, tmp_path, direct_callafter):
        path = tmp_path / "expirado.wzmedia"
        mw = _FakeMainWindow(downloads_to=None)   # download produces nothing

        assert _stub(mw)._ensure_media_on_disk(_msg(), str(path)) is False

    def test_the_user_gets_a_sentence_not_a_python_error(self, tmp_path, direct_callafter):
        path = tmp_path / "expirado.wzmedia"
        mw = _FakeMainWindow(downloads_to=None)

        _stub(mw)._ensure_media_on_disk(_msg(), str(path))

        assert len(direct_callafter) == 1
        message, title, _style = direct_callafter[0]
        assert message == "media_download_failed"
        assert str(path) not in message
        assert "Errno" not in message

    def test_a_download_that_raises_is_reported_the_same_way(self, tmp_path, direct_callafter):
        """A transport error and a refused file are the same story to the
        user, and neither may reach open()."""
        path = tmp_path / "quebrado.wzmedia"
        mw = _FakeMainWindow(downloads_to=None)
        mw.raise_on_download = True

        assert _stub(mw)._ensure_media_on_disk(_msg(), str(path)) is False
        assert direct_callafter[0][0] == "media_download_failed"


class TestOffline:
    """Checked before the download, because it has a different answer: the
    download did not fail, it was never attempted, and "wait for the
    connection" is actionable where "the link may have expired" would be a
    lie."""

    def test_no_download_is_attempted(self, tmp_path, direct_callafter):
        path = tmp_path / "offline.wzmedia"
        mw = _FakeMainWindow(connected=False)

        assert _stub(mw)._ensure_media_on_disk(_msg(), str(path)) is False
        assert mw.media_calls == []

    def test_the_offline_message_is_spoken_and_no_error_box_is_shown(
            self, tmp_path, direct_callafter):
        path = tmp_path / "offline.wzmedia"
        mw = _FakeMainWindow(connected=False)

        _stub(mw)._ensure_media_on_disk(_msg(), str(path))

        assert mw.spoken == ["media_download_offline"]
        assert direct_callafter == []
