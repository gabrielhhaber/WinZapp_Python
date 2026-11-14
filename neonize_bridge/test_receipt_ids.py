"""Receipts that name no message are dropped before any work (receipt_ids.py)."""
import receipt_ids


def test_only_real_ids_are_kept():
    assert receipt_ids.message_ids(["", "3EB0AAA", "", "3EB0BBB"]) == ["3EB0AAA", "3EB0BBB"]
    assert receipt_ids.message_ids([""]) == [] and receipt_ids.message_ids(None) == []


def test_the_drops_are_logged_once_and_then_every_thousand():
    skipped = receipt_ids.Skipped()
    logged = [n for n in range(1, 2501) if skipped.note()]
    assert logged == [1, 1000, 2000] and skipped.count == 2500
