"""Confirmed storage plans cannot expand or lose durable media suppression."""
from contextlib import contextmanager, nullcontext
from dataclasses import replace
import os

import pytest

from core.media_cleaned import CleanedMediaIndex
from core.storage_manager import fileops
from core.storage_manager.policy import Preferences
from core.storage_manager.scan import scan
from core.storage_manager.store import StorageStore
from core.storage_manager.transactions import execute, make_plan
from core.storage_manager.types import Root, fingerprint


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    account = tmp_path / "account"
    media = account / "media"
    media.mkdir(parents=True)
    (media / "one.wzmedia").write_bytes(b"encrypted fixture")
    snapshot = scan([Root("media", str(media), "media", managed=True)],
                    classify=lambda e: replace(e, media_id="one", reason="received"))
    prefs = Preferences()
    store = StorageStore(str(account))
    monkeypatch.setattr(store, "lock", nullcontext)
    monkeypatch.setattr(fileops, "pinned_directory", lambda path: nullcontext())
    index = CleanedMediaIndex(lambda: str(account / "media_cleaned.json"))

    @contextmanager
    def provider(entry):
        if fingerprint(os.lstat(entry.path)) != entry.identity:
            raise fileops.ChangedFile()
        class Held:
            def move(self, destination):
                os.rename(entry.path, destination)
            def remove(self):
                os.unlink(entry.path)
        yield Held()

    return snapshot, prefs, store, index, provider


def run(tx, method="remove", **kwargs):
    snapshot, prefs, store, index, provider = tx
    plan = make_plan(snapshot, {e.key for e in snapshot.entries}, method, prefs)
    return execute(plan, snapshot, prefs, store, index, provider=provider, **kwargs), plan


def test_remove_is_local_and_blocks_only_the_deleted_media(transaction):
    result, _ = run(transaction)
    snapshot, _, _, index, _ = transaction
    assert result.completed == 1 and result.bytes_removed == len(b"encrypted fixture")
    assert not os.path.exists(snapshot.entries[0].path)
    assert index.contains("one") and not index.contains("other")


def test_quarantine_does_not_claim_immediate_freed_space(transaction):
    result, _ = run(transaction, "quarantine")
    assert result.completed == 1 and result.bytes_removed == 0
    assert result.bytes_held == len(b"encrypted fixture")


def test_expired_preview_cannot_be_replayed_after_token_retirement(transaction):
    import time
    snapshot, prefs, store, index, provider = transaction
    plan = make_plan(snapshot, [snapshot.entries[0].key], "remove", prefs)
    with pytest.raises(ValueError):
        execute(replace(plan, created=time.time() - 90000), snapshot, prefs, store, index, provider=provider)
    assert os.path.isfile(snapshot.entries[0].path) and not index.contains("one")


def test_oversized_batch_is_refused_instead_of_silently_truncated(transaction):
    from core.storage_manager.transactions import PlanTooLarge
    snapshot, prefs, *_ = transaction
    original = snapshot.entries[0]
    snapshot.entries = [replace(original, relative=str(n)) for n in range(501)]
    with pytest.raises(PlanTooLarge):
        make_plan(snapshot, [e.key for e in snapshot.entries], "remove", prefs)


def test_late_file_is_never_added_to_the_plan(transaction):
    snapshot, prefs, store, index, provider = transaction
    plan = make_plan(snapshot, [snapshot.entries[0].key], "remove", prefs)
    late = os.path.join(snapshot.entries[0].root.path, "late.wzmedia")
    with open(late, "wb") as stream:
        stream.write(b"keep")
    execute(plan, snapshot, prefs, store, index, provider=provider)
    assert os.path.isfile(late)


def test_changed_file_is_not_deleted_and_its_new_block_is_rolled_back(transaction):
    entry = transaction[0].entries[0]
    with open(entry.path, "ab") as stream:
        stream.write(b"new data")
    result, _ = run(transaction)
    assert result.failed == 1 and os.path.exists(entry.path)
    assert not transaction[3].contains("one")


def test_existing_cleanup_block_is_not_undone_by_a_failed_plan(transaction):
    transaction[3].update_durable(["one"])
    with open(transaction[0].entries[0].path, "ab") as stream:
        stream.write(b"changed")
    run(transaction)
    assert transaction[3].contains("one")


def test_protection_added_after_preview_wins(transaction):
    snapshot, prefs, store, index, provider = transaction
    plan = make_plan(snapshot, [snapshot.entries[0].key], "remove", prefs)
    prefs.protected.add(snapshot.entries[0].key)
    result = execute(plan, snapshot, prefs, store, index, provider=provider)
    assert result.skipped == 1 and os.path.isfile(snapshot.entries[0].path)


def test_busy_after_preview_is_skipped(transaction):
    result, _ = run(transaction, busy=lambda e: True)
    assert result.skipped == 1 and result.completed == 0


def test_cancel_does_not_mean_undo(transaction):
    result, _ = run(transaction, cancel=lambda: True)
    assert result.cancelled and result.completed == 0
    assert os.path.isfile(transaction[0].entries[0].path)


def test_replaying_a_consumed_plan_is_rejected(transaction):
    _, plan = run(transaction)
    snapshot, prefs, store, index, provider = transaction
    with pytest.raises(ValueError, match="consumed"):
        execute(plan, snapshot, prefs, store, index, provider=provider)


def test_snapshot_change_invalidates_confirmation(transaction):
    snapshot, prefs, store, index, provider = transaction
    plan = make_plan(snapshot, [snapshot.entries[0].key], "remove", prefs)
    snapshot.id = "new generation"
    with pytest.raises(ValueError, match="stale"):
        execute(plan, snapshot, prefs, store, index, provider=provider)


def test_failed_index_persistence_prevents_file_mutation(transaction, monkeypatch):
    index = transaction[3]
    monkeypatch.setattr(index, "_save", lambda: False)
    result, _ = run(transaction)
    assert result.failed == 1 and os.path.isfile(transaction[0].entries[0].path)


def test_malformed_index_is_not_replaced_by_an_empty_set(transaction):
    store = transaction[2]
    path = os.path.join(store.directory, "media_cleaned.json")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("broken index")
    result, _ = run(transaction)
    assert result.failed == 1 and os.path.isfile(transaction[0].entries[0].path)
    with open(path, encoding="utf-8") as stream:
        assert stream.read() == "broken index"


def test_plan_identity_cannot_be_a_path(transaction):
    snapshot, prefs, store, index, provider = transaction
    plan = make_plan(snapshot, [snapshot.entries[0].key], "remove", prefs)
    with pytest.raises(ValueError, match="identity"):
        execute(replace(plan, id="../../outside"), snapshot, prefs, store, index, provider=provider)
