"""A pending PN snapshot may miss ready data under its known LID.

Exercise the shipped helper and coordinator with fake HTTP, clock, queued
workers and callbacks only; no App, window, socket, account or live thread.
"""
from types import SimpleNamespace

import pytest

from core.contact_presence import cached, merge
from core.contact_presence_query import query_targets, read_snapshot
from main_window.contact_presence import ContactPresenceMixin
import main_window.contact_presence as coordinator
from tests.test_contact_presence_freshness import Owner, PN, LID, scheduled

PENDING = {"status": "success", "presence": {"status": "pending"}}


def ready(**fields):
    return {"status": "success", "presence": {
        "status": "ready", "restricted": False, "isOnline": False,
        "state": "unavailable", "lastSeen": 1700000000, **fields,
    }}


def response(payload, code=200):
    return SimpleNamespace(status_code=code, json=lambda: payload)


@pytest.mark.parametrize("jid", [PN, LID])
def test_only_reciprocal_known_pair_adds_an_alternative(jid):
    assert query_targets(jid, {LID: PN}, {PN: LID}) == (PN, LID)


@pytest.mark.parametrize("jid,lids,phones,expected", [
    (PN, {}, {}, (PN,)),
    (LID, {}, {}, (LID,)),
    (PN, {}, {PN: LID}, (PN,)),
    (LID, {LID: PN}, {}, (PN,)),
    (LID, {LID: PN, "789@lid": PN}, {PN: "789@lid"}, (PN,)),
    (PN, {LID: "789@s.whatsapp.net"}, {PN: LID}, (PN,)),
    (PN, {}, {PN: None}, (PN,)),
    (LID, {LID: None}, {}, ()),
])
def test_missing_or_inconsistent_bridge_never_discovers_another_identity(jid, lids, phones, expected):
    assert query_targets(jid, lids, phones) == expected


@pytest.mark.parametrize("jid", ["123@g.us", "123@newsletter", "status@broadcast", "bad", "123@lid@c.us", "123@c.us"])
def test_targets_require_a_normalized_private_contact(jid):
    assert query_targets(jid, {}, {}) == ()


@pytest.mark.parametrize("code", [200, 201])
def test_explicit_modern_pending_reads_paired_ready_snapshot(code):
    calls = []
    def request(target, timeout):
        calls.append((target, timeout))
        return response(PENDING if target == PN else ready(), code)
    result = read_snapshot((PN, LID), request, lambda: 100)
    assert result["lastSeen"] == 1700000000
    assert calls == [(PN, 10), (LID, 10)]


@pytest.mark.parametrize("payload", [
    ready(restricted=True, lastSeen=None),
    ready(lastSeen=None),
    ready(isOnline=True, state="available", lastSeen=None),
    {"status": "success", "response": None},
    {"status": "success", "response": False},
    {"status": "success", "response": 1000},
])
def test_ready_and_legacy_results_are_terminal_even_without_a_timestamp(payload):
    calls = []
    def request(target, timeout):
        calls.append(target)
        return response(payload)
    assert read_snapshot((PN, LID), request, lambda: 100) is not None
    assert calls == [PN]


@pytest.mark.parametrize("payload,code", [
    ({}, 401), ({}, 403), ({}, 500), (PENDING, 500),
    (None, 200), ([], 200), ({"status": "error", "presence": {"status": "pending"}}, 200),
    ({"status": "success"}, 200),
    ({"status": "success", "presence": {"status": "ready"}}, 200),
    (ready(lastSeen="bad"), 200),
    ({"status": "success", "presence": {"status": "unsupported"}}, 200),
    ({"status": "success", "presence": {"status": "pending", "restricted": True}}, 200),
])
def test_failure_malformed_or_restricted_pending_is_not_alias_permission(payload, code):
    calls = []
    def request(target, timeout):
        calls.append(target)
        return response(payload, code)
    assert read_snapshot((PN, LID), request, lambda: 100) is None
    assert calls == [PN]


@pytest.mark.parametrize("phase", ["transport", "json"])
def test_exception_does_not_open_alternative_request(phase):
    calls = []
    def fail():
        raise TimeoutError("synthetic")
    def request(target, timeout):
        calls.append(target)
        if phase == "transport":
            fail()
        return SimpleNamespace(status_code=200, json=fail)
    assert read_snapshot((PN, LID), request, lambda: 100) is None
    assert calls == [PN]


def test_both_pending_stop_after_two_reads():
    calls = []
    def request(target, timeout):
        calls.append(target)
        return response(PENDING)
    assert read_snapshot((PN, LID, "789@lid"), request, lambda: 100) is None
    assert calls == [PN, LID]


@pytest.mark.parametrize("elapsed,expected_timeouts", [(3, [10, 7]), (10, [10]), (11, [10])])
def test_second_read_uses_remaining_timeout_or_is_skipped(elapsed, expected_timeouts):
    clock, timeouts = [100], []
    def request(target, timeout):
        timeouts.append(timeout)
        clock[0] += elapsed
        return response(PENDING if target == PN else ready())
    result = read_snapshot((PN, LID), request, lambda: clock[0])
    assert timeouts == expected_timeouts
    assert (result is not None) == (elapsed < 10)


