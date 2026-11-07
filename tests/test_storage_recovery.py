"""Holding manifests cannot restore outside their current roots or overwrite files."""
from contextlib import contextmanager, nullcontext
from dataclasses import replace
import json
import os

import pytest

from core.media_cleaned import CleanedMediaIndex
from core.storage_manager import fileops
from core.storage_manager.policy import Preferences
from core.storage_manager.recovery import finish, holdings
from core.storage_manager.scan import scan
from core.storage_manager.store import StorageStore
from core.storage_manager.transactions import execute, make_plan
from core.storage_manager.types import Root, fingerprint


@pytest.fixture
def held(tmp_path, monkeypatch):
    account = tmp_path / "account"
    media = account / "media"
    media.mkdir(parents=True)
    (media / "one.wzmedia").write_bytes(b"encrypted fixture")
    root = Root("media", str(media), "media", managed=True)
    snapshot = scan([root], classify=lambda e: replace(e, reason="received", media_id="one"))
    store = StorageStore(str(account))
    monkeypatch.setattr(store, "lock", nullcontext)
    monkeypatch.setattr(fileops, "pinned_directory", lambda path: nullcontext())
    index = CleanedMediaIndex(lambda: str(account / "media_cleaned.json"))

    @contextmanager
    def provider(entry):
        if fingerprint(os.lstat(entry.path)) != entry.identity:
            raise fileops.ChangedFile()
        class Pinned:
            def move(self, destination):
                if os.path.lexists(destination):
                    raise FileExistsError()
                os.rename(entry.path, destination)
            def remove(self):
                os.unlink(entry.path)
        yield Pinned()

    prefs = Preferences()
    plan = make_plan(snapshot, [snapshot.entries[0].key], "quarantine", prefs)
    execute(plan, snapshot, prefs, store, index, provider=provider)
    return store, [root], index, prefs, provider


def test_undo_restores_content_and_only_its_new_block(held):
    store, roots, index, prefs, provider = held
    index.update_durable(["unrelated"])
    item, = holdings(store, roots)
    assert finish(item, store, roots, index, prefs, restore=True, provider=provider) == len(b"encrypted fixture")
    with open(item.original.path, "rb") as stream:
        assert stream.read() == b"encrypted fixture"
    assert not index.contains("one") and index.contains("unrelated")
    assert holdings(store, roots) == []


def test_restore_never_overwrites_new_content(held):
    store, roots, index, prefs, provider = held
    item, = holdings(store, roots)
    with open(item.original.path, "wb") as stream:
        stream.write(b"new current file")
    with pytest.raises(FileExistsError):
        finish(item, store, roots, index, prefs, restore=True, provider=provider)
    with open(item.original.path, "rb") as stream:
        assert stream.read() == b"new current file"


def test_permanent_purge_keeps_the_automatic_download_block(held):
    store, roots, index, prefs, provider = held
    item, = holdings(store, roots)
    finish(item, store, roots, index, prefs, restore=False, provider=provider)
    assert index.contains("one") and holdings(store, roots) == []


def test_changed_root_cannot_reuse_an_old_holding_manifest(held):
    store, roots, *_ = held
    assert holdings(store, [replace(roots[0], path=roots[0].path + "-new")]) == []


def test_parent_traversal_in_manifest_is_not_a_restore_target(held):
    store, roots, *_ = held
    item, = holdings(store, roots)
    with open(item.journal, encoding="utf-8") as stream:
        raw = json.load(stream)
    raw["items"][0]["relative"] = "../../outside"
    with open(item.journal, "w", encoding="utf-8") as stream:
        json.dump(raw, stream)
    assert holdings(store, roots) == []


def test_pending_rename_is_recoverable_after_a_crash(held):
    store, roots, *_ = held
    item, = holdings(store, roots)
    with open(item.journal, encoding="utf-8") as stream:
        raw = json.load(stream)
    raw["items"][0]["state"] = "pending"
    with open(item.journal, "w", encoding="utf-8") as stream:
        json.dump(raw, stream)
    assert len(holdings(store, roots)) == 1


def test_new_user_protection_prevents_purge_too(held):
    store, roots, index, prefs, provider = held
    item, = holdings(store, roots)
    prefs.protected.add(item.original.key)
    with pytest.raises(fileops.ChangedFile):
        finish(item, store, roots, index, prefs, restore=False, provider=provider)
    assert os.path.isfile(item.entry.path)


def test_journal_reference_cannot_point_outside_the_private_holding_directory(held):
    store, roots, index, prefs, provider = held
    item, = holdings(store, roots)
    with pytest.raises(ValueError):
        finish(replace(item, journal=os.path.join(store.directory, "outside.json")),
               store, roots, index, prefs, restore=False, provider=provider)


def test_holding_page_exposes_continuation_and_does_not_repeat_an_item(held):
    store, roots, *_ = held
    first = holdings(store, roots, limit=1)
    assert len(first) == 1 and first.next_cursor is not None
    assert holdings(store, roots, limit=1, after=first.next_cursor) == []


def test_unrecognizable_held_item_makes_coverage_explicitly_partial(held):
    store, roots, *_ = held
    item, = holdings(store, roots)
    with open(item.journal, encoding="utf-8") as stream:
        raw = json.load(stream)
    raw["created"] = 10**20
    with open(item.journal, "w", encoding="utf-8") as stream:
        json.dump(raw, stream)
    result = holdings(store, roots)
    assert result == [] and result.partial


def test_retirement_only_removes_expired_empty_tokens_never_held_content(held):
    import time
    from core.storage_manager.journals import retire
    store, roots, index, prefs, provider = held
    item, = holdings(store, roots)
    assert retire(store, provider=provider, now=time.time() + 300000)[0] == 0
    assert os.path.isfile(item.entry.path)
    finish(item, store, roots, index, prefs, restore=False, provider=provider)
    assert retire(store, provider=provider)[0] == 0
    assert retire(store, provider=provider, now=time.time() + 300000)[0] == 1
    assert not os.path.exists(item.journal)


def test_holding_that_becomes_busy_after_pinning_is_not_mutated(held):
    store, roots, index, prefs, provider = held
    item, = holdings(store, roots)
    checks = []
    def busy(entry):
        checks.append(entry)
        return len(checks) > 1
    with pytest.raises(fileops.ChangedFile):
        finish(item, store, roots, index, prefs, restore=False, provider=provider, busy=busy)
    assert os.path.isfile(item.entry.path) and index.contains("one")
    assert len(holdings(store, roots)) == 1
