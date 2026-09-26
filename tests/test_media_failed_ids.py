"""Tests for MainWindow._load_media_failed_ids()/_save_media_failed_ids().

This set (message IDs whose media CDN URL previously expired, so
sync_if_media() skips a pointless repeat download) used to be a bare set
with no eviction — persisted to data/media_failed.json and reloaded in
full on every startup, growing forever for an account with a lot of old
media. Every entry is provably dead weight once its message is older than
_MEDIA_MAX_AGE_SECONDS: sync_if_media()'s own age check skips it before
ever consulting this set, so pruning stale entries on load loses nothing.

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so _load_media_failed_ids()/_save_media_failed_ids() are exercised
as plain functions against a small stub — same approach as
tests/test_sender_names.py.
"""

import json
import threading

import pytest

import main as main_module
from main import MainWindow
from tests.god_modules import patch_main_global


class _Stub:
    """Minimal stand-in for MainWindow for the media-failed-ids cache."""

    _MEDIA_MAX_AGE_SECONDS = MainWindow._MEDIA_MAX_AGE_SECONDS
    _load_media_failed_ids = MainWindow._load_media_failed_ids
    _save_media_failed_ids = MainWindow._save_media_failed_ids

    def __init__(self):
        self._media_failed_ids = {}
        self._media_failed_lock = threading.Lock()


@pytest.fixture
def fake_data_path(tmp_path, monkeypatch):
    path = tmp_path / "media_failed.json"

    def _data_path(*parts):
        return str(tmp_path.joinpath(*parts)) if parts else str(tmp_path)

    patch_main_global(monkeypatch, "data_path", _data_path)
    return path


class TestLoadMediaFailedIds:
    def test_missing_file_returns_empty_dict(self, fake_data_path):
        stub = _Stub()
        assert stub._load_media_failed_ids() == {}

    def test_fresh_entries_are_kept(self, fake_data_path):
        import time as time_module
        now = time_module.time()
        fake_data_path.write_text(json.dumps({"MSG1": now, "MSG2": now}))
        stub = _Stub()

        result = stub._load_media_failed_ids()

        assert set(result) == {"MSG1", "MSG2"}

    def test_stale_entries_are_pruned(self, fake_data_path):
        import time as time_module
        now = time_module.time()
        stale_ts = now - MainWindow._MEDIA_MAX_AGE_SECONDS - 1  # just past the cutoff
        fake_data_path.write_text(json.dumps({
            "OLD": stale_ts,
            "NEW": now,
        }))
        stub = _Stub()

        result = stub._load_media_failed_ids()

        assert "OLD" not in result
        assert "NEW" in result

    def test_entry_just_inside_the_age_boundary_is_kept(self, fake_data_path):
        import time as time_module
        now = time_module.time()
        # A few seconds of slack versus the exact cutoff: the load function
        # computes its own "now" a moment after this test does, so an exact
        # zero-slack boundary would flake on timing alone.
        boundary_ts = now - MainWindow._MEDIA_MAX_AGE_SECONDS + 5
        fake_data_path.write_text(json.dumps({"EDGE": boundary_ts}))
        stub = _Stub()

        result = stub._load_media_failed_ids()

        assert "EDGE" in result

    def test_legacy_list_format_is_migrated(self, fake_data_path):
        """Older installs persisted a plain list (the pre-dict format).
        Must not crash, and must not silently drop the skip-hint."""
        fake_data_path.write_text(json.dumps(["OLDFORMAT1", "OLDFORMAT2"]))
        stub = _Stub()

        result = stub._load_media_failed_ids()

        assert set(result) == {"OLDFORMAT1", "OLDFORMAT2"}
        assert all(isinstance(ts, float) for ts in result.values())

    def test_corrupted_file_returns_empty_dict(self, fake_data_path):
        fake_data_path.write_text("not valid json{{{")
        stub = _Stub()
        assert stub._load_media_failed_ids() == {}

    def test_non_numeric_timestamp_entry_is_dropped(self, fake_data_path):
        fake_data_path.write_text(json.dumps({"BAD": "not-a-timestamp"}))
        stub = _Stub()
        assert stub._load_media_failed_ids() == {}


class TestSaveMediaFailedIds:
    def test_round_trips_through_save_and_load(self, fake_data_path):
        import time as time_module
        stub = _Stub()
        stub._media_failed_ids = {"A": time_module.time(), "B": time_module.time()}

        stub._save_media_failed_ids()
        reloaded = stub._load_media_failed_ids()

        assert set(reloaded) == {"A", "B"}

    def test_save_failure_does_not_raise(self, monkeypatch, fake_data_path):
        stub = _Stub()
        stub._media_failed_ids = {"A": 123.0}

        def _broken_open(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr("builtins.open", _broken_open)

        stub._save_media_failed_ids()  # must not raise
