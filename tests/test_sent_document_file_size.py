"""Issue #96 — a document WinZapp sends must show its size, like a received one.

The rendering was never the problem: _get_message_content() keys only on
documentMessage.fileLength and does not care who sent the message. The field
simply never reached it for our own sends.

The virtual (pending) message _on_send_attachment() builds carried caption,
fileName and mimetype but no fileLength, and on_new_message() merges
WPPConnect's echo into that pending record by copying id/timestamp/participant
onto it — the echo's own normalised body, which DOES carry the size, is
discarded. So the record written to the DB never had it either. The size
reappeared only if a later resync re-fetched the message from the server
through _normalize_wpp_message(), which is why old sent files showed a size and
a freshly sent one did not.

These tests drive the real _on_send_attachment() against a real file on disk
rather than asserting on its source text: this is the third bug in a row in
this area where a protection existed and did not do its job, and a source-level
assertion cannot tell those apart.
"""

import types

import pytest

import ui.conversations as conversations_module
from ui.conversations import ConversationsPanel


class _FakeList:
    def __init__(self):
        self.items = []

    def Append(self, row):
        self.items.append(row[0])

    def GetItemCount(self):
        return len(self.items)

    def Select(self, idx, on):
        pass

    def EnsureVisible(self, idx):
        pass


class _SendStub:
    """Everything _on_send_attachment() touches, and nothing else."""

    _on_send_attachment = ConversationsPanel._on_send_attachment
    _get_message_content = ConversationsPanel._get_message_content
    _format_filesize = ConversationsPanel._format_filesize
    _format_duration = ConversationsPanel._format_duration

    def __init__(self, staged, i18n):
        self._staged_attachments = staged
        self.conversation = {"remoteJid": "5511999999999@s.whatsapp.net"}
        self._quoted_message = None
        self._sorted_messages = []
        self.messages_list = _FakeList()
        self.message_field = types.SimpleNamespace(SetFocus=lambda: None)
        self._download_progress = {}
        self.enqueued = []
        self.main_window = types.SimpleNamespace(
            i18n=i18n,
            settings={},
            message_queue=types.SimpleNamespace(enqueue=self.enqueued.append),
            mark_conversation_as_read=lambda jid: None,
            _schedule_set_chats=lambda: None,
        )

    # — collaborators the method calls, all irrelevant to what is asserted —
    def _consume_attachment_caption(self):
        return ""

    def _clear_empty_placeholder(self):
        pass

    def _render_message_line(self, msg):
        return self._get_message_content(msg)

    def _register_virtual_msg(self, msg):
        pass

    def _pre_cache_sent_media(self, local_id, path, media_type):
        pass

    def update_media_upload_progress(self, local_id, progress):
        pass

    def _show_media_transfer_gauge(self):
        pass

    def _sync_pending_document_gauge(self):
        pass

    def _hide_attachment_panel(self):
        pass

    def _on_cancel_reply(self):
        pass

    def _probe_audio_duration(self, path):
        return None


@pytest.fixture
def i18n():
    from core.i18n import I18n

    return I18n("pt-BR")


@pytest.fixture(autouse=True)
def _no_background_thread(monkeypatch):
    """The real method caches and enqueues on a daemon thread; run it inline so
    the assertions do not race it."""

    class _InlineThread:
        def __init__(self, target=None, daemon=None, args=(), kwargs=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            if self._target is not None:
                self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(conversations_module.threading, "Thread", _InlineThread)


def _send(tmp_path, i18n, name, size_bytes, media_type="document"):
    path = tmp_path / name
    path.write_bytes(b"\0" * size_bytes)
    stub = _SendStub(
        [{"path": str(path), "media_type": media_type}], i18n
    )
    stub._on_send_attachment()
    return stub


def test_a_sent_document_carries_its_size(tmp_path, i18n):
    stub = _send(tmp_path, i18n, "example-file.exe", 41_300_000)

    body = stub._sorted_messages[0]["message"]["documentMessage"]
    assert body["fileLength"] == 41_300_000


def test_the_rendered_line_shows_the_size(tmp_path, i18n):
    """The issue as reported: "You: Document, example-file.exe, 39.4 MB"."""
    stub = _send(tmp_path, i18n, "example-file.exe", 41_300_000)

    line = stub.messages_list.items[0]
    assert "example-file.exe" in line
    assert "39,4 mb" in line, line


def test_the_size_is_there_from_the_first_render(tmp_path, i18n):
    """Filled locally rather than taken from WPPConnect's echo on purpose: the
    row is complete when it first appears, so it is never rewritten later.
    Rewriting a list row is what makes a screen reader read the whole row out
    again — the bug _release_chain_held_repaints() exists for."""
    stub = _send(tmp_path, i18n, "relatorio.pdf", 2048)

    assert stub.messages_list.items[0] == stub._get_message_content(
        stub._sorted_messages[0]
    )
    assert "2,0 kb" in stub.messages_list.items[0]


def test_an_empty_document_reads_the_same_as_a_received_one(tmp_path, i18n):
    """A 0-byte file renders "0 b" — which is what a RECEIVED document already
    reads as, both for a genuinely empty one and for one whose size WPPConnect
    did not state (_normalize_wpp_message() falls back to 0). Parity is the
    whole point of the issue, so sent must not be special-cased here."""
    stub = _send(tmp_path, i18n, "vazio.txt", 0)

    body = stub._sorted_messages[0]["message"]["documentMessage"]
    assert body["fileLength"] == 0

    received = {
        "key": {"id": "r1", "fromMe": False},
        "messageType": "documentMessage",
        "message": {"documentMessage": {"fileName": "vazio.txt", "fileLength": 0}},
    }
    assert stub.messages_list.items[0] == stub._get_message_content(received)


def test_an_unreadable_size_does_not_block_the_send(tmp_path, i18n, monkeypatch):
    """getsize() failing must cost the size clause, never the message: this is
    the same OSError path that used to only skip the too-large check."""
    monkeypatch.setattr(
        conversations_module.os.path,
        "getsize",
        lambda p: (_ for _ in ()).throw(OSError),
    )
    stub = _send(tmp_path, i18n, "arquivo.bin", 10)

    assert len(stub._sorted_messages) == 1
    assert len(stub.enqueued) == 1
    assert "fileLength" not in stub._sorted_messages[0]["message"]["documentMessage"]


@pytest.mark.parametrize("media_type", ["image", "video", "audio"])
def test_other_media_types_are_left_alone(tmp_path, i18n, media_type):
    """Only documents render a size, and fileLength is also read by
    MainWindow.sync_if_media() as an auto-download size gate — populating it
    for our own images/videos would change that decision, which is outside
    what issue #96 asked for."""
    stub = _send(tmp_path, i18n, "arquivo.bin", 4096, media_type=media_type)

    vtype = stub._sorted_messages[0]["messageType"]
    assert "fileLength" not in stub._sorted_messages[0]["message"][vtype]