@pytest.mark.parametrize("terminal", [ready(restricted=True, lastSeen=None), PENDING])
def test_second_response_cannot_leak_or_invent_a_timestamp(terminal):
    calls = []
    def request(target, timeout):
        calls.append(target)
        return response(PENDING if target == PN else terminal)
    result = read_snapshot((PN, LID), request, lambda: 100)
    assert result is None or (result["restricted"] and result["lastSeen"] is None)
    assert calls == [PN, LID]


def prepare_owner(owner, monkeypatch, on_alias=lambda: None):
    owner._lid_to_phone[LID], owner._phone_to_lid[PN] = PN, LID
    owner.wpp_server, owner.wpp_port, owner.token = "https://invalid.test", 1, "synthetic"
    owner._fetch_contact_presence = ContactPresenceMixin._fetch_contact_presence.__get__(owner)
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/123"):
            return response(PENDING)
        assert url.endswith("/456%40lid")
        on_alias()
        return response(ready())
    monkeypatch.setattr(coordinator, "api_get", get)
    return calls


@pytest.mark.parametrize("jid", [PN, LID, "123@c.us"])
def test_coordinator_preserves_encoding_auth_and_normalization(monkeypatch, jid):
    owner = Owner()
    calls = prepare_owner(owner, monkeypatch)
    assert owner._fetch_contact_presence(jid)["lastSeen"] == 1700000000
    assert [call[0].rsplit("/", 1)[1] for call in calls] == ["123", "456%40lid"]
    assert all(call[1]["headers"] == {"Authorization": "Bearer synthetic"} for call in calls)


def test_both_reads_keep_the_original_session_endpoint_and_credentials(monkeypatch):
    owner, calls = Owner(), []
    owner._lid_to_phone[LID], owner._phone_to_lid[PN] = PN, LID
    owner.wpp_server, owner.wpp_port, owner.token = "https://invalid.test", 1, "synthetic"
    def get(url, **kwargs):
        calls.append((url, dict(kwargs["headers"])))
        owner.wpp_server, owner.wpp_port, owner.token = "https://other.invalid", 2, "new-synthetic"
        return response(PENDING if len(calls) == 1 else ready())
    monkeypatch.setattr(coordinator, "api_get", get)
    assert owner._fetch_contact_presence(PN)["lastSeen"] == 1700000000
    assert calls == [
        ("https://invalid.test:1/api/synthetic/last-seen/123", {"Authorization": "Bearer synthetic"}),
        ("https://invalid.test:1/api/synthetic/last-seen/456%40lid", {"Authorization": "Bearer synthetic"}),
    ]


def test_fallback_merges_shared_alias_cache_in_one_worker(scheduled, monkeypatch):
    owner, workers, callbacks = scheduled
    calls = prepare_owner(owner, monkeypatch)
    owner._subscribed_presence_cache = {PN: 1}
    owner._refresh_open_contact_presence()
    owner._refresh_open_contact_presence()
    assert len(workers) == 1 and owner.subscriptions == []
    workers.pop()()
    assert len(callbacks) == 1 and len(calls) == 2
    callbacks.pop()()
    assert cached(owner, PN)["lastSeen"] == 1700000000
    assert owner._presence_cache[PN] is owner._presence_cache[LID]
    assert not owner._contact_presence_inflight
    owner._refresh_open_contact_presence()
    assert workers == []  # Existing ten-second coordinator cooldown remains.


def test_event_during_fallback_wins_over_older_http(scheduled, monkeypatch):
    owner, workers, callbacks = scheduled
    prepare_owner(owner, monkeypatch, lambda: merge(owner, LID, {"lastKnownPresence": "available"}))
    owner._refresh_open_contact_presence()
    workers.pop()()
    callbacks.pop()()
    assert cached(owner, PN)["lastKnownPresence"] == "available"
    assert cached(owner, PN)["lastSeen"] is None


@pytest.mark.parametrize("change", ["close", "reopen", "switch", "disconnect", "shutdown", "mapping", "reverse_mapping"])
def test_queued_alias_result_cannot_cross_visit_epoch_shutdown_or_identity(scheduled, monkeypatch, change):
    owner, workers, callbacks = scheduled
    prepare_owner(owner, monkeypatch)
    owner._refresh_open_contact_presence()
    workers.pop()()
    panel = owner.conversations_panel
    if change == "close":
        panel.conversation = None
    elif change == "reopen":
        panel._contact_presence_visit += 1
    elif change == "switch":
        panel.conversation = {"remoteJid": "789@s.whatsapp.net"}
    elif change == "disconnect":
        owner._wa_connected = False
        owner._invalidate_contact_presence()
    elif change == "shutdown":
        owner._shutting_down = True
    elif change == "mapping":
        owner._phone_to_lid[PN] = "789@lid"
    else:
        owner._lid_to_phone.pop(LID)
    callbacks.pop()()
    assert cached(owner, PN, fresh=False) == {}
    # Switching contacts immediately queues that contact's own fresh request.
    assert len(workers) == (1 if change == "switch" else 0)
    assert owner._contact_presence_inflight is (change == "switch")
