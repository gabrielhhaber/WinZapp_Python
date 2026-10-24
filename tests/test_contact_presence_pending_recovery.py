"""Persistent pending may renew a subscription only within a connection budget.

All requests, workers, UI callbacks and clocks are fake; no account or wx.App.
"""
from types import MethodType, SimpleNamespace

import pytest

from core.contact_presence import cached, merge
from core.contact_presence_query import read_presence, read_snapshot
from core.contact_presence_recovery import PendingPresenceRecovery
from main_window.identity import IdentityMixin
import main_window.identity as identity
import main_window.contact_presence as coordinator
from tests.test_contact_presence_deferred_refresh import Clockwork
from tests.test_contact_presence_freshness import Owner, PN, LID

PENDING = {"status": "success", "presence": {"status": "pending"}}


def response(payload, status=200):
    return SimpleNamespace(status_code=status, json=lambda: payload)


def ready(**fields):
    return {"status": "success", "presence": {
        "status": "ready", "isOnline": False, "lastSeen": None, **fields}}


@pytest.fixture
def pipeline(monkeypatch):
    loop = Clockwork(monkeypatch)
    owner = loop.owner
    owner.subscribe_presence = MethodType(IdentityMixin.subscribe_presence, owner)
    del owner._fetch_contact_presence
    loop.payload, loop.http_status, loop.post_status = PENDING, 200, 200
    loop.posts = []

    def get(*args, **kwargs):
        loop.reads.append((loop.now, args, kwargs))
        return response(loop.payload, loop.http_status)

    def post(*args, **kwargs):
        loop.posts.append((loop.now, args, kwargs))
        return response({"status": "success"}, loop.post_status)

    monkeypatch.setattr(coordinator, "api_get", get)
    monkeypatch.setattr(identity, "api_post", post)
    monkeypatch.setattr(identity, "time", SimpleNamespace(
        time=lambda: loop.now, monotonic=lambda: loop.now))
    return loop


def test_persistent_pending_gets_only_two_bounded_recoveries(monkeypatch):
    loop = Clockwork(monkeypatch)
    owner = loop.owner
    owner.wpp_server, owner.wpp_port, owner.token = "http://invalid.test", 1, "synthetic"
    owner.subscribe_presence = MethodType(IdentityMixin.subscribe_presence, owner)
    del owner._fetch_contact_presence
    monkeypatch.setattr(coordinator, "api_get", lambda *a, **kw:
                        loop.reads.append(loop.now) or SimpleNamespace(
                            status_code=200, json=lambda: {
                                "status": "success", "presence": {"status": "pending"}}))
    posts = []
    monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: loop.now, monotonic=lambda: loop.now))
    monkeypatch.setattr(identity, "api_post", lambda *a, **kw:
                        posts.append(loop.now) or SimpleNamespace(
                            status_code=200, json=lambda: {"status": "success"}))
    for moment in range(100, 1001, 30):
        loop.advance(moment)
        loop.begin()
    assert posts == [190, 310]
    assert owner._subscribed_presence_cache[PN] == 310
    assert len(loop.reads) == 31


