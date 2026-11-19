from core.pending_timestamp import pending_send_timestamp


def test_pc_clock_behind_server_never_sorts_reply_above_original():
    original = {"key": {"id": "A"}, "messageTimestamp": 1_000_050}
    assert pending_send_timestamp(1_000_000, [original], original) == 1_000_050


def test_pc_clock_ahead_is_kept():
    assert pending_send_timestamp(2_000_000, [{"messageTimestamp": 1_000_000}]) == 2_000_000


def test_rows_still_pending_and_separators_are_ignored():
    rows = [{"_local_id": "x", "messageTimestamp": 9_999_999},
            {"_local_pending": True, "messageTimestamp": 9_999_999}, "sep", None]
    assert pending_send_timestamp(1_000, rows) == 1_000


def test_millisecond_stamps_are_read_as_seconds():
    assert pending_send_timestamp(10, [{"messageTimestamp": 1_700_000_000_000}]) == 1_700_000_000
