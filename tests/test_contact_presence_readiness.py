"""Subscription completion precedes the first read; pending gets one follow-up."""
from types import MethodType, SimpleNamespace

import pytest

from core.contact_presence import cached
from main_window.identity import IdentityMixin
import main_window.identity as identity
import main_window.contact_presence as coordinator
from tests.test_contact_presence_deferred_refresh import Clockwork
from tests.test_contact_presence_freshness import PN, LID
from core.contact_presence import merge
from core.contact_presence_refresh import take_readiness_followup


@pytest.fixture
def loop(monkeypatch):
    loop = Clockwork(monkeypatch)
    owner = loop.owner
    owner._subscribed_presence_cache = {}
    owner.subscribe_presence = MethodType(IdentityMixin.subscribe_presence, owner)
    del owner._fetch_contact_presence
    loop.payload = {"status": "success", "presence": {"status": "pending"}}
    loop.posts, loop.post_status = [], 200
    monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: loop.now, monotonic=lambda: loop.now))
    monkeypatch.setattr(identity, "wx", SimpleNamespace(CallAfter=lambda fn: loop.callbacks.append(fn)))
    monkeypatch.setattr(identity, "api_post", lambda *a, **kw:
                        loop.posts.append(loop.now) or SimpleNamespace(
                            status_code=loop.post_status, json=lambda: {"status": "success"}))
    monkeypatch.setattr(coordinator, "api_get", lambda *a, **kw:
                        loop.reads.append(loop.now) or SimpleNamespace(
                            status_code=200, json=lambda: loop.payload))
    return loop


def test_first_snapshot_waits_for_subscription_completion(loop):
    loop.owner._refresh_open_contact_presence()
    assert len(loop.workers) == 1  # POST worker only; no competing GET worker.
    assert loop.owner._contact_presence_deferred.pending is not None


def test_confirmed_subscription_pending_snapshot_has_one_short_followup(loop):
    loop.begin()
    assert loop.reads == [100]
    loop.payload = {"status": "success", "presence": {
        "status": "ready", "isOnline": False, "lastSeen": 1700000000}}
    loop.advance(110)
    assert loop.reads == [100, 110]
    assert cached(loop.owner, PN)["lastSeen"] == 1700000000
    loop.advance(1000)
    assert loop.posts == [100] and len(loop.reads) == 2


def test_repeated_pending_stops_after_one_continuation(loop):
    loop.begin()
    loop.advance(1000)
    assert loop.reads == [100, 110] and loop.posts == [100]
    assert not any(timer.active for timer in loop.timers)


@pytest.mark.parametrize("payload", [
    {"status": "success", "presence": {"status": "ready", "isOnline": False, "lastSeen": None}},
    {"status": "success", "presence": {"status": "ready", "isOnline": False, "lastSeen": None, "restricted": True}},
    {"status": "error"}, {"status": "success", "presence": {"status": "error"}},
    {"status": "success", "presence": {"status": "unsupported"}},
    {"status": "success", "presence": {"status": "pending", "restricted": "false"}},
    None,
])
def test_nonpending_result_has_no_readiness_followup(loop, payload):
    loop.payload = payload
    loop.begin()
    loop.advance(1000)
    assert loop.reads == [100] and loop.posts == [100]


def test_failed_subscription_cannot_create_completion_retry_loop(loop):
    loop.post_status = 500
    loop.begin()
    loop.advance(130)
    assert loop.posts == [100] and loop.reads == []
    loop.begin()  # Ordinary later request may retry the failed initial POST.
    loop.advance(1000)
    assert loop.posts == [100, 130] and loop.reads == []


def test_inflight_requests_coalesce_and_first_read_is_not_delayed_by_get_throttle(loop):
    loop.owner._refresh_open_contact_presence()
    for moment in (101, 102, 105, 109):
        loop.now = moment
        loop.owner._refresh_open_contact_presence()
    assert len(loop.workers) == 1
    assert loop.reads == []
    assert len(loop.timers) == 1 and loop.timers[0].at == 130  # POST deadline only.
    loop.drain()
    assert loop.reads == [109]
    loop.advance(119)
    assert loop.reads == [109, 119]


