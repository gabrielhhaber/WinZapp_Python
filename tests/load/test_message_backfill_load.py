"""Synthetic load tests for message-backfill bookkeeping.

Run from the repository root::

    python -m pytest -s -q tests/load/test_message_backfill_load.py

``WINZAPP_LOAD_CHAT_COUNT`` and ``WINZAPP_LOAD_WORKERS`` can increase or
decrease the default 5,000-conversation / 32-worker scenario. This exercises
the real queue implementation without requiring a WhatsApp account or making
network requests; fixed time thresholds are deliberately avoided because CI
and developer machines have very different performance profiles.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import time

import pytest

from main import MainWindow
import main as main_module


pytestmark = pytest.mark.load


class _LoadStub:
    _server_claims_content = staticmethod(MainWindow._server_claims_content)
    _note_backfill_state = MainWindow._note_backfill_state
    _backfill_state_guard = MainWindow._backfill_state_guard
    _canonical_backfill_jid = MainWindow._canonical_backfill_jid
    _collapse_and_list_backfill_pending = MainWindow._collapse_and_list_backfill_pending
    _remove_backfill_pending = MainWindow._remove_backfill_pending
    _is_backfill_pending = MainWindow._is_backfill_pending
    _completed_backfill_targets = MainWindow._completed_backfill_targets
    _jid_address_forms = MainWindow._jid_address_forms
    history_page_target = MainWindow.history_page_target

    def __init__(self, chat_count: int):
        self._backfill_state_lock = threading.RLock()
        self._chats_awaiting_messages = set()
        self._partial_history_counts = {}
        self._history_still_landing = True
        self._history_gap_jids = set()
        self.settings = {"user_interface": {"messages_page_size": 200}}
        self._lid_to_phone = {
            f"{index}@lid": f"5511{index:09d}@s.whatsapp.net"
            for index in range(chat_count)
        }
        self._phone_to_lid = {
            phone: lid for lid, phone in self._lid_to_phone.items()
        }


def _positive_int_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _records(count: int) -> list[dict]:
    return [{"key": {"id": f"LOAD-{index}"}} for index in range(count)]


def test_backfill_queue_survives_large_concurrent_account():
    chat_count = _positive_int_env("WINZAPP_LOAD_CHAT_COUNT", 5_000)
    workers = _positive_int_env("WINZAPP_LOAD_WORKERS", 32)
    stub = _LoadStub(chat_count)
    blank_chat = {"unreadCount": 1, "t": 1}
    full_chat = {"messages": {"messages": {"records": _records(200)}}}

    started = time.perf_counter()

    def queue_both_forms(index: int) -> None:
        lid = f"{index}@lid"
        phone = stub._lid_to_phone[lid]
        stub._note_backfill_state(lid, blank_chat, api_ok=True)
        stub._note_backfill_state(phone, blank_chat, api_ok=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(queue_both_forms, range(chat_count)))

    queued = stub._collapse_and_list_backfill_pending()
    assert len(queued) == chat_count
    assert all(jid.endswith("@s.whatsapp.net") for jid in queued)
    assert not any(jid.endswith("@lid") for jid in queued)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(
            lambda jid: stub._note_backfill_state(jid, full_chat, api_ok=True),
            queued,
        ))

    completed = stub._completed_backfill_targets(queued)
    elapsed = time.perf_counter() - started
    operations = chat_count * 3

    assert completed == chat_count
    assert stub._collapse_and_list_backfill_pending() == []
    assert stub._partial_history_counts == {}

    print("\nBACKFILL_LOAD_RESULT=" + json.dumps({
        "chats": chat_count,
        "workers": workers,
        "state_transitions": operations,
        "elapsed_seconds": round(elapsed, 3),
        "transitions_per_second": round(operations / elapsed),
        "duplicates_after_canonicalization": 0,
        "remaining_pending": 0,
    }, sort_keys=True))


_REAL_SLEEP = time.sleep


class _SweepLoadStub:
    _backfill_empty_chats = MainWindow._backfill_empty_chats
    _backfill_state_guard = MainWindow._backfill_state_guard
    _canonical_backfill_jid = MainWindow._canonical_backfill_jid
    _collapse_and_list_backfill_pending = MainWindow._collapse_and_list_backfill_pending
    _remove_backfill_pending = MainWindow._remove_backfill_pending
    _is_backfill_pending = MainWindow._is_backfill_pending
    _completed_backfill_targets = MainWindow._completed_backfill_targets
    _jid_address_forms = MainWindow._jid_address_forms
    _resolve_backfill_target = MainWindow._resolve_backfill_target
    _local_record_count = MainWindow._local_record_count
    _initial_backfill_delay = MainWindow._initial_backfill_delay
    _background_backfill_work_allowed = staticmethod(MainWindow._background_backfill_work_allowed)
    _backfill_short_queue_delays = MainWindow._backfill_short_queue_delays
    _keep_backfill_pending = MainWindow._keep_backfill_pending
    history_page_target = MainWindow.history_page_target

    _BACKFILL_BUDGET = MainWindow._BACKFILL_BUDGET
    _BACKFILL_LANDING_BUDGET = MainWindow._BACKFILL_LANDING_BUDGET
    _BACKFILL_FIRST_DELAY = MainWindow._BACKFILL_FIRST_DELAY
    _BACKFILL_CHUNK_DELAY = MainWindow._BACKFILL_CHUNK_DELAY
    _BACKFILL_MAX_DELAY = MainWindow._BACKFILL_MAX_DELAY
    _BACKFILL_WORKERS = MainWindow._BACKFILL_WORKERS
    _BACKFILL_CHUNK = MainWindow._BACKFILL_CHUNK
    _DEEP_CHATS_PER_PASS = MainWindow._DEEP_CHATS_PER_PASS
    _OLDER_REQUESTS_PER_PASS = MainWindow._OLDER_REQUESTS_PER_PASS
    _OLDER_REQUEST_GRACE = getattr(MainWindow, "_OLDER_REQUEST_GRACE", 300)

    def __init__(self, chat_count: int):
        self._backfill_state_lock = threading.RLock()
        self._chats_awaiting_messages = {
            f"5511{index:09d}@s.whatsapp.net" for index in range(chat_count)
        }
        self._partial_history_counts = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._history_still_landing = False
        self._ui_ready_event = threading.Event()
        self._ui_ready_event.set()
        self._sync_run_id = 1
        self._wa_connected = True
        self.settings = {"user_interface": {"messages_page_size": 200}}
        self.chats = {
            jid: {
                "remoteJid": jid,
                "messages": {"messages": {"records": []}},
            }
            for jid in self._chats_awaiting_messages
        }
        self.calls = []
        self.refresh_calls = 0
        self.save_calls = 0
        self._active_calls = 0
        self.max_active_calls = 0
        self._metrics_lock = threading.Lock()

    def refresh_history_still_landing(self, context=""):
        self.refresh_calls += 1
        return False

    def _pending_name_resolution(self):
        return []

    def _backfill_names(self):
        return 0

    def _chats_needing_deep_history(self):
        return []

    def _schedule_save(self):
        self.save_calls += 1

    def _schedule_set_chats(self):
        return None

    def _start_deferred_media_sync(self):
        pass

    def unblock_history_sync(self):
        pass

    def _sync_older_chat_history_from_phone(self, target, run_id=None):
        return False

    def request_older_messages(self, jid):
        return False

    def _persist_older_requested(self):
        pass

    def _persist_backfill_pending_state(self):
        pass

    def _persist_history_gap_jids(self):
        pass

    def sync_chat_messages(self, chat, run_id=None):
        jid = chat["remoteJid"]
        with self._metrics_lock:
            self._active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self._active_calls)
        try:
            # Small real latency keeps calls overlapping even though the loop's
            # own long pacing sleeps are replaced below.
            _REAL_SLEEP(0.0005)
            self._remove_backfill_pending(jid)
            self.chats[jid]["messages"]["messages"]["records"] = [{"key": {"id": "1"}}] * 200
            with self._metrics_lock:
                self.calls.append(jid)
        finally:
            with self._metrics_lock:
                self._active_calls -= 1


def test_real_backfill_loop_covers_large_account_with_bounded_workers(monkeypatch):
    chat_count = _positive_int_env("WINZAPP_LOAD_SWEEP_CHATS", 935)
    stub = _SweepLoadStub(chat_count)
    monkeypatch.setattr(main_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(main_module.wx, "CallAfter", lambda *_args, **_kwargs: None)

    started = time.perf_counter()
    stub._backfill_empty_chats()
    elapsed = time.perf_counter() - started

    assert len(stub.calls) == chat_count
    assert len(set(stub.calls)) == chat_count
    assert stub._collapse_and_list_backfill_pending() == []
    assert 1 < stub.max_active_calls <= MainWindow._BACKFILL_WORKERS
    assert stub.refresh_calls <= 2

    expected_chunks = -(-chat_count // MainWindow._BACKFILL_CHUNK)
    print("\nBACKFILL_SWEEP_LOAD_RESULT=" + json.dumps({
        "chats": chat_count,
        "chunks": expected_chunks,
        "configured_workers": MainWindow._BACKFILL_WORKERS,
        "max_observed_concurrency": stub.max_active_calls,
        "elapsed_seconds_without_pacing_sleeps": round(elapsed, 3),
        "unique_chats_processed": len(set(stub.calls)),
        "remaining_pending": 0,
    }, sort_keys=True))
