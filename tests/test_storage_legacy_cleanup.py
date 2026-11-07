"""Both cleanup entry points honor keep-lists and durable download blocks."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core.media_cleanup import CleanupCandidate, CleanupPreview
from core.media_cleaned import CleanedMediaIndex
from core.storage_manager.legacy_cleanup import execute
from core.storage_manager.policy import Preferences
from core.storage_manager.references import chat_token
from core.storage_manager.types import canonical


@pytest.mark.parametrize("protection", ["file", "chat", "category"])
def test_legacy_cleaner_keeps_protected_files_and_holds_locks_through_indexing(tmp_path, protection):
    media, voice = tmp_path / "media", tmp_path / "voice"
    media.mkdir(); voice.mkdir()
    one, two = media / "one.wzmedia", voice / "two.msv"
    one.write_bytes(b"keep"); two.write_bytes(b"remove")
    prefs, shared, events = Preferences(), Preferences(), []
    if protection == "file": shared.protected.add(canonical(one))
    if protection == "chat": prefs.protected_chats.add(chat_token("keep-chat"))
    if protection == "category": shared.protected_categories.add("media")
    @contextmanager
    def lock(name):
        events.append("enter " + name)
        yield
        events.append("exit " + name)
    own_store = SimpleNamespace(lock=lambda: lock("own"), preferences=lambda **kw: prefs)
    shared_store = SimpleNamespace(lock=lambda: lock("shared"), preferences=lambda **kw: shared)
    preview = CleanupPreview([CleanupCandidate("keep-chat", "one", "photos", 4, str(one)),
                              CleanupCandidate("other-chat", "two", "audios", 6, str(two))])
    index = CleanedMediaIndex(lambda: str(tmp_path / "cleaned.json"))
    original = index.update_durable
    def update(*args, **kwargs):
        assert events == ["enter shared", "enter own"]
        return original(*args, **kwargs)
    index.update_durable = update
    stats = execute(preview, [str(media), str(voice)], own_store, shared_store, index, lambda key: key)
    assert stats.files_deleted == 1 and stats.files_protected == 1
    assert one.exists() and not two.exists()
    assert index.contains("two") and not index.contains("one")
    assert events == ["enter shared", "enter own", "exit own", "exit shared"]


def test_unreadable_protection_settings_stop_the_old_cleaner_before_deletion(tmp_path):
    from contextlib import nullcontext
    def invalid(**kwargs): raise ValueError("future preferences")
    store = SimpleNamespace(lock=nullcontext, preferences=invalid)
    called = []
    with pytest.raises(ValueError):
        execute(CleanupPreview(), [], store, store,
                SimpleNamespace(update_durable=lambda *a: called.append(1)), lambda key: key)
    assert not called
