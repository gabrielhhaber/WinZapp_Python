"""Copying a selection of files to the clipboard (CF_HDROP) in one go."""

import os

import pytest

from ui.conversation_panel import bulk_copy_files
from ui.conversation_panel.bulk_copy_files import BulkCopyFilesMixin
from ui.conversation_panel.bulk_copy_plan import split_copy_selection, unique_copy_name


class _I18n:
    def t(self, key):
        return key


class _Window:
    key = b""

    def __init__(self, connected=True):
        self.i18n = _I18n()
        self._wa_connected = connected
        self.announced = []

    def output(self, text, interrupt=False):
        self.announced.append(text)


class _Panel:
    _stage = BulkCopyFilesMixin._stage_files_for_bulk_copy
    _finish_bulk_copy = BulkCopyFilesMixin._finish_bulk_copy

    def __init__(self, connected=True, downloads=True):
        self.main_window = _Window(connected)
        self.downloads = downloads
        self.finished = []

    def _resolve_media_filename(self, msg):
        return msg["_name"]

    def _download_media_to_disk(self, msg, media_path):
        if not self.downloads:
            return False
        with open(media_path, "wb") as fh:
            fh.write(b"data")
        return True


def _msg(msg_id, name, msg_type="documentMessage"):
    return {"key": {"id": msg_id}, "messageType": msg_type, "_name": name}


@pytest.fixture
def staged(monkeypatch, tmp_path):
    """Runs CallAfter inline, caches under tmp_path, skips real decryption."""
    calls = []
    monkeypatch.setattr(bulk_copy_files.wx, "CallAfter", lambda f, *a: f(*a))
    monkeypatch.setattr(bulk_copy_files, "decrypt_bytes", lambda raw, key: raw)
    monkeypatch.setattr(
        bulk_copy_files, "cached_media_path",
        lambda msg_type, msg_id: str(tmp_path / f"{msg_id}.cache"),
    )
    monkeypatch.setattr(
        bulk_copy_files, "set_clipboard_files_and_text",
        lambda paths, text: calls.append((list(paths), text)) or True,
    )
    return calls


class TestSplit:
    def test_separates_text_files_and_ignores_the_rest(self):
        msgs = [
            {"messageType": "conversation"}, {"messageType": "documentMessage"},
            {"messageType": "locationMessage"}, {"messageType": "audioMessage"},
            {"messageType": "extendedTextMessage"}, {"messageType": "imageMessage"},
            {"messageType": "videoMessage"},
        ]
        texts, files = split_copy_selection(msgs)
        assert [m["messageType"] for m in texts] == ["conversation", "extendedTextMessage"]
        assert [m["messageType"] for m in files] == [
            "documentMessage", "audioMessage", "imageMessage", "videoMessage"]


class TestUniqueName:
    def test_repeated_names_get_a_counter_before_the_extension(self):
        used = set()
        assert [unique_copy_name("a.pdf", used) for _ in range(3)] == [
            "a.pdf", "a_1.pdf", "a_2.pdf"]

    def test_collision_ignores_case(self):
        used = set()
        unique_copy_name("A.PDF", used)
        assert unique_copy_name("a.pdf", used) == "a_1.pdf"

    def test_no_extension(self):
        used = set()
        unique_copy_name("notes", used)
        assert unique_copy_name("notes", used) == "notes_1"


class TestStaging:
    def test_every_file_is_written_with_its_own_name_and_copied_once(self, staged, tmp_path):
        for name in ("m1", "m2"):
            (tmp_path / f"{name}.cache").write_bytes(b"x")
        panel = _Panel()
        panel._stage("", [_msg("m1", "same.pdf"), _msg("m2", "same.pdf")])
        (paths, text), = staged
        assert [os.path.basename(p) for p in paths] == ["same.pdf", "same_1.pdf"]
        assert all(os.path.isfile(p) for p in paths)
        assert text == ""
        assert panel.main_window.announced == ["messages_copied_bulk_files"]

    def test_text_travels_with_the_files(self, staged, tmp_path):
        (tmp_path / "m1.cache").write_bytes(b"x")
        panel = _Panel()
        panel._stage("linha", [_msg("m1", "a.pdf")])
        assert staged[0][1] == "linha"

    def test_missing_media_is_downloaded_once_announced_once(self, staged):
        panel = _Panel()
        panel._stage("", [_msg("m1", "a.pdf"), _msg("m2", "b.pdf")])
        assert len(staged[0][0]) == 2
        assert panel.main_window.announced.count("downloading") == 1

    def test_offline_files_not_on_disk_are_counted_not_announced_each(self, staged, tmp_path):
        (tmp_path / "m1.cache").write_bytes(b"x")
        panel = _Panel(connected=False)
        panel._stage("", [_msg("m1", "a.pdf"), _msg("m2", "b.pdf"), _msg("m3", "c.pdf")])
        assert len(staged[0][0]) == 1
        assert panel.main_window.announced == ["messages_copied_bulk_files_partial"]

    def test_nothing_copied_reports_the_copy_error(self, staged):
        panel = _Panel(downloads=False)
        panel._stage("", [_msg("m1", "a.pdf")])
        assert staged == []
        assert panel.main_window.announced == ["msg_copy_error"]

    def test_text_alone_still_copies_when_every_file_failed(self, staged):
        panel = _Panel(downloads=False)
        panel._stage("texto", [_msg("m1", "a.pdf")])
        assert staged == [([], "texto")]
        assert panel.main_window.announced == ["messages_copied_bulk_files_partial"]

    def test_clipboard_refusal_is_reported(self, staged, monkeypatch, tmp_path):
        (tmp_path / "m1.cache").write_bytes(b"x")
        monkeypatch.setattr(bulk_copy_files, "set_clipboard_files_and_text", lambda p, t: False)
        panel = _Panel()
        panel._stage("", [_msg("m1", "a.pdf")])
        assert panel.main_window.announced == ["msg_copy_error"]
