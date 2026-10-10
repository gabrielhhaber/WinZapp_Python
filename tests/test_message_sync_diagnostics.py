import logging

from core.message_sync_diagnostics import fetched_page_summary, newest_message_seconds
from main import MainWindow
import main as main_module
from tests.test_incremental_delta_outcomes import _DeltaStub, _Resp, _seed_warm_chat


def _msg(mid, ts):
    return {"key": {"id": mid}, "messageTimestamp": ts}


def test_new_older_history_is_not_recovered_newer_messages():
    summary = fetched_page_summary([_msg("old", 10), _msg("known", 20)], {"known"}, 20)
    assert summary[:3] == (2, 1, 0)
    assert summary[3] == summary[4] == "1970-01-01T00:00:20+00:00"


def test_duplicate_ids_are_counted_once_and_milliseconds_normalized():
    ts = 1_791_000_000
    summary = fetched_page_summary([_msg("new", (ts + 1) * 1000)] * 2, set(), ts)
    assert summary[:3] == (1, 1, 1)
    assert newest_message_seconds([_msg("known", ts * 1000)]) == ts


def test_empty_response_does_not_claim_the_preserved_cache_was_fetched(monkeypatch, caplog):
    stub = _DeltaStub()
    _seed_warm_chat(stub)
    jid = next(iter(stub.chats))
    monkeypatch.setattr(main_module.requests, "get", lambda *args, **kw: _Resp(
        200, {"response": []}))
    with caplog.at_level(logging.INFO):
        MainWindow.sync_chat_messages(stub, stub.chats[jid], sync_mode="incremental")
    assert "fetched=0 new_to_cache=0 newer_than_cache=0 latest_fetched=none" in caplog.text
    assert stub.chats[jid]["messages"]["messages"]["records"]


def test_known_page_does_not_count_preserved_history_as_fetched(monkeypatch, caplog):
    stub = _DeltaStub()
    _seed_warm_chat(stub)
    jid = next(iter(stub.chats))
    records = stub.chats[jid]["messages"]["messages"]["records"]
    old = records[0]
    records.append(dict(old, key=dict(old["key"], id="older"), messageTimestamp=10))
    stub._normalized = [old]
    monkeypatch.setattr(main_module.requests, "get", lambda *args, **kw: _Resp(
        200, {"response": [old]}))
    with caplog.at_level(logging.INFO):
        MainWindow.sync_chat_messages(stub, stub.chats[jid], sync_mode="incremental")
    assert "fetched=1 new_to_cache=0 newer_than_cache=0" in caplog.text
    assert len(stub.chats[jid]["messages"]["messages"]["records"]) == 2
