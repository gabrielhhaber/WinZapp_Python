"""Account preferences are durable but never exported with private file paths."""
import json
from contextlib import nullcontext

import pytest

from core.storage_manager.policy import Preferences
from core.storage_manager.store import StorageStore


def store(tmp_path, monkeypatch):
    value = StorageStore(str(tmp_path / "account"))
    monkeypatch.setattr(value, "lock", nullcontext)
    return value


def test_reading_missing_preferences_does_not_create_files(tmp_path):
    value = StorageStore(str(tmp_path / "absent"))
    assert not value.preferences().scan_on_start
    assert not (tmp_path / "absent").exists()


def test_settings_roundtrip_is_account_local(tmp_path, monkeypatch):
    value = store(tmp_path, monkeypatch)
    value.save_preferences(Preferences(warning_bytes=123, protected={"private-path"}))
    assert value.preferences().warning_bytes == 123
    assert value.preferences().protected == {"private-path"}
    assert list(tmp_path.rglob("app.json")) == []


@pytest.mark.parametrize("raw", ["broken", "[]", '{"version":2}', '{"version":true}'])
def test_bad_or_future_store_starts_manual(tmp_path, monkeypatch, raw):
    value = store(tmp_path, monkeypatch)
    (tmp_path / "account").mkdir()
    (tmp_path / "account" / "storage_manager.json").write_text(raw, encoding="utf-8")
    assert not value.preferences().rules and not value.preferences().scan_on_start


def test_private_paths_are_not_stored_in_history(tmp_path, monkeypatch):
    value = store(tmp_path, monkeypatch)
    value.record(100000, 3, completed=2, bytes_removed=12)
    rows = value.history(100100, 3)
    assert rows == [dict(time=100000, completed=2, skipped=0, failed=0, bytes_removed=12)]
    assert value.history(500000, 3) == []
    value.clear_history()
    assert value.history(100100, 3) == []


def test_write_failure_does_not_destroy_previous_preferences(tmp_path, monkeypatch):
    value = store(tmp_path, monkeypatch)
    value.save_preferences(Preferences(warning_bytes=12))
    def denied(*args):
        raise OSError("fixture failure")
    monkeypatch.setattr("core.storage_manager.store.os.replace", denied)
    with pytest.raises(OSError):
        value.save_preferences(Preferences(warning_bytes=99))
    assert value.preferences().warning_bytes == 12


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_every_published_payload_fits_the_exact_reader_character_bound(tmp_path, monkeypatch, delta):
    import json
    from pathlib import Path
    from core.storage_manager.store import MAX_STORE_CHARS, PreferencesTooLarge
    value = store(tmp_path, monkeypatch)
    value.save_preferences(Preferences(warning_bytes=12))
    before = Path(value.path).read_bytes()
    raw = {"version": 1, "padding": ""}
    overhead = len(json.dumps(raw, ensure_ascii=False))
    raw["padding"] = "x" * (MAX_STORE_CHARS - overhead + delta)
    if delta > 0:
        with pytest.raises(PreferencesTooLarge): value._write(raw)
        assert Path(value.path).read_bytes() == before
    else:
        value._write(raw)
        assert len(Path(value.path).read_text(encoding="utf-8")) == MAX_STORE_CHARS + delta
        assert value._read(strict=True)["padding"] == raw["padding"]


def test_large_keep_list_is_refused_without_losing_a_readable_saved_value(tmp_path, monkeypatch):
    from pathlib import Path
    from core.storage_manager.store import PreferencesTooLarge
    value = store(tmp_path, monkeypatch)
    value.save_preferences(Preferences(warning_bytes=12))
    before = Path(value.path).read_bytes()
    with pytest.raises(PreferencesTooLarge):
        value.save_preferences(Preferences(protected={str(n) + "/" + "x" * 180 for n in range(12000)}))
    assert value.preferences(strict=True).warning_bytes == 12
    assert Path(value.path).read_bytes() == before


def test_history_cannot_push_a_near_limit_store_past_its_reader_bound(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from core.storage_manager.store import MAX_STORE_CHARS, PreferencesTooLarge
    value = store(tmp_path, monkeypatch)
    raw = {"version": 1, "preferences": Preferences().as_dict(), "history": [], "padding": ""}
    raw["padding"] = "x" * (MAX_STORE_CHARS - len(json.dumps(raw, ensure_ascii=False)))
    value._write(raw)
    before = Path(value.path).read_bytes()
    with pytest.raises(PreferencesTooLarge): value.record(10000, 1, completed=1)
    assert Path(value.path).read_bytes() == before and value.history(10000, 1) == []
