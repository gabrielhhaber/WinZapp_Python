from core.incremental_sync import (
    chat_sync_marker,
    chat_sync_marker_changed,
    classify_chat_sync,
    messages_overlap,
    next_incremental_limit,
)


def _message(mid, ts):
    return {"key": {"id": mid}, "messageTimestamp": ts}


def _chat(*, t=0, last_received="", last_message="", records=()):
    chat = {
        "remoteJid": "5511999999999@s.whatsapp.net",
        "t": t,
        "messages": {"messages": {"records": list(records)}},
    }
    if last_received:
        chat["lastReceivedKey"] = {"id": last_received}
    if last_message:
        chat["lastMessage"] = _message(last_message, t)
    return chat


class TestChatSyncMarker:
    def test_records_server_activity_and_newest_local_message(self):
        marker = chat_sync_marker(
            _chat(
                t=30,
                last_received="SERVER3",
                last_message="SERVER4",
                records=[_message("LOCAL1", 10), _message("LOCAL2", 20)],
            )
        )
        assert marker == {
            "activity": 30,
            "unread_count": 0,
            "last_received_id": "SERVER3",
            "last_message_id": "SERVER4",
            "newest_local_id": "LOCAL2",
            "newest_local_ts": 20,
            "record_count": 2,
        }

    def test_same_snapshot_is_unchanged(self):
        chat = _chat(
            t=30,
            last_received="M2",
            last_message="M2",
            records=[_message("M2", 30)],
        )
        assert chat_sync_marker_changed(chat, chat_sync_marker(chat)) is False

    def test_unread_count_change_requires_incremental_refresh(self):
        old = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        new = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        new["unreadCount"] = 2
        assert chat_sync_marker_changed(new, chat_sync_marker(old)) is True

    def test_newer_activity_requires_incremental_refresh(self):
        old = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        new = _chat(t=31, last_received="M3", records=[_message("M2", 30)])
        assert chat_sync_marker_changed(new, chat_sync_marker(old)) is True

    def test_last_received_change_is_evidence_even_if_timestamp_is_same(self):
        old = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        new = _chat(t=30, last_received="M3", records=[_message("M2", 30)])
        assert chat_sync_marker_changed(new, chat_sync_marker(old)) is True

    def test_last_message_change_catches_other_device_outgoing_message(self):
        old = _chat(
            t=30,
            last_received="IN1",
            last_message="OUT1",
            records=[_message("OUT1", 30)],
        )
        new = _chat(
            t=30,
            last_received="IN1",
            last_message="OUT2",
            records=[_message("OUT1", 30)],
        )
        assert chat_sync_marker_changed(new, chat_sync_marker(old)) is True

    def test_legacy_marker_without_last_message_uses_newest_local_id(self):
        chat = _chat(t=30, last_message="M2", records=[_message("M2", 30)])
        baseline = {
            "activity": 30,
            "last_received_id": "",
            "newest_local_id": "M2",
            "newest_local_ts": 30,
            "record_count": 1,
        }
        assert chat_sync_marker_changed(chat, baseline) is False


class TestChatClassification:
    def test_unchanged_warm_chat_is_skipped(self):
        chat = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        assert classify_chat_sync(chat, chat_sync_marker(chat)) == ("skip", "unchanged")

    def test_changed_warm_chat_is_incremental(self):
        old = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        new = _chat(t=31, last_received="M3", records=[_message("M2", 30)])
        assert classify_chat_sync(new, chat_sync_marker(old)) == (
            "incremental",
            "activity-changed",
        )

    def test_new_chat_gets_full_first_page(self):
        assert classify_chat_sync(_chat(t=30), {}) == ("full", "new-chat")

    def test_missing_local_history_gets_full_first_page(self):
        baseline = chat_sync_marker(_chat(t=30, records=[]))
        assert classify_chat_sync(
            _chat(t=30), baseline, server_claims_content=True
        ) == ("full", "missing-local-history")

    def test_known_gap_is_full_repair(self):
        chat = _chat(t=30, records=[_message("M2", 30)])
        assert classify_chat_sync(
            chat, chat_sync_marker(chat), repair_needed=True
        ) == ("full", "history-repair")

    def test_force_full_overrides_unchanged_cache(self):
        chat = _chat(t=30, records=[_message("M2", 30)])
        assert classify_chat_sync(
            chat, chat_sync_marker(chat), force_full=True
        ) == ("full", "forced-full")


class TestAdaptiveWindow:
    def test_overlap_stops_growth(self):
        assert messages_overlap([_message("M2", 30)], [_message("M2", 30)]) is True
        assert next_incremental_limit(50, 200, 50, True) == 50

    def test_short_server_answer_stops_growth(self):
        assert next_incremental_limit(50, 200, 12, False) == 50

    def test_saturated_disjoint_window_doubles_until_page_size(self):
        assert next_incremental_limit(50, 200, 50, False) == 100
        assert next_incremental_limit(100, 200, 100, False) == 200
        assert next_incremental_limit(200, 200, 200, False) == 200

    def test_disjoint_ids_do_not_overlap(self):
        assert messages_overlap([_message("NEW", 40)], [_message("OLD", 30)]) is False
