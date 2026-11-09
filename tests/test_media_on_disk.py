"""The media sweep counts only what is left to download (a user's
"723 de 728 processadas (66 baixadas)" when 659 were already on disk)."""
import os

from core.media_on_disk import already_stored, stored_id, stored_path


def path(*parts):
    return os.path.join("DATA", *parts)


def msg(mid):
    return {"key": {"id": mid}}


def test_files_are_named_as_the_downloads_name_them():
    assert stored_id("3EB0ABC") == "3EB0ABC"
    assert stored_id("false_123@g.us_3EB0ABC_456@lid") == "3EB0ABC"
    assert stored_path(msg("A1"), "imageMessage", path) == os.path.join("DATA", "media", "A1.wzmedia")
    assert stored_path(msg("A2"), "audioMessage", path) == os.path.join("DATA", "voice_messages", "A2.msv")
    assert stored_path(msg(""), "imageMessage", path) == ""


def test_only_a_file_on_disk_counts_as_stored():
    on_disk = {os.path.join("DATA", "media", "A1.wzmedia")}
    assert already_stored(msg("A1"), "videoMessage", path, on_disk.__contains__)
    assert not already_stored(msg("A1"), "audioMessage", path, on_disk.__contains__)
    assert not already_stored(msg(""), "videoMessage", path, on_disk.__contains__)


def test_no_account_folder_means_not_stored():
    def no_account(*parts):
        raise RuntimeError("no active account")
    assert not already_stored(msg("A1"), "imageMessage", no_account)
