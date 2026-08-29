from pathlib import Path


SOURCE = (Path(__file__).parents[1] / "client" / "main.py").read_text(encoding="utf-8")


def test_startup_captures_baseline_before_remote_chat_merge():
    capture = SOURCE.index("sync_baseline = self._capture_chat_sync_baseline()")
    remote = SOURCE.index("result   = self.get_remote_chats", capture)
    plan = SOURCE.index("self._plan_message_sync(sync_baseline", remote)
    assert capture < remote < plan


def test_warm_start_has_distinct_non_full_status():
    assert 'self.i18n.t("updating_conversations")' in SOURCE
    assert 'if force_full:\n            unblock_result = self.unblock_history_sync()' in SOURCE


def test_full_sync_is_explicitly_latched_for_manual_resync_and_empty_cache():
    assert 'self._persist_full_sync_pending("manual-resync")' in SOURCE
    assert 'self._persist_full_sync_pending("empty-local-cache")' in SOURCE


def test_periodic_poll_uses_message_delta_instead_of_global_resync():
    block = SOURCE[SOURCE.index("def start_periodic_contacts_sync"):SOURCE.index(
        "def _phone_digits_equivalent")]
    assert "baseline = self._capture_chat_sync_baseline()" in block
    assert "self._plan_message_sync(" in block
    assert "self.sync_remote_chats(full_targets, incremental=False)" in block
    assert "self.sync_remote_chats(incremental_targets, incremental=True)" in block
    assert "self.sync_media_for_all_chats(changed_jids)" in block


def test_pending_history_repair_survives_restart():
    assert 'get_metadata_json("backfill_pending_v1", {})' in SOURCE
    assert 'set_metadata_json("backfill_pending_v1", payload)' in SOURCE


def test_incremental_message_window_is_bounded_and_adaptive():
    block = SOURCE[SOURCE.index("def sync_chat_messages"):SOURCE.index(
        "def _fetch_remote_message_ids")]
    assert 'sync_mode="full"' in block
    assert "_INCREMENTAL_MESSAGE_WINDOW" in block
    assert "_next_incremental_limit(" in block
    assert "incremental_no_overlap" in block


def test_failed_message_delta_is_durable_and_blocks_false_completion():
    assert 'get_metadata_json("message_retry_jids_v1", [])' in SOURCE
    assert 'set_metadata_json("message_retry_jids_v1", payload)' in SOURCE
    assert 'message_sync_ok = not message_failures' in SOURCE
    assert 'chat_list_ok and chat_list_settled and message_sync_ok' in SOURCE
    assert 'reasons[jid] = "retry-failed"' in SOURCE

def test_list_chat_markers_are_deferred_until_message_delta_succeeds():
    assert "defer_chat_save: bool = False" in SOURCE
    assert "elif not defer_chat_save:" in SOURCE
    run_sync = SOURCE[SOURCE.index("def _run_sync"):SOURCE.index("def clear_local_data")]
    assert run_sync.count("defer_chat_save=True") >= 3
    assert "self._schedule_save()" in run_sync


def test_periodic_poll_does_not_commit_failed_delta_marker():
    block = SOURCE[SOURCE.index("def start_periodic_contacts_sync"):SOURCE.index(
        "def _phone_digits_equivalent")]
    assert "defer_chat_save=True" in block
    assert "message_failures = set()" in block
    assert "if not message_failures:" in block
    assert "Deferring chat-list metadata save" in block


def test_chat_marker_is_committed_only_after_selected_message_fetch_succeeds():
    block = SOURCE[SOURCE.index("def sync_chat_messages"):SOURCE.index(
        "def _fetch_remote_message_ids")]
    assert "message_fetch_satisfied = bool(api_ok and incremental_satisfied)" in block
    assert "if message_fetch_satisfied:\n                self.db.upsert_chat" in block
    assert "if api_ok and all_messages:\n                self.db.insert_messages_batch" in block
    assert block.index("self.db.insert_messages_batch(remote_jid, all_messages)") < block.index(
        "self.db.upsert_chat(remote_jid, chat)"
    )


def test_history_repair_state_is_persisted_before_chat_marker_commit():
    block = SOURCE[SOURCE.index("def sync_chat_messages"):SOURCE.index(
        "def _fetch_remote_message_ids")]
    gap_persist = block.index("self._persist_history_gap_jids()")
    pending_persist = block.index("self._persist_backfill_pending_state()")
    chat_commit = block.index("self.db.upsert_chat(remote_jid, chat)")
    assert gap_persist < chat_commit
    assert pending_persist < chat_commit
