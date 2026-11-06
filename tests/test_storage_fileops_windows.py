"""Real pinned-handle mutations use CI-owned fixtures, never the local desktop."""
import os

import pytest

from core.storage_manager.fileops import ChangedFile, pinned
from core.storage_manager.scan import scan
from core.storage_manager.types import Root


pytestmark = pytest.mark.skipif(os.name != "nt" or os.environ.get("CI") != "true",
                                reason="native file provider acceptance is CI-only")


def fixture_entry(tmp_path):
    file = tmp_path / "source"
    file.write_bytes(b"fixture")
    return scan([Root("fixture", str(tmp_path), "compile_cache", managed=True)]).entries[0]


def test_native_disposition_removes_the_previewed_identity(tmp_path):
    entry = fixture_entry(tmp_path)
    with pinned(entry) as file:
        file.remove()
    assert not os.path.exists(entry.path)


def test_native_rename_preserves_content_without_overwrite(tmp_path):
    entry = fixture_entry(tmp_path)
    target = tmp_path / "held"
    with pinned(entry) as file:
        file.move(str(target))
    assert target.read_bytes() == b"fixture"
    target.write_bytes(b"new")
    replacement = fixture_entry(tmp_path)
    # Scan includes target too; select the source explicitly.
    replacement = next(e for e in scan([Root("fixture", str(tmp_path))]).entries if e.relative == "source")
    with pinned(replacement) as file:
        with pytest.raises(OSError):
            file.move(str(target))
    assert target.read_bytes() == b"new"


def test_native_provider_rejects_changed_metadata(tmp_path):
    entry = fixture_entry(tmp_path)
    with open(entry.path, "ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ChangedFile):
        with pinned(entry):
            raise AssertionError("changed file was accepted")


def test_native_transaction_and_restore_keep_content_and_download_policy(tmp_path):
    from dataclasses import replace
    from core.media_cleaned import CleanedMediaIndex
    from core.storage_manager.policy import Preferences
    from core.storage_manager.recovery import holdings, finish
    from core.storage_manager.store import StorageStore
    from core.storage_manager.transactions import make_plan, execute
    media = tmp_path / "media"
    media.mkdir()
    (media / "one.wzmedia").write_bytes(b"only CI-owned fixture content")
    roots = [Root("media", str(media), "media", managed=True)]
    snapshot = scan(roots, classify=lambda e: replace(e, reason="received", media_id="one"))
    preferences = Preferences()
    store = StorageStore(str(tmp_path))
    index = CleanedMediaIndex(lambda: str(tmp_path / "cleaned.json"))
    plan = make_plan(snapshot, [snapshot.entries[0].key], "quarantine", preferences)
    result = execute(plan, snapshot, preferences, store, index)
    assert result.completed == 1 and result.failed == 0 and index.contains("one")
    held, = holdings(store, roots)
    finish(held, store, roots, index, preferences, restore=True)
    assert (media / "one.wzmedia").read_bytes() == b"only CI-owned fixture content"
    assert not index.contains("one") and not holdings(store, roots)
