"""Serial, headless presence contracts: no App, socket or provider account."""
from types import SimpleNamespace

import pytest

from core.contact_presence import aliases, cached, merge, response_snapshot, timestamp
from main_window.contact_presence import ContactPresenceMixin
import main_window.contact_presence as coordinator
from ui.conversation_panel.contact_presence import ContactPresencePanelMixin
import ui.conversation_panel.contact_presence as ticker

PN = "123@s.whatsapp.net"
LID = "456@lid"


class Owner(ContactPresenceMixin):
    def __init__(self):
        self._presence_cache = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._wa_connected = True
        self.notes = []
        self.subscriptions = []
        self.conversations_panel = SimpleNamespace(
            conversation={"remoteJid": PN}, _contact_presence_visit=1,
            _refresh_presence_note=self.notes.append, IsShown=lambda: True)
        self.IsShown = lambda: True
        self.IsIconized = lambda: False
        self.IsActive = lambda: True
        self.subscribe_presence = self.subscriptions.append

    @staticmethod
    def _normalize_jid(jid):
        return jid.replace("@c.us", "@s.whatsapp.net")


@pytest.mark.parametrize("value,expected", [
    (None, None), (True, None), (False, None), (0, None), (-1, None),
    (float("nan"), None), (float("inf"), None), ({}, None), ([], None),
    ("bad", None), (1700000000, 1700000000),
    ("1700000000000", 1700000000), (1700000000123, 1700000000),
])
def test_timestamp_validation(value, expected):
    assert timestamp(value) == expected


def test_event_time_never_becomes_last_seen():
    owner = Owner()
    entry = merge(owner, PN, {"lastKnownPresence": "unavailable", "eventTimestamp": 1700000000000}, now=100)
    assert entry["lastSeen"] is None


@pytest.mark.parametrize("state", ["available", "composing", "recording"])
def test_online_transition_clears_old_last_seen(state):
    owner = Owner()
    merge(owner, PN, {"lastKnownPresence": "unavailable", "lastSeen": 1700000000}, now=100)
    assert merge(owner, PN, {"lastKnownPresence": state}, now=101)["lastSeen"] is None


def test_is_online_wins_over_paused_state():
    assert merge(Owner(), PN, {"lastKnownPresence": "paused", "isOnline": True})["lastKnownPresence"] == "available"


def test_old_last_seen_is_valid_when_newly_observed_but_expires_by_receipt_age():
    owner = Owner()
    merge(owner, PN, {"lastKnownPresence": "unavailable", "lastSeen": 1000}, now=100)
    assert cached(owner, PN, now=189)["lastSeen"] == 1000
    assert cached(owner, PN, now=190) == {}


def test_missing_field_does_not_refresh_last_seen_age():
    owner = Owner()
    merge(owner, PN, {"lastSeen": 1000}, now=100)
    merge(owner, PN, {"lastKnownPresence": "unavailable"}, now=180)
    assert cached(owner, PN, now=189)["lastSeen"] == 1000
    assert cached(owner, PN, now=190)["lastSeen"] is None


@pytest.mark.parametrize("update", [{"lastSeen": None}, {"lastSeen": False}, {"restricted": True}])
def test_explicit_withholding_clears_previous_timestamp(update):
    owner = Owner()
    merge(owner, PN, {"lastSeen": 1000}, now=100)
    assert merge(owner, PN, update, now=101)["lastSeen"] is None


def test_mapping_arriving_after_lid_event_retains_shared_snapshot():
    owner = Owner()
    entry = merge(owner, LID, {"lastSeen": 1000}, now=100)
    owner._lid_to_phone[LID] = PN
    owner._phone_to_lid[PN] = LID
    assert cached(owner, PN, now=101) is entry
    entry = merge(owner, PN, {"lastKnownPresence": "available"}, now=102)
    assert owner._presence_cache[PN] is owner._presence_cache[LID] is entry
    assert aliases(owner, "123@c.us") == (PN, LID)


def test_older_event_cannot_replace_newer_state():
    owner = Owner()
    entry = merge(owner, PN, {"lastKnownPresence": "available", "eventTimestamp": 1700000002000}, now=100)
    assert merge(owner, PN, {"lastKnownPresence": "unavailable", "eventTimestamp": 1700000001000}, now=101) is entry


