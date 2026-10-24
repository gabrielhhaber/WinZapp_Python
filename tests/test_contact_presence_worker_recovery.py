"""Never-completing fake workers cannot own the UI lease indefinitely."""
import pytest
from types import SimpleNamespace

from tests.test_contact_presence_deferred_refresh import Clockwork
from tests.test_contact_presence_readiness import loop as subscription_loop
from tests.test_contact_presence_freshness import PN, LID
from core.contact_presence import cached, merge
from core.contact_presence_work import PresenceWork
import main_window.contact_presence as coordinator
import main_window.identity as identity


def callbacks_only(loop):
    def drain():
        for _ in range(100):
            if not loop.callbacks:
                return
            loop.callbacks.pop(0)()
        pytest.fail("Unbounded UI continuation")
    loop.drain = drain
    return loop


@pytest.fixture
def loop(monkeypatch):
    return callbacks_only(Clockwork(monkeypatch))


def test_stuck_get_releases_logical_single_flight_for_current_conversation(loop):
    owner = loop.owner
    owner._refresh_open_contact_presence()
    loop.now = 101
    owner.conversations_panel.conversation = {"remoteJid": "789@s.whatsapp.net"}
    owner._refresh_open_contact_presence()
    loop.advance(130)
    assert len(loop.workers) == 2
    assert owner._contact_presence_inflight is True


def test_stuck_post_stops_blocking_snapshot_reads(subscription_loop):
    loop = callbacks_only(subscription_loop)
    loop.owner._refresh_open_contact_presence()
    assert loop.owner._presence_subscription_inflight == {PN}
    loop.advance(130)
    assert loop.owner._presence_subscription_inflight == set()
    assert len(loop.workers) == 2  # Old POST still held, current GET may start.


def run_worker(loop, index=0):
    loop.workers.pop(index)()
    loop.drain()


def test_one_stuck_read_does_not_prevent_current_data_from_being_displayed(loop):
    owner = loop.owner
    owner._refresh_open_contact_presence()
    loop.now = 101
    owner.conversations_panel.conversation = {"remoteJid": "789@s.whatsapp.net"}
    owner._refresh_open_contact_presence()
    loop.advance(130)
    loop.ready()
    run_worker(loop, 1)
    assert cached(owner, "789@s.whatsapp.net")["lastSeen"] == 1700000000
    assert len(owner._contact_presence_work.running) == 1


def test_two_stuck_reads_stop_spawning_and_returning_worker_frees_capacity(loop):
    owner = loop.owner
    owner._refresh_open_contact_presence()
    loop.advance(160)
    assert len(loop.workers) == 2
    assert owner._contact_presence_inflight is False
    for moment in range(190, 1001, 30):
        loop.advance(moment)
        owner._refresh_open_contact_presence()
    assert len(loop.workers) == len(owner._contact_presence_work.running) == 2
    run_worker(loop)  # Expired worker frees a physical slot, without using its result.
    assert len(loop.workers) == 2  # Other old worker + one current replacement.
    assert owner._contact_presence_inflight is True
    loop.ready()
    run_worker(loop, 1)
    assert cached(owner, PN)["lastSeen"] == 1700000000
    assert len(owner._contact_presence_work.running) == 1


def test_old_result_and_completion_cannot_clear_newer_single_flight(loop):
    owner = loop.owner
    def slow(jid):
        loop.advance(130)  # Simulate deadline firing while real query is outstanding.
        return {"lastSeen": 1000, "isOnline": False}
    owner._fetch_contact_presence = slow
    owner._refresh_open_contact_presence()
    run_worker(loop)
    new_job = owner._contact_presence_read_job
    assert new_job is not None and owner._contact_presence_inflight is True
    assert cached(owner, PN) == {}
    loop.ready()
    owner._fetch_contact_presence = loop.fetch
    run_worker(loop)
    assert cached(owner, PN)["lastSeen"] == 1700000000
    assert owner._contact_presence_read_job is None


@pytest.mark.parametrize("change", ["hidden", "inactive", "closed", "disconnect", "shutdown", "deny", "online"])
def test_deadline_cannot_start_an_ineligible_or_satisfied_read(loop, change):
    owner = loop.owner
    owner._refresh_open_contact_presence()
    if change == "hidden":
        owner._window_hidden = True
    elif change == "inactive":
        loop.flags["active"] = False
    elif change == "closed":
        owner.conversations_panel.conversation = None
    elif change == "disconnect":
        owner._wa_connected = False
    elif change == "shutdown":
        owner._shutting_down = True
    else:
        merge(owner, PN, {"restricted": True} if change == "deny" else {"isOnline": True})
    loop.advance(130)
    assert len(loop.workers) == 1
    assert owner._contact_presence_inflight is False