@pytest.mark.parametrize("change", ["close", "visit", "other_chat", "epoch", "mapping", "hidden", "inactive", "disconnect", "shutdown", "token"])
def test_subscription_completion_cannot_start_a_read_in_stale_or_inactive_scope(loop, change):
    owner = loop.owner
    owner._refresh_open_contact_presence()
    if change == "close":
        owner.conversations_panel.conversation = None
    elif change == "visit":
        owner.conversations_panel._contact_presence_visit += 1
    elif change == "other_chat":
        owner.conversations_panel.conversation = {"remoteJid": "789@s.whatsapp.net"}
    elif change == "epoch":
        owner._contact_presence_epoch = 1
    elif change == "mapping":
        owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    elif change == "hidden":
        owner._window_hidden = True
    elif change == "inactive":
        loop.flags["active"] = False
    elif change == "disconnect":
        owner._wa_connected = False
    elif change == "token":
        owner.token = "replacement"
    else:
        owner._shutting_down = True
    loop.drain()
    loop.advance(1000)
    assert loop.reads == []


def test_routing_change_after_post_completion_rejects_queued_callback(loop):
    loop.owner._refresh_open_contact_presence()
    loop.workers.pop(0)()
    assert loop.callbacks
    loop.owner.token = "replacement"
    loop.drain()
    assert loop.reads == []


@pytest.mark.parametrize("update", [
    {"restricted": True}, {"lastKnownPresence": "available", "isOnline": True},
    {"lastKnownPresence": "composing", "isOnline": False}, {"lastSeen": 1700000000},
])
def test_push_data_can_satisfy_waiting_read_before_post_completes(loop, update):
    loop.owner._refresh_open_contact_presence()
    merge(loop.owner, PN, update)
    loop.drain()
    loop.advance(1000)
    assert loop.reads == []


@pytest.mark.parametrize("change", ["visit", "hidden", "disconnect", "mapping", "cancel"])
def test_short_followup_rechecks_scope_and_visibility(loop, change):
    loop.begin()
    owner = loop.owner
    if change == "visit":
        owner.conversations_panel._contact_presence_visit += 1
    elif change == "hidden":
        owner._window_hidden = True
    elif change == "disconnect":
        owner._wa_connected = False
    elif change == "mapping":
        owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    else:
        owner._cancel_deferred_contact_presence()
    loop.advance(1000)
    assert loop.reads == [100]


def test_late_alias_mapping_does_not_refund_readiness_allowance(loop):
    owner = loop.owner
    owner._contact_presence_readiness_candidates = {PN, LID}
    assert take_readiness_followup(owner, (PN,))
    assert not take_readiness_followup(owner, (PN, LID))
    assert not take_readiness_followup(owner, (LID, PN))


def test_connection_invalidation_resets_readiness_and_rejects_old_worker_state(loop, monkeypatch):
    owner = loop.owner
    monkeypatch.setattr(coordinator, "notify_personal_presence", lambda *a: None)
    owner._refresh_open_contact_presence()
    old_confirmed = owner._presence_subscription_confirmed
    owner._invalidate_contact_presence()
    loop.drain()
    assert old_confirmed is not owner._presence_subscription_confirmed
    assert owner._presence_subscription_confirmed == set()
    assert owner._contact_presence_readiness_used == set()
    assert loop.reads == []
    loop.now = 130
    loop.begin()
    loop.advance(140)
    assert loop.reads == [130, 140]


def test_ready_timestamp_ends_wait_without_extra_http_read(loop):
    loop.payload = {"status": "success", "presence": {
        "status": "ready", "isOnline": False, "lastSeen": 1700000000}}
    loop.begin()
    loop.advance(1000)
    assert loop.reads == [100]
