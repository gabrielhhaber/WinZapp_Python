import phone_chat_delete as pcd


def test_a_live_deletion_with_nothing_newer_deletes_the_chat():
    assert pcd.applies(False, 1000, 900) is True
    assert pcd.applies(False, 1000, None) is True


def test_a_replayed_deletion_from_a_full_sync_is_ignored():
    assert pcd.applies(True, 1000, 900) is False


def test_a_message_after_the_deletion_keeps_the_chat():
    assert pcd.applies(False, 1000, 1006) is False
    assert pcd.applies(False, 1000, 1005) is True


def test_an_unknown_deletion_time_deletes_the_chat():
    assert pcd.applies(False, 0, 5000) is True