def test_epoch_changes_cannot_discard_physical_limit(loop, monkeypatch):
    owner = loop.owner
    monkeypatch.setattr(coordinator, "notify_personal_presence", lambda *args: None)
    pool = owner._contact_presence_work
    for _ in range(100):
        owner._refresh_open_contact_presence()
        owner._invalidate_contact_presence()
    assert owner._contact_presence_work is pool
    assert len(loop.workers) == len(pool.running) == 2
    assert not any(timer.active for timer in loop.timers)


def test_post_deadline_allows_read_without_duplicate_post(subscription_loop):
    loop = callbacks_only(subscription_loop)
    owner = loop.owner
    owner._refresh_open_contact_presence()
    loop.advance(130)
    assert len(loop.workers) == 2
    loop.payload = {"status": "success", "presence": {
        "status": "ready", "isOnline": False, "lastSeen": 1700000000}}
    run_worker(loop, 1)
    assert cached(owner, PN)["lastSeen"] == 1700000000
    assert PN not in owner._subscribed_presence_cache
    assert len(owner._contact_presence_work.running) == 1
    assert loop.posts == []  # Held fake POST never returns/executes.


def test_late_post_success_cannot_restore_marker_or_continue_to_alias(subscription_loop, monkeypatch):
    loop = callbacks_only(subscription_loop)
    owner = loop.owner
    owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    def slow_post(*a, **kw):
        loop.posts.append(kw["json"]["phone"])
        loop.advance(130)
        return SimpleNamespace(status_code=200, json=lambda: {"status": "success"})
    monkeypatch.setattr(identity, "api_post", slow_post)
    owner._refresh_open_contact_presence()
    run_worker(loop)
    assert len(loop.posts) == 1
    assert owner._subscribed_presence_cache == {}
    assert owner._presence_subscription_confirmed == set()
    assert owner._contact_presence_readiness_candidates == set()
    assert len(loop.workers) == 1  # GET only, no second alias POST.


def test_post_slots_are_shared_across_visits_and_epochs(subscription_loop, monkeypatch):
    loop = callbacks_only(subscription_loop)
    owner = loop.owner
    monkeypatch.setattr(coordinator, "notify_personal_presence", lambda *a: None)
    for index in range(10):
        owner.conversations_panel.conversation = {"remoteJid": f"{123 + index}@s.whatsapp.net"}
        owner._refresh_open_contact_presence()
        owner._invalidate_contact_presence()
    posts = [job for job in owner._contact_presence_work.running if job.kind == "post"]
    assert len(posts) == 2
    assert len(owner._contact_presence_work.running) <= 4


def test_completed_post_with_lost_ui_callback_expires_safely(subscription_loop):
    loop = callbacks_only(subscription_loop)
    owner = loop.owner
    owner._refresh_open_contact_presence()
    loop.workers.pop(0)()  # POST finishes, but UI completion remains queued.
    loop.callbacks.clear()
    loop.advance(130)
    assert len(loop.workers) == 1  # Deadline still starts the pending GET.
    assert owner._contact_presence_inflight is True


def test_completed_old_post_callback_is_invalidated_before_new_epoch(subscription_loop, monkeypatch):
    loop = callbacks_only(subscription_loop)
    owner = loop.owner
    monkeypatch.setattr(coordinator, "notify_personal_presence", lambda *a: None)
    owner._refresh_open_contact_presence()
    loop.workers.pop(0)()
    owner._invalidate_contact_presence()
    loop.advance(130)
    assert loop.workers == [] and owner._presence_subscription_confirmed == set()


@pytest.mark.parametrize("kind", ["get", "post"])
@pytest.mark.parametrize("phase", ["timer", "construct", "start"])
def test_failed_dispatch_releases_physical_slot_and_logical_state(loop, monkeypatch, kind, phase):
    owner = loop.owner
    if kind == "post":
        from types import MethodType
        from main_window.identity import IdentityMixin
        owner._subscribed_presence_cache = {}
        owner.subscribe_presence = MethodType(IdentityMixin.subscribe_presence, owner)
    def fail(*a, **kw):
        raise RuntimeError("synthetic_dispatch")
    if phase == "timer":
        owner._contact_presence_work.call_later = fail
    else:
        class Thread:
            def __init__(self, target, daemon):
                if phase == "construct":
                    fail()
            def start(self):
                fail()
        target = coordinator if kind == "get" else identity
        monkeypatch.setattr(target, "threading", SimpleNamespace(Thread=Thread))
    with pytest.raises(RuntimeError, match="synthetic_dispatch"):
        owner._refresh_open_contact_presence()
    assert owner._contact_presence_work.running == set()
    assert owner._contact_presence_work.pending == set()
    assert not getattr(owner, "_contact_presence_inflight", False)
    assert not getattr(owner, "_presence_subscription_inflight", set())