@pytest.mark.parametrize("payload,expected", [
    ({}, None), ({"status": "error", "response": 1000}, None),
    ({"status": "success"}, None),
    ({"status": "success", "response": True}, None),
    ({"status": "success", "response": "bad"}, None),
    ({"status": "success", "presence": {"status": "pending"}}, None),
    ({"status": "success", "presence": {"status": "unsupported"}}, None),
])
def test_unknown_response_is_not_privacy_denial(payload, expected):
    assert response_snapshot(payload) == expected


@pytest.mark.parametrize("value,expected", [(False, None), (None, None), (1000, 1000), ({"t": 1000}, 1000), ({"lastSeen": 2000, "t": 1000}, 2000)])
def test_legacy_api_responses(value, expected):
    assert response_snapshot({"status": "success", "response": value})["lastSeen"] == expected


@pytest.fixture
def scheduled(monkeypatch):
    workers, callbacks = [], []
    class Thread:
        def __init__(self, target, daemon):
            self.target = target
        def start(self):
            workers.append(self.target)
    monkeypatch.setattr(coordinator.threading, "Thread", Thread)
    monkeypatch.setattr(coordinator.wx, "CallAfter", lambda fn, *a, **kw: callbacks.append(lambda: fn(*a, **kw)))
    monkeypatch.setattr(coordinator.time, "monotonic", lambda: 100)
    owner = Owner()
    owner._fetch_contact_presence = lambda jid: {"lastKnownPresence": "unavailable", "lastSeen": 1000}
    return owner, workers, callbacks


def test_live_event_wins_over_slow_query(scheduled):
    owner, workers, callbacks = scheduled
    owner._refresh_open_contact_presence()
    workers.pop()()
    merge(owner, PN, {"lastKnownPresence": "available"})
    callbacks.pop()()
    assert cached(owner, PN)["lastKnownPresence"] == "available"
    assert cached(owner, PN)["lastSeen"] is None


def test_new_alias_snapshot_wins_even_with_same_per_alias_version(scheduled):
    owner, workers, callbacks = scheduled
    owner.conversations_panel.conversation = {"remoteJid": LID}
    merge(owner, LID, {"lastKnownPresence": "unavailable", "lastSeen": 1000}, now=90)
    owner._refresh_open_contact_presence()
    workers.pop()()
    merge(owner, PN, {"lastKnownPresence": "available"}, now=100)
    owner._lid_to_phone[LID] = PN
    owner._phone_to_lid[PN] = LID
    callbacks.pop()()
    assert cached(owner, PN)["lastKnownPresence"] == "available"


def test_event_order_preserves_millisecond_precision():
    owner = Owner()
    entry = merge(owner, PN, {"lastKnownPresence": "available", "eventTimestamp": 1700000000123}, now=100)
    assert merge(owner, PN, {"lastKnownPresence": "unavailable", "eventTimestamp": 1700000000122}, now=101) is entry


def test_event_order_accepts_legacy_seconds_after_milliseconds():
    owner = Owner()
    merge(owner, PN, {"lastKnownPresence": "available", "eventTimestamp": 1700000000123}, now=100)
    assert merge(owner, PN, {"lastKnownPresence": "unavailable", "eventTimestamp": 1700000001}, now=101)["lastKnownPresence"] == "unavailable"


@pytest.mark.parametrize("change", ["close", "reopen", "switch", "disconnect"])
def test_old_visit_or_connection_cannot_accept_response(scheduled, change):
    owner, workers, callbacks = scheduled
    owner._refresh_open_contact_presence()
    workers.pop()()
    panel = owner.conversations_panel
    if change == "close":
        panel.conversation = None
    elif change == "reopen":
        panel._contact_presence_visit += 1
    elif change == "switch":
        panel.conversation = {"remoteJid": "789@s.whatsapp.net"}
    else:
        owner._wa_connected = False
        owner._invalidate_contact_presence()
    callbacks.pop()()
    assert cached(owner, PN, fresh=False) == {}


def test_requests_are_single_flight_and_retry_after_cooldown(scheduled, monkeypatch):
    owner, workers, callbacks = scheduled
    owner._fetch_contact_presence = lambda jid: None
    for _ in range(10):
        owner._refresh_open_contact_presence()
    assert len(workers) == len(owner.subscriptions) == 1
    workers.pop()()
    callbacks.pop()()
    owner._refresh_open_contact_presence()
    assert workers == []
    monkeypatch.setattr(coordinator.time, "monotonic", lambda: 111)
    owner._refresh_open_contact_presence()
    assert len(workers) == 1


