"""Storage scans are bounded metadata snapshots, never implicit maintenance."""
import os
import stat
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.storage_manager.scan import scan
from core.storage_manager.types import Root, unsafe


def put(folder, name, size=3):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_nested_roots_count_once_and_most_specific_category_wins(tmp_path):
    put(tmp_path, "app.exe", 7)
    put(tmp_path, "data/media/a.wzmedia", 11)
    result = scan([Root("program", str(tmp_path), "program"),
                   Root("media", str(tmp_path / "data/media"), "media")])
    assert result.complete and result.total_bytes == 18
    assert result.totals() == {"media": (1, 11), "program": (1, 7)}


def test_duplicate_root_is_not_counted_twice(tmp_path):
    put(tmp_path, "a", 13)
    result = scan([Root("one", str(tmp_path)), Root("two", str(tmp_path / "."))])
    assert result.total_bytes == 13 and len(result.entries) == 1


def test_optional_missing_root_is_not_an_error(tmp_path):
    result = scan([Root("missing", str(tmp_path / "absent"))])
    assert result.complete and result.total_bytes == 0


def test_read_failure_is_not_zero_byte_success(tmp_path, monkeypatch):
    def denied(path):
        raise PermissionError()
    monkeypatch.setattr("core.storage_manager.scan.os.scandir", denied)
    result = scan([Root("root", str(tmp_path))])
    assert not result.complete and result.issues == {"unreadable": 1}


def test_cancelled_scan_does_not_report_complete(tmp_path):
    put(tmp_path, "a")
    result = scan([Root("root", str(tmp_path))], cancel=lambda: True)
    assert result.cancelled and not result.complete


def test_file_limit_bounds_snapshot_and_marks_partial(tmp_path):
    for n in range(5):
        put(tmp_path, str(n))
    result = scan([Root("root", str(tmp_path))], max_entries=2)
    assert len(result.entries) == 2 and result.issues == {"limit": 1}


def test_directory_only_tree_is_also_bounded(tmp_path):
    for n in range(8):
        (tmp_path / str(n)).mkdir()
    result = scan([Root("root", str(tmp_path))], max_nodes=3)
    assert not result.complete and result.issues == {"limit": 1}


def test_excluded_subtree_is_not_traversed(tmp_path):
    put(tmp_path, "visible", 5)
    secret = put(tmp_path, "excluded/file", 20)
    result = scan([Root("root", str(tmp_path), exclude=(str(secret.parent),))])
    assert result.complete and result.total_bytes == 5


def test_metadata_scan_does_not_open_content(tmp_path, monkeypatch):
    path = put(tmp_path, "session.db", 17)
    def forbidden(*args, **kwargs):
        raise AssertionError("metadata scan tried to open content")
    monkeypatch.setattr("builtins.open", forbidden)
    result = scan([Root("root", str(tmp_path), "sessions")])
    assert result.total_bytes == 17 and path.exists()


def test_file_identity_and_timestamp_are_carried_to_plan(tmp_path):
    path = put(tmp_path, "a", 19)
    entry, = scan([Root("root", str(tmp_path))]).entries
    info = path.stat()
    assert entry.identity == (info.st_dev, info.st_ino, 19, info.st_mtime_ns, info.st_ctime_ns)


def test_hard_links_count_one_identity(tmp_path):
    original = put(tmp_path, "original", 23)
    try:
        os.link(original, tmp_path / "alias")
    except OSError:
        pytest.skip("hard links unavailable on this fixture volume")
    result = scan([Root("root", str(tmp_path))])
    assert result.total_bytes == 23 and result.duplicates == 1


@pytest.mark.parametrize("attributes", [0x400, 0x1000, 0x40000, 0x400000])
def test_reparse_and_cloud_recall_attributes_are_not_followed(attributes):
    assert unsafe(SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=attributes))


def test_symlink_mode_is_not_safe_even_without_windows_attributes():
    assert unsafe(SimpleNamespace(st_mode=stat.S_IFLNK))


def test_classification_is_injected_without_a_gui_or_database(tmp_path):
    put(tmp_path, "a")
    result = scan([Root("root", str(tmp_path))],
                  classify=lambda e: replace(e, reason="received", media_id="known"))
    assert result.entries[0].reason == "received"


def test_an_unavailable_optional_owner_does_not_erase_other_measured_categories(tmp_path):
    put(tmp_path, "a", 12)
    result = scan([Root("known", str(tmp_path)),
                   Root("unavailable", "", "models", error="owner_unavailable")])
    assert result.total_bytes == 12 and result.issues == {"owner_unavailable": 1}
