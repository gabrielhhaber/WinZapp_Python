"""A read on the desktop must reach the known phone alias, or stay retryable.

Plain mixins/stubs only: no windows, real HTTP, account files or worker threads.
"""
from types import SimpleNamespace

import pytest

from main_window.read_state import ReadStateMixin
import main_window.read_state as read_state


PHONE = "12025550123@s.whatsapp.net"
WIRE_PHONE = "12025550123@c.us"
LID = "100000000000001@lid"
GROUP = "120363000000001@g.us"


class Reader(ReadStateMixin):
    _normalize_jid = staticmethod(lambda jid: jid.replace("@c.us", "@s.whatsapp.net"))

    def __init__(self, unread=0, server_unread=4):
        self._phone_to_lid = {PHONE: LID}
        self._lid_to_phone = {LID: PHONE}
        self.wpp_server = "http://synthetic.invalid"
        self.wpp_port = 0
        self.token = "synthetic"
        self.chats = {PHONE: {"unreadCount": unread, "t": 100}}
        self._server_unread = {PHONE: server_unread}
        self.jobs = []
        self.saves = []
        self.refreshes = []
        self.persisted = []

    def _sync_conversation_read_state(self, jid, unread, on_failure, on_success=None):
        self.jobs.append(SimpleNamespace(fail=on_failure, succeed=on_success))

    def _persist_locally_read_at(self):
        self.persisted.append(dict(getattr(self, "_locally_read_at", {})))

    def _schedule_save(self, dirty_jid=None):
        self.saves.append(dirty_jid)

    def _refresh_chat_row_in_list(self, jid):
        self.refreshes.append(jid)

    def _schedule_set_chats(self):
        self.refreshes.append("list")

    def set_chats(self):
        self.refreshes.append("list")


