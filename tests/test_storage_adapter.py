"""Storage resolves owner paths and visible chats without opening a session."""
from types import SimpleNamespace

import pytest

from core.storage_manager.model_roots import external_model_files
from core.storage_manager.types import Entry, Root
from main_window.storage_management import StorageContext


def context(tmp_path, monkeypatch):
    monkeypatch.setattr("main_window.storage_management.app_paths.data_path", lambda: str(tmp_path))
    window = SimpleNamespace(is_chat_locked=lambda jid: jid == "locked",
                             is_chat_hidden_by_vault=lambda jid: jid == "hidden")
    return StorageContext(window), window


def test_chat_capture_includes_only_allowed_names_and_never_a_raw_jid(tmp_path, monkeypatch):
    value, window = context(tmp_path, monkeypatch)
    window.conversations_panel = SimpleNamespace(_all_chats_list=[{"remoteJid": j} for j in
        ("allowed", "locked", "hidden", "raw@s.whatsapp.net")],
        _all_chat_names=["Visible", "Locked", "Hidden", "raw@s.whatsapp.net"])
    value.capture_chats()
    assert value.chats == {"allowed": "Visible", "raw@s.whatsapp.net": ""}


def test_new_lock_and_busy_queue_are_conservative_without_waiting(tmp_path, monkeypatch):
    value, window = context(tmp_path, monkeypatch)
    entry = Entry(Root("media", str(tmp_path), "media", managed=True), "one.wzmedia", 1, 1,
                  (1, 2, 1, 3, 4), "media", media_id="one")
    window._background_transcription = object()
    assert value.busy(entry)
    window._background_transcription = None
    attempts = []
    class Lock:
        def acquire(self, **kw):
            attempts.append(kw)
            return False
    window.message_queue = SimpleNamespace(_lock=Lock())
    assert value.busy(entry) and attempts == [{"blocking": False}]


def test_external_model_layout_uses_catalogue_files_not_arbitrary_directories(tmp_path):
    configured = str(tmp_path / "models")
    app = SimpleNamespace(get_strict=lambda key: configured if key == "transcription_models_dir" else None)
    paths = external_model_files(app)
    assert paths and configured not in paths
    assert all(p.startswith(configured) for p in paths)
    assert any(p.endswith(".part") for p in paths)


def test_unknown_private_preference_schema_blocks_mutation(tmp_path):
    import json
    from core.storage_manager.store import StorageStore
    store = StorageStore(str(tmp_path))
    with open(store.path, "w", encoding="utf-8") as stream:
        json.dump({"version": 1, "preferences": {"version": 2}}, stream)
    assert not store.preferences().rules
    with pytest.raises(ValueError):
        store.preferences(strict=True)