@pytest.mark.parametrize("payload,outcome", [
    (ready(lastSeen=1700000000), "ready"),
    (ready(isOnline=True), "ready"),
    (ready(state="composing"), "ready"),
    (ready(state="recording"), "ready"),
    (ready(), "withheld"),
    (ready(restricted=True), "restricted"),
    ({"status": "success", "response": None}, "withheld"),
    ({"status": "success", "response": False}, "withheld"),
    ({"status": "success", "response": 1700000000}, "ready"),
    (PENDING, "pending"),
    ({"status": "success", "presence": {"status": "pending", "restricted": False}}, "pending"),
    ({"status": "success", "presence": {"status": "pending", "restricted": True}}, "restricted"),
    ({"status": "success", "presence": {"status": "pending", "restricted": None}}, "malformed"),
    ({"status": "success", "presence": {"status": "pending", "isOnline": 0}}, "malformed"),
    ({"status": "success", "presence": {"status": "pending", "isOnline": True}}, "malformed"),
    ({"status": "success", "presence": {"status": "pending", "state": "available"}}, "malformed"),
    ({"status": "success", "presence": {"status": "pending", "lastSeen": 1700000000}}, "malformed"),
    ({"status": "success", "presence": {"status": "unsupported"}}, "unsupported"),
    ({"status": "success", "presence": {"status": "other"}}, "malformed"),
    ({"status": "success", "presence": None, "response": 1700000000}, "malformed"),
    (ready(isOnline=None), "malformed"),
    (ready(restricted=0), "malformed"),
    (ready(state={}), "malformed"),
    (ready(lastSeen="bad"), "malformed"),
    ({"status": "success"}, "malformed"),
    ({"status": "error"}, "error"),
    ([], "malformed"),
])
def test_outcomes_remain_distinct_without_enabling_terminal_aliases(payload, outcome):
    calls = []
    def request(target, timeout):
        calls.append((target, timeout))
        return response(payload)
    result = read_presence((PN, LID), request, lambda: 100)
    assert result.outcome == outcome
    assert len(calls) == (2 if outcome == "pending" else 1)
    assert read_snapshot((PN, LID), request, lambda: 100) == result.snapshot


@pytest.mark.parametrize("payload,outcome", [
    (PENDING, "pending"), (ready(), "withheld"), (ready(restricted=True), "restricted"),
    (ready(lastSeen=1700000000), "ready"), ({"status": "error"}, "error"),
    ({"status": "success", "presence": {"status": "unsupported"}}, "unsupported"),
    (None, "malformed"),
])
def test_terminal_second_identity_overrides_first_pending(payload, outcome):
    calls = []
    def request(target, timeout):
        calls.append(target)
        return response(PENDING if target == PN else payload)
    assert read_presence((PN, LID), request, lambda: 100).outcome == outcome
    assert calls == [PN, LID]


@pytest.mark.parametrize("failure", ["http", "json", "request", "budget"])
def test_failures_are_not_pending(failure):
    now, calls = [100], []
    def request(target, timeout):
        calls.append(target)
        if failure == "request":
            raise TimeoutError("synthetic")
        if failure == "json":
            return SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError()))
        if failure == "budget":
            now[0] += 10
            return response(PENDING)
        return response(PENDING, 500)
    assert read_presence((PN, LID), request, lambda: now[0]).outcome == "error"
    assert calls == [PN]


def observe_for(budget, targets=(PN,), start=100, revision=None):
    for now in (start, start + 30, start + 60, start + 90):
        budget.observe(targets, "pending", now, revision)


def test_threshold_interval_and_total_budget_are_independent():
    budget = PendingPresenceRecovery()
    observe_for(budget)
    assert not budget.claim((PN,), 189.999)
    assert not budget.claim((PN,), 190, inflight=True)
    assert budget.claim((PN,), 190)
    assert not budget.claim((PN,), 190)
    observe_for(budget, start=200)
    assert not budget.claim((PN,), 309.999)
    assert budget.claim((PN,), 310)
    observe_for(budget, start=320)
    assert not budget.claim((PN,), 410)


@pytest.mark.parametrize("outcome", ["ready", "withheld", "restricted", "error", "malformed", "unsupported"])
def test_every_nonpending_outcome_breaks_the_streak(outcome):
    budget = PendingPresenceRecovery()
    observe_for(budget)
    budget.observe((PN,), outcome, 191, None)
    assert not budget.claim((PN,), 191)
    for moment in (200, 230, 260):
        budget.observe((PN,), "pending", moment, None)
    assert not budget.claim((PN,), 260)
    budget.observe((PN,), "pending", 290, None)
    assert budget.claim((PN,), 290)