@pytest.fixture(autouse=True)
def no_desktop_or_network(monkeypatch):
    monkeypatch.setattr(read_state.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(read_state.time, "sleep", lambda _: None)
    monkeypatch.setattr(read_state, "api_post", lambda *a, **kw: pytest.fail("real HTTP"))


def response(ok=True, data=None):
    return SimpleNamespace(ok=ok, status_code=200 if ok else 404, text="synthetic",
                           json=lambda: {"response": {"data": [True] if data is None else data}})


@pytest.mark.parametrize("jid", [PHONE, WIRE_PHONE, LID])
@pytest.mark.parametrize("unread", [False, True])
def test_rejected_lid_falls_back_to_phone_from_either_identity(monkeypatch, jid, unread):
    reader = Reader()
    calls = []

    def post(url, *, json, **kwargs):
        calls.append(json)
        return response(json["phone"] == WIRE_PHONE)

    monkeypatch.setattr(read_state, "api_post", post)
    assert reader._send_read_state_blocking(jid, unread, attempts=1) is True
    assert calls == [
        {"phone": LID, "isGroup": False, "unread": unread, "isLid": True},
        {"phone": WIRE_PHONE, "isGroup": False, "unread": unread},
    ]


@pytest.mark.parametrize("jid,expected", [(PHONE, WIRE_PHONE), (WIRE_PHONE, WIRE_PHONE),
                                         (LID, LID), (GROUP, GROUP)])
def test_unmapped_and_group_targets_are_not_duplicated(monkeypatch, jid, expected):
    reader = Reader()
    reader._phone_to_lid = {}
    reader._lid_to_phone = {}
    calls = []
    monkeypatch.setattr(read_state, "api_post", lambda *a, **kw: calls.append(kw["json"]) or response(False))
    assert reader._send_read_state_blocking(jid, False, attempts=2) is False
    assert [call["phone"] for call in calls] == [expected, expected]
    assert all(call["isGroup"] == (jid == GROUP) for call in calls)
    assert all(call.get("isLid", False) == (jid == LID) for call in calls)


def test_successful_lid_stops_before_phone(monkeypatch):
    calls = []
    monkeypatch.setattr(read_state, "api_post", lambda *a, **kw: calls.append(kw["json"]["phone"]) or response())
    assert Reader()._send_read_state_blocking(PHONE, False) is True
    assert calls == [LID]


def test_server_only_unread_survives_failure_and_explicit_reread():
    reader = Reader()
    reader.mark_conversation_as_read(PHONE)
    reader.jobs[0].fail()
    assert reader.chats[PHONE]["unreadCount"] == 0
    assert reader._server_unread[PHONE] == 4
    assert PHONE not in reader._locally_read_at
    reader.mark_conversation_as_read(PHONE)
    assert len(reader.jobs) == 2


def test_failed_old_read_cannot_undo_new_success_in_same_second():
    reader = Reader(unread=3)
    reader.mark_conversation_as_read(PHONE)
    reader.mark_conversation_as_read(PHONE, force=True)
    reader.jobs[1].succeed()
    reader.jobs[0].fail()
    assert reader.chats[PHONE]["unreadCount"] == 0
    assert reader._locally_read_at[PHONE] == 100
    assert reader._server_unread.get(PHONE, 0) == 0


def test_second_failed_read_keeps_the_first_requests_remote_evidence():
    reader = Reader()
    reader.mark_conversation_as_read(PHONE)
    reader.mark_conversation_as_read(PHONE, force=True)
    reader.jobs[0].fail()
    reader.jobs[1].fail()
    assert reader._server_unread[PHONE] == 4


@pytest.mark.parametrize("server_count", [0, 2, 7])
def test_superseding_read_uses_the_newest_server_evidence(server_count):
    reader = Reader()
    reader.mark_conversation_as_read(PHONE)
    reader._note_server_unread(PHONE, server_count)
    reader.mark_conversation_as_read(PHONE, force=True)
    reader.jobs[0].fail()
    reader.jobs[1].fail()
    assert reader._server_unread.get(PHONE, 0) == server_count


@pytest.mark.parametrize("server_count", [0, 2, 7])
def test_failure_preserves_a_more_recent_server_report(server_count):
    reader = Reader()
    reader.mark_conversation_as_read(PHONE)
    reader._note_server_unread(PHONE, server_count)
    reader.jobs[0].fail()
    assert reader._server_unread[PHONE] == server_count


def test_failure_cannot_overwrite_a_new_message():
    reader = Reader(unread=3)
    reader.mark_conversation_as_read(PHONE)
    reader.chats[PHONE].update(t=101, unreadCount=1)
    reader._new_since_read[PHONE] = 1
    reader.jobs[0].fail()
    assert reader.chats[PHONE]["unreadCount"] == 1
    assert reader._locally_read_at[PHONE] == 100


def test_advanced_activity_with_local_zero_uses_the_captured_timestamp():
    reader = Reader(unread=3)
    reader.mark_conversation_as_read(PHONE)
    reader.chats[PHONE]["t"] = 101
    # A later read has the new marker, but the earlier callback owns t=100.
    reader._locally_read_at[PHONE] = 101
    reader.jobs[0].fail()
    assert reader.chats[PHONE]["unreadCount"] == 0
    assert reader._locally_read_at[PHONE] == 101


def test_failure_cannot_modify_a_replacement_chat_with_the_same_timestamp():
    reader = Reader(unread=3)
    reader.mark_conversation_as_read(PHONE)
    reader.chats[PHONE] = {"unreadCount": 0, "t": 100}
    reader.jobs[0].fail()
    assert reader.chats[PHONE]["unreadCount"] == 0


def test_explicit_unread_invalidates_the_pending_read_failure():
    reader = Reader(unread=3)
    reader.mark_conversation_as_read(PHONE)
    reader.mark_conversation_as_unread(PHONE)
    reader.jobs[0].fail()
    assert reader.chats[PHONE]["unreadCount"] == 1
    assert reader._server_unread.get(PHONE, 0) == 0


def test_bulk_failure_preserves_server_only_unread_and_does_not_touch_newer_read():
    reader = Reader()
    old_job = reader.mark_conversation_as_read(PHONE, batched=True)
    reader.mark_conversation_as_read(PHONE, force=True)
    reader.jobs[-1].succeed()
    announced = []
    reader.output = lambda text, **kw: announced.append(text)
    reader.i18n = SimpleNamespace(t=lambda key: "{count}")
    reader._on_bulk_read_failed([old_job])
    assert reader.chats[PHONE]["unreadCount"] == 0
    assert reader._server_unread.get(PHONE, 0) == 0
    assert announced == []


@pytest.mark.parametrize("success_first", [True, False])
def test_any_confirmed_overlapping_read_beats_a_same_activity_failure(success_first):
    reader = Reader(unread=3)
    reader.mark_conversation_as_read(PHONE)
    reader.mark_conversation_as_read(PHONE, force=True)
    callbacks = [reader.jobs[0].succeed, reader.jobs[1].fail]
    for callback in callbacks if success_first else reversed(callbacks):
        callback()
    assert reader.chats[PHONE]["unreadCount"] == 0
    assert reader._locally_read_at[PHONE] == 100
    assert reader._server_unread.get(PHONE, 0) == 0


@pytest.mark.parametrize("reread_before_failure", [True, False])
def test_remote_retry_evidence_survives_activity_advancement(reread_before_failure):
    reader = Reader()
    reader.mark_conversation_as_read(PHONE)
    reader.chats[PHONE]["t"] = 101
    if reread_before_failure:
        reader.mark_conversation_as_read(PHONE)
        assert len(reader.jobs) == 2
    reader.jobs[0].fail()
    if reread_before_failure:
        reader.jobs[1].fail()
    else:
        assert reader._server_unread[PHONE] == 4
        reader.mark_conversation_as_read(PHONE)
        assert len(reader.jobs) == 2


def test_noop_reread_cannot_let_old_failure_restore_a_consumed_server_zero():
    reader = Reader()
    reader.mark_conversation_as_read(PHONE)
    reader._note_server_unread(PHONE, 0)
    reader.mark_conversation_as_read(PHONE)
    assert len(reader.jobs) == 1
    reader.jobs[0].fail()
    assert reader._server_unread.get(PHONE, 0) == 0


def test_a_later_read_cannot_reuse_an_already_completed_confirmation():
    reader = Reader(unread=3)
    reader.mark_conversation_as_read(PHONE)
    reader.jobs[0].succeed()
    reader.chats[PHONE]["unreadCount"] = 1
    reader._new_since_read[PHONE] = 1  # a new arrival in the same second
    reader.mark_conversation_as_read(PHONE)
    reader.jobs[1].fail()
    assert reader.chats[PHONE]["unreadCount"] == 1


@pytest.mark.parametrize("server_report", [False, True])
def test_older_success_does_not_confirm_a_new_arrival_in_the_same_second(server_report):
    reader = Reader(unread=3)
    reader.mark_conversation_as_read(PHONE)
    reader.chats[PHONE]["unreadCount"] = 1
    if server_report:
        reader._note_server_unread(PHONE, 1)
    else:
        reader._new_since_read[PHONE] = 1
    reader.mark_conversation_as_read(PHONE)
    reader.jobs[0].succeed()
    reader.jobs[1].fail()
    assert reader.chats[PHONE]["unreadCount"] == 1


def test_current_bulk_failure_still_announces_when_new_activity_blocks_rollback():
    reader = Reader()
    job = reader.mark_conversation_as_read(PHONE, batched=True)
    reader.chats[PHONE]["t"] = 101
    announced = []
    reader.output = lambda text, **kw: announced.append(text)
    reader.i18n = SimpleNamespace(t=lambda key: "{count}")
    reader._on_bulk_read_failed([job])
    assert announced == ["1"]
    assert reader._server_unread[PHONE] == 4


def test_visible_same_second_arrival_is_not_covered_by_older_confirmation():
    reader = Reader(server_unread=0)
    reader.mark_conversation_as_read(PHONE, force=True)
    reader.chats[PHONE]["messages"] = {"messages": {"records": [{
        "key": {"id": "new-incoming", "fromMe": False},
        "messageTimestamp": 100,
        "message": {"conversation": "synthetic"},
    }]}}
    # Active visible arrivals do not increment unread or _new_since_read.
    reader.mark_conversation_as_read(PHONE, force=True)
    reader.jobs[0].succeed()
    reader.jobs[1].fail()
    reader.mark_conversation_as_read(PHONE)
    assert len(reader.jobs) == 3


def test_failed_forced_read_stays_retryable_even_without_an_unread_count():
    reader = Reader(server_unread=0)
    reader.mark_conversation_as_read(PHONE, force=True)
    reader.jobs[0].fail()
    reader.mark_conversation_as_read(PHONE)
    assert len(reader.jobs) == 2


def test_fresh_server_evidence_after_failure_is_not_confirmed_by_older_inflight_read():
    reader = Reader(unread=3)
    reader.mark_conversation_as_read(PHONE)
    reader.mark_conversation_as_read(PHONE, force=True)
    reader.jobs[1].fail()
    reader._note_server_unread(PHONE, 6)
    reader.mark_conversation_as_read(PHONE)
    reader.jobs[0].succeed()
    reader.jobs[2].fail()
    assert reader._server_unread[PHONE] == 6


def test_visible_arrival_before_failure_does_not_restore_an_older_badge():
    reader = Reader(unread=3)
    reader.mark_conversation_as_read(PHONE)
    reader.chats[PHONE]["messages"] = {"messages": {"records": [{
        "key": {"id": "new-incoming", "fromMe": False}, "messageTimestamp": 100,
    }]}}
    reader.jobs[0].fail()
    assert reader.chats[PHONE]["unreadCount"] == 0
    reader.mark_conversation_as_read(PHONE)
    assert len(reader.jobs) == 2


def test_bulk_server_only_failure_is_retryable_without_a_new_snapshot():
    reader = Reader()
    job = reader.mark_conversation_as_read(PHONE, batched=True)
    reader.output = lambda *a, **kw: None
    reader.i18n = SimpleNamespace(t=lambda key: "{count}")
    reader._on_bulk_read_failed([job])
    assert reader._server_unread[PHONE] == 4
    reader.mark_conversation_as_read(PHONE)
    assert len(reader.jobs) == 1


def test_worker_entry_defers_local_mutation_to_the_main_thread(monkeypatch):
    reader = Reader(unread=3)
    queued = []
    monkeypatch.setattr(read_state.wx, "IsMainThread", lambda: False)
    monkeypatch.setattr(read_state.wx, "CallAfter", lambda fn, *a, **kw: queued.append((fn, a, kw)))
    reader.mark_conversation_as_read(PHONE)
    assert reader.chats[PHONE]["unreadCount"] == 3
    assert reader.jobs == []
    fn, args, kwargs = queued.pop()
    monkeypatch.setattr(read_state.wx, "IsMainThread", lambda: True)
    fn(*args, **kwargs)
    assert reader.chats[PHONE]["unreadCount"] == 0
    assert len(reader.jobs) == 1


@pytest.mark.parametrize("ok", [False, True])
def test_background_sender_delivers_only_its_matching_callback(monkeypatch, ok):
    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    reader = Reader()
    reader._send_read_state_blocking = lambda jid, unread: ok
    monkeypatch.setattr(read_state.threading, "Thread", InlineThread)
    calls = []
    ReadStateMixin._sync_conversation_read_state(
        reader, PHONE, False, lambda: calls.append("failed"), lambda: calls.append("confirmed")
    )
    assert calls == ["confirmed" if ok else "failed"]


def test_transport_error_on_lid_still_tries_phone(monkeypatch):
    calls = []

    def post(*a, **kw):
        calls.append(kw["json"]["phone"])
        if calls[-1] == LID:
            raise TimeoutError("synthetic")
        return response()

    monkeypatch.setattr(read_state, "api_post", post)
    assert Reader()._send_read_state_blocking(PHONE, False, attempts=1)
    assert calls == [LID, WIRE_PHONE]


@pytest.mark.parametrize("data", [[], [False], [True, False], [1], "true", {"success": True}])
def test_http_success_without_positive_results_is_not_confirmation(monkeypatch, data):
    monkeypatch.setattr(read_state, "api_post", lambda *a, **kw: response(data=data))
    assert Reader()._send_read_state_blocking(PHONE, False, attempts=1) is False