def test_worker_exception_releases_single_flight(scheduled):
    owner, workers, callbacks = scheduled
    def fail(jid):
        raise ValueError("synthetic")
    owner._fetch_contact_presence = fail
    owner._refresh_open_contact_presence()
    workers.pop()()
    callbacks.pop()()
    assert owner._contact_presence_inflight is False


def test_reconnect_during_request_queues_a_fresh_request(scheduled):
    owner, workers, callbacks = scheduled
    owner._refresh_open_contact_presence()
    workers.pop()()
    owner._invalidate_contact_presence()
    callbacks.pop()()
    assert cached(owner, PN, fresh=False) == {}
    assert len(workers) == 1


def test_shutdown_neither_queries_nor_updates_widgets(scheduled):
    owner, workers, _ = scheduled
    owner._shutting_down = True
    owner._refresh_open_contact_presence()
    owner._refresh_open_contact_presence_note()
    assert owner.notes == workers == []


@pytest.mark.parametrize("gate", ["hidden", "minimized", "inactive", "tray", "offline", "group", "newsletter"])
def test_non_visible_or_non_contact_chat_never_queries(scheduled, gate):
    owner, workers, _ = scheduled
    if gate == "hidden":
        owner.conversations_panel.IsShown = lambda: False
    elif gate == "minimized":
        owner.IsIconized = lambda: True
    elif gate == "inactive":
        owner.IsActive = lambda: False
    elif gate == "tray":
        owner._window_hidden = True
    elif gate == "offline":
        owner._wa_connected = False
    else:
        owner.conversations_panel.conversation = {"remoteJid": "123@" + ("g.us" if gate == "group" else "newsletter")}
    owner._refresh_open_contact_presence()
    assert workers == owner.subscriptions == []


def test_http_query_preserves_unmapped_lid_and_uses_no_real_connection(monkeypatch):
    owner = Owner()
    owner.wpp_server, owner.wpp_port, owner.token = "http://invalid.test", 1, "synthetic"
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"status": "success", "response": 1000})
    monkeypatch.setattr(coordinator, "api_get", get)
    assert owner._fetch_contact_presence(LID)["lastSeen"] == 1000
    assert calls[0][0].endswith("/456%40lid")
    assert calls[0][1]["timeout"] == 10
    assert owner._fetch_contact_presence("123@g.us") is None
    assert len(calls) == 1


@pytest.mark.parametrize("status,payload", [(401, {}), (403, {}), (500, {}), (200, None), (200, []), (200, {"status": "error"}), (200, {"status": "success", "presence": {"status": "pending"}})])
def test_http_fault_or_pending_does_not_claim_privacy_denial(monkeypatch, status, payload):
    owner = Owner()
    owner.wpp_server, owner.wpp_port, owner.token = "http://invalid.test", 1, "synthetic"
    monkeypatch.setattr(coordinator, "api_get", lambda *a, **kw: SimpleNamespace(status_code=status, json=lambda: payload))
    assert owner._fetch_contact_presence(PN) is None


def test_http_exception_is_unknown_without_overwriting_cache(monkeypatch):
    owner = Owner()
    owner.wpp_server, owner.wpp_port, owner.token = "http://invalid.test", 1, "synthetic"
    merge(owner, PN, {"lastSeen": 1000})
    def fail(*a, **kw):
        raise TimeoutError("synthetic")
    monkeypatch.setattr(coordinator, "api_get", fail)
    assert owner._fetch_contact_presence(PN) is None
    assert cached(owner, PN)["lastSeen"] == 1000


def test_timer_stops_on_close_and_old_tick_cannot_restart(monkeypatch):
    queued, timers = [], []
    monkeypatch.setattr(ticker.wx, "CallAfter", lambda fn: queued.append(fn))
    class Timer:
        def __init__(self, delay, fn):
            assert delay == 30000
            self.fn, self.stopped = fn, False
            timers.append(self)
        def Stop(self):
            self.stopped = True
    monkeypatch.setattr(ticker.wx, "CallLater", Timer)
    calls = []
    panel = ContactPresencePanelMixin()
    panel.conversation = {"remoteJid": PN}
    panel.main_window = SimpleNamespace(_refresh_open_contact_presence=lambda: calls.append(True))
    panel._start_contact_presence()
    queued.pop()()
    panel._stop_contact_presence()
    timers[0].fn()
    assert timers[0].stopped and calls == [True] and len(timers) == 1