@pytest.mark.parametrize("change", ["gap", "new_event", "clock_reversal"])
def test_lack_of_continuous_matching_evidence_restarts_observation(change):
    budget = PendingPresenceRecovery()
    revision = object()
    observe_for(budget, revision=revision)
    now = 236 if change == "gap" else 180 if change == "clock_reversal" else 200
    budget.observe((PN,), "pending", now, object() if change == "new_event" else revision)
    assert not budget.claim((PN,), now)


def test_late_mapping_combines_used_budgets_without_minting_attempts():
    budget = PendingPresenceRecovery()
    for target in (PN, LID):
        observe_for(budget, (target,))
        assert budget.claim((target,), 190)
    observe_for(budget, (PN, LID), start=400)
    assert not budget.claim((PN, LID), 490)
    assert budget.has_recovered((PN,)) and budget.has_recovered((LID,))


@pytest.mark.parametrize("payload", [ready(), ready(restricted=True),
    {"status": "success", "response": None}, {"status": "error"},
    {"status": "success", "presence": {"status": "unsupported"}},
    {"status": "success", "presence": {"status": "pending", "restricted": 0}},
    {"status": "success", "presence": {"status": "pending", "isOnline": 0}},
    ready(state=[]), None,
])
def test_terminal_or_invalid_data_never_renews_subscription(pipeline, payload):
    pipeline.payload = payload
    for moment in range(100, 1001, 30):
        pipeline.advance(moment)
        pipeline.begin()
    assert pipeline.posts == []


@pytest.mark.parametrize("post_status", [200, 500])
def test_reopen_and_failed_post_cannot_bypass_recovery_budget(pipeline, post_status):
    pipeline.post_status = post_status
    owner = pipeline.owner
    for moment in range(100, 1001, 30):
        pipeline.advance(moment)
        owner.conversations_panel._contact_presence_visit += 1
        if pipeline.posts:
            owner.subscribe_presence(PN)  # Real navigation path, not the coordinator.
        pipeline.begin()
    assert [p[0] for p in pipeline.posts] == [190, 310]


def test_inflight_subscription_defers_recovery_without_spending_budget(pipeline):
    owner = pipeline.owner
    owner._presence_subscription_inflight = {PN}
    for moment in range(100, 221, 30):
        pipeline.advance(moment)
        pipeline.begin()
    assert pipeline.posts == []
    owner._presence_subscription_inflight.clear()
    pipeline.advance(250)
    pipeline.begin()
    # Initial reads now wait for POST completion; no pending evidence existed
    # during the simulated in-flight POST, so its 90-second window starts here.
    assert pipeline.posts == []
    for moment in (280, 310, 340):
        pipeline.advance(moment)
        pipeline.begin()
    assert [p[0] for p in pipeline.posts] == [340]


@pytest.mark.parametrize("change", ["hidden", "inactive", "iconized", "visit", "epoch", "mapping", "shutdown", "event"])
def test_pending_delivery_cannot_recover_stale_or_ineligible_scope(pipeline, change):
    owner = pipeline.owner
    for moment in (100, 130, 160):
        pipeline.advance(moment)
        pipeline.begin()
    pipeline.advance(190)
    owner._refresh_open_contact_presence()
    pipeline.workers.pop(0)()
    if change == "hidden":
        owner._window_hidden = True
    elif change == "inactive":
        pipeline.flags["active"] = False
    elif change == "iconized":
        pipeline.flags["iconized"] = True
    elif change == "visit":
        owner.conversations_panel._contact_presence_visit += 1
    elif change == "epoch":
        owner._contact_presence_epoch = 1
    elif change == "mapping":
        owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    elif change == "shutdown":
        owner._shutting_down = True
    else:
        merge(owner, PN, {"lastKnownPresence": "unavailable", "isOnline": False})
    pipeline.drain()
    assert pipeline.posts == []


def test_valid_data_breaks_pending_even_when_it_has_no_timestamp(pipeline):
    for moment in (100, 130, 160, 190, 220, 250, 280, 310):
        pipeline.payload = ready() if moment == 190 else PENDING
        pipeline.advance(moment)
        pipeline.begin()
    assert [p[0] for p in pipeline.posts] == [310]


