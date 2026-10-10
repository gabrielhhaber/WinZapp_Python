"""Resume must find new messages even when chat-list metadata never moves.

Methods run on plain stubs; no wx app, frame, socket or server is started.
"""

import logging
from types import SimpleNamespace
import pytest

import connection_state as cs
from core.resume_message_sync import RESUME_RECHECK_PER_ROUND
from main import MainWindow
from tests.test_periodic_poll_delta import _PollStub, _cycles, A, B
from tests.test_incremental_delta_outcomes import _DeltaStub, _Resp, _seed_warm_chat
import main as main_module


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(main_module.time, "time", lambda: 1002.0)


def _wake(stub, now):
    cs.reset_state_for_resume(stub, now)


def test_wake_fetches_unchanged_chats_in_the_automatic_poll(monkeypatch):
    stub = _PollStub()
    _wake(stub, main_module.time.time() + 1)
    _cycles(stub, monkeypatch)
    assert stub.message_rounds == [(True, [A, B])]
    assert stub.saves == 1
    assert stub.reconciles == 1


def test_active_alias_is_first_and_budget_defers_recent_chats():
    stub = _PollStub()
    template = stub.chats[A]
    stub.chats = {}
    for i in range(RESUME_RECHECK_PER_ROUND + 3):
        jid = f"55119000{i:05d}@s.whatsapp.net"
        stub.chats[jid] = dict(template, remoteJid=jid, t=100 + i)
    active = next(iter(stub.chats))  # Oldest chat, but it must come first.
    lid = "123456789000000@lid"
    stub._phone_to_lid = {active: lid}
    stub._lid_to_phone = {lid: active}
    stub.conversations_panel = SimpleNamespace(conversation={"remoteJid": lid})
    _wake(stub, 1000)
    stub._chat_verified_at = {jid: 999 for jid in stub.chats}
    stub._verified_activity = {jid: chat["t"] for jid, chat in stub.chats.items()}
    baseline = stub._capture_chat_sync_baseline()
    full, incremental, skipped, reasons = stub._plan_message_sync(baseline)
    assert full == []
    assert incremental[0]["remoteJid"] == active
    assert len(incremental) == RESUME_RECHECK_PER_ROUND
    assert skipped == 3
    assert set(reasons.values()) == {"resume-recheck"}
    assert incremental[1]["t"] == 100 + RESUME_RECHECK_PER_ROUND + 2
    for chat in incremental:
        stub._chat_verified_at[chat["remoteJid"]] = 1001
    _, remaining, _, _ = stub._plan_message_sync(baseline)
    assert len(remaining) == 3
    assert not {c["remoteJid"] for c in remaining} & set(reasons)


def test_post_wake_alias_verification_prevents_repeated_fetches():
    stub = _PollStub()
    lid = "123456789000000@lid"
    stub._phone_to_lid = {A: lid}
    stub._lid_to_phone = {lid: A}
    _wake(stub, 1000)
    stub._chat_verified_at = {lid: 1001, B: 1001}
    assert stub._plan_message_sync(stub._capture_chat_sync_baseline())[1] == []
    _wake(stub, 1002)
    assert len(stub._plan_message_sync(stub._capture_chat_sync_baseline())[1]) == 2


def test_real_io_fault_still_defers_metadata_save(monkeypatch):
    stub = _PollStub()
    _wake(stub, main_module.time.time() + 1)
    stub.failing_jids = {A}
    _cycles(stub, monkeypatch)
    assert stub.message_rounds == [(True, [A, B])]
    assert stub.saves == 0


def test_pre_wake_request_finishing_after_wake_does_not_verify_recovery():
    stub = _PollStub()
    _wake(stub, 1000.5)
    stub._chat_verified_at = {}
    MainWindow._note_chat_verified_now(stub, A, started_at=1000.4)
    MainWindow._note_chat_verified_now(stub, B, started_at=1000.6)
    _, targets, _, _ = stub._plan_message_sync(stub._capture_chat_sync_baseline())
    assert [c["remoteJid"] for c in targets] == [A]
    # An old request completing late must not overwrite a newer verification.
    MainWindow._note_chat_verified_now(stub, B, started_at=999)
    assert stub._chat_verified_at[B] == 1000.6


def test_forced_full_sync_does_not_duplicate_resume_targets():
    stub = _PollStub()
    _wake(stub, 1000)
    full, incremental, skipped, _ = stub._plan_message_sync(
        stub._capture_chat_sync_baseline(), force_full=True)
    assert len(full) == 2
    assert incremental == []
    assert skipped == 0


def test_stale_metadata_new_messages_are_saved_and_counted(monkeypatch, caplog):
    stub = _DeltaStub()
    _seed_warm_chat(stub)
    jid = next(iter(stub.chats))
    old = stub.chats[jid]["messages"]["messages"]["records"][0]
    new = dict(old, key=dict(old["key"], id="new"), messageTimestamp=101)
    stub._normalized = [old, new]
    stub._note_chat_verified_now = lambda remote_jid, **kw: MainWindow._note_chat_verified_now(
        stub, remote_jid, **kw)
    stub._note_verified_activity = lambda *args: None
    _wake(stub, 1000.5)
    clock = [1000.6]
    monkeypatch.setattr(main_module.time, "time", lambda: clock[0])

    def respond(*args, **kwargs):
        clock[0] = 1000.8  # Completion must not replace the query start time.
        return _Resp(200, {"response": [old, new]})

    monkeypatch.setattr(main_module.requests, "get", respond)
    with caplog.at_level(logging.INFO):
        assert MainWindow.sync_chat_messages(stub, stub.chats[jid],
                                            sync_mode="incremental") is True
    assert any(m["key"]["id"] == "new" for _, messages in stub.db.inserted for m in messages)
    assert stub._chat_verified_at[jid] == 1000.6
    assert "new_to_cache=1 newer_than_cache=1" in caplog.text
    assert "persisted=True" in caplog.text


def test_wake_poll_waits_for_connection_and_active_sync(monkeypatch):
    stub = _PollStub()
    _wake(stub, main_module.time.time() + 1)
    stub._wa_connected = False
    _cycles(stub, monkeypatch)
    assert stub.message_rounds == []
    stub = _PollStub()
    _wake(stub, main_module.time.time() + 1)
    stub._initial_sync_running = True
    _cycles(stub, monkeypatch)
    assert stub.message_rounds == []
