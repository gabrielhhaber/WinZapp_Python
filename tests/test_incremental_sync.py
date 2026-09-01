from core.incremental_sync import (
    chat_sync_marker,
    chat_sync_marker_changed,
    classify_chat_sync,
    local_history_behind_server,
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


class TestTheMarkerCannotOutrankTheContent:
    """The exact false negative behind "only F5 brings that chat up to date".

    A round can commit a chat's new activity marker without ever storing the
    message it belongs to — sync_chat_messages() does it deliberately once a
    delta has come back empty too often. The next baseline is a copy of that
    committed marker, so every snapshot-vs-snapshot signal agrees the chat is
    unchanged, for ever. Measured on a real account: 20 of 155 chats were in
    exactly this state, all of them skipped every round.
    """

    def _poisoned(self):
        # t says 14:00, the newest message we actually stored is from 13:00,
        # and the baseline already carries the same claim.
        chat = _chat(t=1400, last_received="M2", records=[_message("M2", 1300)])
        baseline = chat_sync_marker(chat)
        return chat, baseline

    def test_the_old_snapshot_signals_all_agree_it_is_unchanged(self):
        chat, baseline = self._poisoned()
        assert baseline["activity"] == 1400
        assert baseline["newest_local_id"] == "M2"
        assert baseline["last_received_id"] == "M2"

    def test_a_chat_whose_stored_history_lags_its_marker_is_refreshed(self):
        chat, baseline = self._poisoned()
        assert classify_chat_sync(chat, baseline) == (
            "incremental", "local-behind-server")

    def test_a_confirmed_fetch_stops_it_asking_again(self):
        """Otherwise a chat whose newest event never becomes a stored message
        (a filtered protocol row) would be re-queried on every round."""
        chat, baseline = self._poisoned()
        assert classify_chat_sync(
            chat, baseline, verified_activity=1400) == ("skip", "unchanged")

    def test_a_chat_that_really_is_up_to_date_is_still_skipped(self):
        """The guard against turning the plan into a full sync in disguise."""
        chat = _chat(t=1400, last_received="M2", records=[_message("M2", 1400)])
        assert classify_chat_sync(chat, chat_sync_marker(chat)) == (
            "skip", "unchanged")

    def test_millisecond_timestamps_do_not_read_as_newer_than_the_server(self):
        chat = _chat(t=1_400_000_000, last_received="M2",
                     records=[_message("M2", 1_400_000_000_000)])
        assert local_history_behind_server(chat) is False

    def test_an_empty_local_cache_is_left_to_the_full_path(self):
        assert local_history_behind_server(_chat(t=1400)) is False


class TestTheReasonNamesTheSignal:
    """The plan's log line reported every incremental target as
    "activity-changed" whatever had moved, which made a planner bug
    indistinguishable from a genuinely quiet account."""

    def test_an_unread_change_is_not_called_activity(self):
        old = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        new = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        new["unreadCount"] = 2
        assert classify_chat_sync(new, chat_sync_marker(old)) == (
            "incremental", "unread-changed")

    def test_a_new_last_received_id_says_so(self):
        old = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        new = _chat(t=30, last_received="M3", records=[_message("M2", 30)])
        assert classify_chat_sync(new, chat_sync_marker(old)) == (
            "incremental", "last-received-changed")


class TestActivityMovingBackwards:
    """A baseline above the current activity must NOT count as a change.

    The baseline is not "what the server last said": sync_chat_messages()
    raises chat["t"] to the newest displayable message whenever that is newer
    than the server's marker, and on_historical_message() can write a raw
    millisecond timestamp into it. Once local `t` sits above the server's, a
    backwards-counts-too rule re-selects that chat every 60s poll round for
    ever — and the fetch it triggers raises `t` again, re-arming the next
    round. Nothing useful comes back either way: sync_chat_messages()
    deliberately never deletes, so a revoke is not actionable from a delta.
    """

    def test_a_lower_server_activity_is_not_a_reason_to_refetch(self):
        old = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        new = _chat(t=25, last_received="M2", records=[_message("M2", 30)])
        assert chat_sync_marker_changed(new, chat_sync_marker(old)) is False

    def test_and_the_content_signal_does_not_smuggle_it_back_in(self):
        """local_history_behind_server() must stay quiet here too: the server
        claims *less* than we hold, which is the one direction that cannot mean
        a message is missing locally."""
        new = _chat(t=25, last_received="M2", records=[_message("M2", 30)])
        assert local_history_behind_server(new) is False

    def test_a_higher_server_activity_is_still_the_first_signal(self):
        old = _chat(t=30, last_received="M2", records=[_message("M2", 30)])
        new = _chat(t=45, last_received="M2", records=[_message("M2", 30)])
        assert classify_chat_sync(new, chat_sync_marker(old)) == (
            "incremental", "activity-changed")


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