def test_new_cache_revision_requires_a_new_pending_observation_window(pipeline):
    for moment in (100, 130, 160, 190, 220, 250, 280):
        pipeline.advance(moment)
        if moment == 190:
            merge(pipeline.owner, PN, {"lastKnownPresence": "unavailable", "isOnline": False})
        pipeline.begin()
    assert [p[0] for p in pipeline.posts] == [280]


def test_recovery_uses_only_known_pair_and_stays_bounded_per_identity(pipeline):
    owner = pipeline.owner
    owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    for moment in range(100, 1001, 30):
        pipeline.advance(moment)
        pipeline.begin()
    assert sorted((p[0], p[2]["json"]["phone"]) for p in pipeline.posts) == [
        (190, "123@c.us"), (190, LID), (310, "123@c.us"), (310, LID)]
    assert len(pipeline.reads) == 62


def test_connection_invalidation_resets_budget_and_old_inflight_registry(pipeline, monkeypatch):
    owner = pipeline.owner
    monkeypatch.setattr(coordinator, "notify_personal_presence", lambda *a: None)
    for moment in range(100, 311, 30):
        pipeline.advance(moment)
        pipeline.begin()
    old_budget = owner._contact_presence_recovery
    owner._invalidate_contact_presence()
    assert owner._contact_presence_recovery is not old_budget
    for moment in range(400, 611, 30):
        pipeline.advance(moment)
        pipeline.begin()
    assert [p[0] for p in pipeline.posts] == [190, 310, 400, 490, 610]


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_recovery_worker_start_failure_consumes_budget_without_normal_retry(pipeline, monkeypatch, phase):
    class Broken:
        def __init__(self, target, daemon):
            if phase == "construct":
                raise RuntimeError("synthetic")
        def start(self):
            raise RuntimeError("synthetic")
    original = identity.threading
    for moment in (100, 130, 160):
        pipeline.advance(moment)
        pipeline.begin()
    monkeypatch.setattr(identity, "threading", SimpleNamespace(Thread=Broken))
    pipeline.advance(190)
    with pytest.raises(RuntimeError, match="synthetic"):
        pipeline.begin()
    assert pipeline.owner._presence_subscription_inflight == set()
    assert PN not in pipeline.owner._subscribed_presence_cache
    monkeypatch.setattr(identity, "threading", original)
    for moment in (220, 250, 280, 310):
        pipeline.advance(moment)
        pipeline.begin()
    assert [p[0] for p in pipeline.posts] == [310]


@pytest.mark.parametrize("change", ["epoch", "cache", "token", "server", "shutdown", "disconnected"])
def test_old_subscription_worker_cannot_touch_replacement_connection(monkeypatch, change):
    loop = Clockwork(monkeypatch)
    owner = loop.owner
    owner._subscribed_presence_cache = {}
    owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    posts = []
    monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: loop.now))
    monkeypatch.setattr(identity, "api_post", lambda *a, **kw: posts.append((a, kw)) or response({"status": "success"}))
    IdentityMixin.subscribe_presence(owner, PN)
    old_cache = owner._subscribed_presence_cache
    old_inflight = owner._presence_subscription_inflight
    if change == "epoch":
        owner._contact_presence_epoch = 1
    elif change == "cache":
        owner._subscribed_presence_cache = {PN: 777}
    elif change == "token":
        owner.token = "replacement"
    elif change == "server":
        owner.wpp_server = "http://replacement.invalid"
    elif change == "shutdown":
        owner._shutting_down = True
    else:
        owner._wa_connected = False
    loop.drain()
    assert posts == [] and old_cache == {} and old_inflight == set()
    if change == "cache":
        assert owner._subscribed_presence_cache == {PN: 777}


