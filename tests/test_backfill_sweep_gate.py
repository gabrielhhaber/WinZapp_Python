"""Deep history and names must run once a sweep has tried every pending chat
(issue #756).

`attempted` used to be cleared at the end of the last chunk of a sweep, so the
next pass's gate saw every unopened chat as untried again, found them all
"blocking", and never let `_backfill_names()` or `deep_backfill_chat()` run:
the unit test of `_chats_blocking_background_work` passed because it never
crossed a sweep boundary. This one drives the real loop across several sweeps.
"""

import types

from core.sync_lifecycle import capture_sync_context
from main import MainWindow
from tests.god_modules import patch_main_global
from tests.test_phone_history_needs_an_open_chat import _Stub

UNOPENED = [f"55119000000{index:02d}@s.whatsapp.net" for index in range(5)]


def _run_loop(monkeypatch):
    stub = _Stub()
    clock = types.SimpleNamespace(now=1.0)
    synced, names_calls, deep_calls = [], [], []
    stub._sync_run_id, stub.token = 1, "synthetic:key"
    stub.wpp_server, stub.wpp_port = "http://synthetic.invalid", 6300
    stub._wa_connected = True
    stub._ui_ready_event = types.SimpleNamespace(is_set=lambda: True)
    stub._BACKFILL_BUDGET = stub._BACKFILL_LANDING_BUDGET = 60
    stub._BACKFILL_FIRST_DELAY, stub._BACKFILL_MAX_DELAY = 1, 2
    stub._BACKFILL_CHUNK = 2
    stub._BACKFILL_WORKERS = 1
    stub._DEEP_CHATS_PER_PASS = 3
    stub._OLDER_REQUESTS_PER_PASS, stub._PHONE_REQUEST_MIN_GAP = 1, 0
    stub._OLDER_REQUEST_GRACE, stub._MAX_PHONE_HISTORY_REQUESTS = 30, 2
    stub._older_requested_chats, stub._older_request_attempts = {}, {}
    stub._initial_backfill_delay = lambda *a: 1
    stub._collapse_and_list_backfill_pending = lambda: list(UNOPENED)
    stub._pending_name_resolution = lambda: []
    stub._chats_needing_deep_history = lambda: ["deep-chat"]
    stub._voice_call_in_progress = lambda: False
    stub.refresh_history_still_landing = lambda **k: False
    stub._start_deferred_media_sync = lambda: None
    stub._background_backfill_work_allowed = MainWindow._background_backfill_work_allowed
    stub._chats_blocking_background_work = MainWindow._chats_blocking_background_work
    stub._backfill_names = lambda: names_calls.append(1) or 0
    stub.deep_backfill_chat = lambda jid, expected_context=None: deep_calls.append(jid) or 0
    stub._resolve_backfill_target = lambda jid: (jid, {"remoteJid": jid})
    stub._local_record_count = lambda jid: 1
    stub.history_page_target = lambda: 200
    stub._oldest_stored_message = lambda jid: {"key": {"id": "oldest"}}
    stub._anchor_identity = lambda message: message["key"]["id"]
    stub._keep_backfill_pending = lambda *a: None
    stub._remove_backfill_pending = lambda jid: None
    stub._persist_backfill_pending_state = stub._persist_history_gap_jids = lambda: None
    stub._persist_older_requested = lambda: None
    stub._completed_backfill_targets = lambda window: 0
    stub.request_older_messages = lambda *a, **k: False
    context = capture_sync_context(stub)

    def sync(chat, run, expected_context=None):
        synced.append(chat["remoteJid"])
        return True                 # answered: only such a fetch lets a chat rest

    stub.sync_chat_messages = sync

    class InlinePool:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, *args, **kwargs):
            result = fn(*args, **kwargs)
            return types.SimpleNamespace(result=lambda: result)

    patch_main_global(monkeypatch, "ThreadPoolExecutor", InlinePool)
    patch_main_global(monkeypatch, "as_completed", lambda futures: futures)
    patch_main_global(monkeypatch, "time", types.SimpleNamespace(
        monotonic=lambda: clock.now, time=lambda: clock.now,
        sleep=lambda delay: setattr(clock, "now", clock.now + delay)))
    MainWindow._backfill_empty_chats(stub, expected_context=context)
    return synced, names_calls, deep_calls


def test_deep_history_and_names_run_after_a_sweep_of_unopened_chats(monkeypatch):
    synced, names_calls, deep_calls = _run_loop(monkeypatch)
    assert set(UNOPENED) <= set(synced)       # every chat had its local fetch
    assert names_calls, "names never ran: unopened chats still block the sweep"
    assert "deep-chat" in deep_calls, "deep history never ran"


def test_short_chats_that_stay_empty_rest_and_deep_history_runs_more_often(monkeypatch):
    """Issue #766: 60 of 257 short chats came back empty at every pass, and
    deep history only ran once each of those sweeps had finished."""
    synced, names_calls, deep_calls = _run_loop(monkeypatch)
    from collections import Counter
    turns = Counter(synced)
    # 60 s of budget at 1 s passes: without the rest every chat would take a
    # turn every few passes; with it, a chat that stays empty waits 2 minutes
    assert max(turns.values()) == 1, turns
    assert len(deep_calls) > 5, deep_calls