@pytest.mark.parametrize("kind", ["get", "post"])
def test_pool_expiration_does_not_free_an_actual_worker(kind):
    clock, callbacks = [0.0], []
    pool = PresenceWork(lambda: clock[0], lambda delay, fn: SimpleNamespace(Stop=lambda: None), callbacks.append)
    first = pool.claim(kind, (PN,))
    first.arm(lambda: None)
    clock[0] = 30
    assert first.expire()
    assert not first.apply(lambda: pytest.fail("Late state commit"))
    if kind == "post":
        assert pool.claim(kind, (PN,)) is None
    second = pool.claim(kind, (LID,))
    assert second is not None
    assert pool.claim(kind, ("789@s.whatsapp.net",)) is None
    pool.invalidate()
    assert len(pool.running) == 2
    first.finish()
    assert pool.claim(kind, (PN,)) is not None


def test_timeout_is_not_a_pending_model_observation(loop):
    owner = loop.owner
    from core.contact_presence_recovery import PendingPresenceRecovery
    owner._contact_presence_recovery = recovery = PendingPresenceRecovery()
    for moment in (10, 40, 70):
        recovery.observe((PN,), "pending", moment, None)
    owner._refresh_open_contact_presence()
    loop.advance(130)
    assert not recovery.claim((PN,), 130)


@pytest.mark.parametrize("kind", ["get", "post"])
def test_completed_but_undelivered_jobs_are_bounded_and_invalidated(kind):
    pool = PresenceWork(lambda: 0, lambda *a: SimpleNamespace(Stop=lambda: None), lambda fn: None)
    first, second = pool.claim(kind, (PN,)), pool.claim(kind, (LID,))
    first.finish()
    second.finish()
    assert pool.running == set() and len(pool.pending) == 2
    assert pool.claim(kind, ("789@s.whatsapp.net",)) is None
    pool.invalidate()
    assert not first.active and not second.active
    assert pool.claim(kind, (PN,)) is not None


def test_routing_change_inside_post_cannot_confirm_old_subscription(subscription_loop, monkeypatch):
    loop = callbacks_only(subscription_loop)
    owner = loop.owner
    def post(*a, **kw):
        owner.token = "replacement"
        return SimpleNamespace(status_code=200, json=lambda: {"status": "success"})
    monkeypatch.setattr(identity, "api_post", post)
    owner._refresh_open_contact_presence()
    run_worker(loop)
    assert owner._presence_subscription_confirmed == set()
    assert owner._contact_presence_readiness_candidates == set()
    assert loop.reads == []


def test_timely_get_before_deadline_stops_watchdog_without_extra_read(loop):
    loop.owner._refresh_open_contact_presence()
    loop.advance(129.999)
    run_worker(loop)
    assert cached(loop.owner, PN)["isOnline"] is True
    loop.advance(1000)
    assert loop.workers == []
    assert loop.owner._contact_presence_work.running == set()
    assert loop.owner._contact_presence_work.pending == set()


def test_same_identity_old_get_cannot_replace_successor_snapshot(loop):
    owner = loop.owner
    owner._refresh_open_contact_presence()
    loop.advance(130)
    loop.ready()
    run_worker(loop, 1)
    expected = cached(owner, PN, fresh=False)
    loop.answer = {"lastSeen": 1000, "isOnline": False}
    run_worker(loop)
    assert cached(owner, PN, fresh=False) is expected
    assert expected["lastSeen"] == 1700000000


@pytest.mark.parametrize("result", ["success", "error", "exception"])
def test_late_post_outcomes_never_mark_subscription_or_refund_budget(subscription_loop, monkeypatch, result):
    loop = callbacks_only(subscription_loop)
    owner = loop.owner
    def post(*a, **kw):
        loop.advance(130)
        if result == "exception":
            raise TimeoutError("SYNTHETIC_PRIVATE_DETAIL")
        return SimpleNamespace(status_code=200 if result == "success" else 500,
                               json=lambda: {"status": "success"})
    monkeypatch.setattr(identity, "api_post", post)
    owner._refresh_open_contact_presence()
    run_worker(loop)
    assert owner._subscribed_presence_cache == {}
    assert owner._presence_subscription_confirmed == set()
    assert owner._contact_presence_readiness_candidates == set()
    assert len([job for job in owner._contact_presence_work.running if job.kind == "get"]) == 1


def test_already_queued_stopped_watchdogs_cannot_renew_a_ready_null_read(subscription_loop):
    loop = subscription_loop
    loop.payload = {"status": "success", "presence": {
        "status": "ready", "isOnline": False, "lastSeen": None}}
    loop.begin()
    assert loop.reads == [100]
    stopped = list(loop.timers)
    assert stopped and all(not timer.active for timer in stopped)
    loop.now = 130
    for timer in stopped:
        timer.callback()  # Simulate a native callback already queued before Stop.
        loop.drain()
    assert loop.reads == [100] and loop.posts == [100]
