"""Partial or protected histories cannot make cached media expendable."""
from dataclasses import replace

from core.storage_manager.references import chat_token, collect
from core.storage_manager.types import Entry, Root, canonical


def message(name, **extra):
    return dict(key={"id": name, "fromMe": False}, messageType="imageMessage", **extra)


def gather(rows, *, count=None, protected=lambda m: False, max_messages=100000):
    return collect(["private-jid"], lambda jid: len(rows) if count is None else count,
                   lambda jid, size, offset: rows[offset:offset + size],
                   lambda kind, name: "/cache/" + name,
                   lambda name: name, protected=protected, max_messages=max_messages)


def entry(name="a"):
    return Entry(Root("media", "/cache", "media", managed=True), name, 10, 1, (1, 2, 3, 4, 5), "media")


def test_complete_received_reference_can_be_selected():
    result = gather([message("a")])
    value = result.classify(entry())
    assert result.complete and value.reason == "received" and value.media_id == "a"
    assert value.chat_key == chat_token("private-jid") and "private-jid" not in value.chat_key


def test_a_missing_or_decryption_filtered_row_disables_cleanup():
    result = gather([message("a")], count=2)
    assert not result.complete and result.classify(entry()).reason == "unmatched"


def test_backend_specific_database_error_retains_protection():
    import sqlite3
    def unavailable(*args): raise sqlite3.OperationalError("fixture is busy")
    result = collect(["private-jid"], unavailable, unavailable, lambda *a: "/cache/a", lambda name: name)
    assert not result.complete and result.classify(entry()).reason == "unmatched"


def test_changing_message_count_invalidates_reference_proof():
    counts = iter([1, 2])
    result = collect(["chat"], lambda j: next(counts), lambda *args: [message("a")],
                     lambda *args: "/cache/a", lambda name: name)
    assert not result.complete


def test_a_pending_or_outgoing_file_keeps_its_only_copy():
    for item in (message("a", _local_pending=True),
                 dict(message("a"), key={"id": "a", "fromMe": True})):
        assert gather([item]).classify(entry()).reason == "important"


def test_same_cache_id_with_a_protected_reference_cannot_be_cleaned():
    result = gather([message("a"), message("a", _send_failed=True)])
    assert result.classify(entry()).reason == "important"


def test_incomplete_budget_cannot_turn_a_partial_map_into_eligibility():
    result = gather([message("a")], max_messages=0)
    assert not result.complete and result.classify(entry()).reason == "unmatched"


def test_other_account_entries_remain_information_only():
    result = gather([message("a")])
    value = entry()
    value = replace(value, root=replace(value.root, scope="other_accounts"))
    assert result.classify(value).reason == "unmatched"


def test_large_history_is_read_in_pages_without_the_old_20000_truncation():
    rows = [message(str(i)) for i in range(20003)]
    result = gather(rows)
    assert result.complete and len(result.files) == 20003


def test_cancelled_history_is_not_complete():
    result = collect(["chat"], lambda j: 1, lambda *a: [message("a")],
                     lambda *a: "/cache/a", lambda x: x, cancel=lambda: True)
    assert not result.complete