def test_slow_subscription_cannot_be_duplicated_after_throttle_interval(monkeypatch):
    loop = Clockwork(monkeypatch)
    owner = loop.owner
    owner._subscribed_presence_cache = {}
    monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: loop.now))
    IdentityMixin.subscribe_presence(owner, PN)
    loop.now = 120
    IdentityMixin.subscribe_presence(owner, PN)
    assert len(loop.workers) == 1


def test_subscription_failure_logs_neither_identity_nor_raw_exception(monkeypatch, caplog):
    loop = Clockwork(monkeypatch)
    owner = loop.owner
    owner._subscribed_presence_cache = {}
    monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: loop.now))
    def post(*a, **kw):
        raise RuntimeError("SYNTHETIC_SECRET")
    monkeypatch.setattr(identity, "api_post", post)
    IdentityMixin.subscribe_presence(owner, PN)
    loop.drain()
    assert "Request failed" in caplog.text
    assert "SYNTHETIC_SECRET" not in caplog.text and "123" not in caplog.text


def test_worker_exception_breaks_pending_streak(pipeline, monkeypatch):
    original = coordinator.api_get
    for moment in (100, 130, 160, 190, 220, 250, 280, 310):
        pipeline.advance(moment)
        if moment == 190:
            def fail(*a, **kw):
                raise TimeoutError("synthetic")
            monkeypatch.setattr(coordinator, "api_get", fail)
        else:
            monkeypatch.setattr(coordinator, "api_get", original)
        pipeline.begin()
    assert [p[0] for p in pipeline.posts] == [310]


def test_expired_denial_still_prevents_pending_recovery(pipeline):
    merge(pipeline.owner, PN, {"restricted": True}, now=0)
    for moment in range(100, 1001, 30):
        pipeline.advance(moment)
        pipeline.begin()
    assert cached(pipeline.owner, PN) == {}
    assert pipeline.posts == []


def test_repeated_direct_recovery_requests_cannot_reuse_a_spent_claim(pipeline):
    owner = pipeline.owner
    for moment in range(100, 191, 30):
        pipeline.advance(moment)
        pipeline.begin()
    for _ in range(100):
        owner.subscribe_presence(PN, recovery=True)
    pipeline.drain()
    assert [p[0] for p in pipeline.posts] == [190]


def test_account_change_during_first_post_stops_second_identity(monkeypatch):
    loop = Clockwork(monkeypatch)
    owner = loop.owner
    owner._subscribed_presence_cache = {}
    owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    posts = []
    monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: loop.now))
    def post(*args, **kwargs):
        posts.append((args, kwargs))
        owner.token = "replacement"
        owner._subscribed_presence_cache = {PN: 777}
        owner._presence_subscription_inflight = {PN}
        return response({"status": "error"})
    monkeypatch.setattr(identity, "api_post", post)
    IdentityMixin.subscribe_presence(owner, PN)
    loop.drain()
    assert len(posts) == 1
    assert "/synthetic/subscribe-presence" in posts[0][0][0]
    assert posts[0][1]["headers"]["Authorization"] == "Bearer synthetic"
    assert owner._subscribed_presence_cache == {PN: 777}
    assert owner._presence_subscription_inflight == {PN}


def test_terminal_alias_error_does_not_leave_first_pending_as_a_recovery_reason(pipeline, monkeypatch):
    owner = pipeline.owner
    owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    def get(url, **kwargs):
        return response({"status": "error"} if url.endswith("%40lid") else PENDING)
    monkeypatch.setattr(coordinator, "api_get", get)
    for moment in range(100, 1001, 30):
        pipeline.advance(moment)
        pipeline.begin()
    assert pipeline.posts == []


def test_recovery_interval_is_independent_of_wall_clock_adjustment(pipeline, monkeypatch):
    monkeypatch.setattr(identity, "time", SimpleNamespace(
        time=lambda: -500, monotonic=lambda: pipeline.now))
    for moment in range(100, 1001, 30):
        pipeline.advance(moment)
        pipeline.begin()
    assert [p[0] for p in pipeline.posts] == [190, 310]
