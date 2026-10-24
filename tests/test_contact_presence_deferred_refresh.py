"""Missed offline refreshes get one cancellable follow-up, never a polling loop."""
from types import SimpleNamespace

import pytest

import core.contact_presence as presence
import main_window.contact_presence as coordinator
import main_window.identity as identity
import ui.conversation_panel.contact_presence as panel_presence
from tests.test_contact_presence_freshness import Owner, PN, LID


class Clockwork:
    def __init__(self, monkeypatch):
        self.now = 100.0
        self.workers, self.callbacks, self.timers, self.reads = [], [], [], []
        self.answer = {"lastKnownPresence": "available", "isOnline": True, "lastSeen": None}
        self.flags = {"active": True, "shown": True, "iconized": False}
        self.owner = Owner()
        self.owner._subscribed_presence_cache = {PN: 1, LID: 1}
        self.owner.IsActive = lambda: self.flags["active"]
        self.owner.IsShown = lambda: self.flags["shown"]
        self.owner.IsIconized = lambda: self.flags["iconized"]
        self.owner._fetch_contact_presence = self.fetch
        self.owner._presence_timers = {}
        clock = SimpleNamespace(monotonic=lambda: self.now)
        monkeypatch.setattr(coordinator, "time", clock)
        monkeypatch.setattr(presence, "time", clock)
        outer = self

        class Timer:
            def __init__(self, delay, callback):
                self.at, self.callback, self.active = outer.now + delay / 1000, callback, True
                outer.timers.append(self)

            def Stop(self):
                self.active = False

        class Thread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                outer.workers.append(self.target)

        ui = SimpleNamespace(CallAfter=lambda fn: self.callbacks.append(fn), CallLater=Timer)
        self.owner._contact_presence_work.call_later = ui.CallLater
        self.owner._contact_presence_work.call_after = ui.CallAfter
        monkeypatch.setattr(coordinator, "wx", ui)
        monkeypatch.setattr(identity, "wx", ui)
        monkeypatch.setattr(panel_presence, "wx", ui)
        monkeypatch.setattr(coordinator.threading, "Thread", Thread)

    def fetch(self, jid):
        self.reads.append((self.now, jid))
        return self.answer

    def drain(self):
        for _ in range(100):
            while self.callbacks:
                self.callbacks.pop(0)()
            if not self.workers:
                return
            self.workers.pop(0)()
        pytest.fail("Presence refresh spawned an unbounded callback/worker chain")

    def advance(self, moment):
        for _ in range(100):
            due = [t for t in self.timers if t.active and t.at <= moment]
            if not due:
                self.now = moment
                return
            timer = min(due, key=lambda item: item.at)
            self.now, timer.active = timer.at, False
            timer.callback()
            self.drain()
        pytest.fail("Presence refresh created an unbounded timer chain")

    def begin(self):
        self.owner._refresh_open_contact_presence()
        self.drain()

    def offline(self):
        presence.merge(self.owner, PN, {"lastKnownPresence": "unavailable", "isOnline": False})
        self.owner._refresh_open_contact_presence()

    def ready(self):
        self.answer = {"lastKnownPresence": "unavailable", "isOnline": False, "lastSeen": 1700000000}


@pytest.fixture
def loop(monkeypatch):
    return Clockwork(monkeypatch)


def test_offline_during_cooldown_is_refreshed_at_first_eligible_time(loop):
    loop.begin()
    loop.now = 101
    loop.ready()
    loop.offline()
    loop.advance(109.999)
    assert len(loop.reads) == 1
    loop.advance(110)
    assert len(loop.reads) == 2
    assert presence.cached(loop.owner, PN)["lastSeen"] == 1700000000


def test_offline_during_inflight_rejects_old_online_then_reads_current_snapshot(loop):
    loop.owner._refresh_open_contact_presence()
    loop.now = 101
    loop.offline()
    loop.drain()
    assert presence.cached(loop.owner, PN)["isOnline"] is False
    loop.ready()
    loop.advance(110)
    assert presence.cached(loop.owner, PN)["lastSeen"] == 1700000000
    assert len(loop.reads) == 2


def test_repeated_requests_coalesce_without_extending_the_due_time(loop):
    loop.begin()
    for index in range(100):
        loop.now = 101 + index / 100
        loop.offline()
    assert len([t for t in loop.timers if t.active]) == 1
    assert loop.timers[-1].at == 110
    loop.ready()
    loop.advance(110)
    assert len(loop.reads) == 2
    loop.advance(1000)
    assert len(loop.reads) == 2


@pytest.mark.parametrize("answer", [None, {"lastKnownPresence": "unavailable", "lastSeen": None}])
def test_followup_result_does_not_schedule_itself_again(loop, answer):
    loop.begin()
    loop.now = 101
    loop.offline()
    loop.answer = answer
    loop.advance(110)
    assert len(loop.reads) == 2
    loop.advance(1000)
    assert len(loop.reads) == 2
    assert not [timer for timer in loop.timers if timer.active]


@pytest.mark.parametrize("update", [
    {"lastKnownPresence": "available", "isOnline": True},
    {"lastKnownPresence": "composing", "isOnline": False},
    {"lastKnownPresence": "unavailable", "lastSeen": 1700000000},
    {"lastKnownPresence": "unavailable", "restricted": True},
])
def test_new_usable_or_withheld_event_cancels_missing_timestamp_work(loop, update):
    loop.begin()
    loop.now = 101
    loop.offline()
    presence.merge(loop.owner, PN, update)
    loop.advance(110)
    assert len(loop.reads) == 1


@pytest.mark.parametrize("change", ["close", "reopen", "switch", "mapping", "disconnect", "shutdown",
                                  "hidden", "inactive", "minimized", "not_shown"])
def test_queued_timer_cannot_cross_its_scope_or_window_gate(loop, change):
    loop.begin()
    loop.now = 101
    loop.offline()
    panel = loop.owner.conversations_panel
    if change == "close":
        panel.conversation = None
    elif change == "reopen":
        panel._contact_presence_visit += 1
    elif change == "switch":
        panel.conversation = {"remoteJid": "789@s.whatsapp.net"}
    elif change == "mapping":
        loop.owner._lid_to_phone[LID] = PN
        loop.owner._phone_to_lid[PN] = LID
    elif change == "disconnect":
        loop.owner._wa_connected = False
        loop.owner._invalidate_contact_presence()
    elif change == "shutdown":
        loop.owner._shutting_down = True
    elif change == "hidden":
        loop.owner._window_hidden = True
    else:
        loop.flags[{"inactive": "active", "minimized": "iconized", "not_shown": "shown"}[change]] = change == "minimized"
    loop.advance(110)
    assert len(loop.reads) == 1
    assert not loop.workers


def test_already_queued_cancelled_timer_cannot_send_for_a_new_request(loop):
    loop.begin()
    loop.now = 101
    loop.offline()
    old = loop.timers[-1]
    loop.owner._invalidate_contact_presence()
    loop.owner._subscribed_presence_cache = {PN: 1}
    loop.owner._refresh_open_contact_presence()
    loop.drain()
    old.callback()
    loop.drain()
    assert len(loop.reads) == 2


def test_normal_tick_consumes_deferred_work_instead_of_duplicating_it(loop):
    loop.begin()
    loop.now = 101
    loop.offline()
    loop.ready()
    loop.now = 110
    loop.owner._refresh_open_contact_presence()
    loop.drain()
    loop.advance(111)
    assert len(loop.reads) == 2


def test_same_person_new_visit_keeps_replacement_after_old_request_finishes(loop):
    loop.owner._refresh_open_contact_presence()
    loop.now = 101
    loop.owner.conversations_panel._contact_presence_visit += 1
    loop.owner._refresh_open_contact_presence()
    loop.drain()
    loop.ready()
    loop.advance(110)
    assert presence.cached(loop.owner, PN)["lastSeen"] == 1700000000


def test_new_offline_observation_refreshes_an_older_cached_last_seen(loop):
    loop.ready()
    loop.begin()
    loop.now = 101
    loop.answer = {"lastKnownPresence": "unavailable", "isOnline": False, "lastSeen": 1700000300}
    loop.offline()
    assert presence.cached(loop.owner, PN)["lastSeen"] == 1700000000
    loop.advance(110)
    assert presence.cached(loop.owner, PN)["lastSeen"] == 1700000300


def test_equal_clock_ticks_do_not_make_an_inherited_timestamp_a_new_observation(loop):
    loop.ready()
    loop.begin()
    loop.answer = {"lastKnownPresence": "unavailable", "isOnline": False, "lastSeen": 1700000300}
    loop.offline()  # A distinct event can share the previous receipt's clock tick.
    loop.advance(110)
    assert presence.cached(loop.owner, PN)["lastSeen"] == 1700000300


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_deferred_thread_failure_releases_inflight_and_leaves_no_retry_loop(loop, monkeypatch, phase):
    loop.begin()
    loop.now = 101
    loop.offline()

    class Broken:
        def __init__(self, target, daemon):
            if phase == "construct":
                raise RuntimeError("synthetic")

        def start(self):
            raise RuntimeError("synthetic")

    monkeypatch.setattr(coordinator.threading, "Thread", Broken)
    with pytest.raises(RuntimeError, match="synthetic"):
        loop.advance(110)
    assert not loop.owner._contact_presence_inflight
    assert not [timer for timer in loop.timers if timer.active]


def test_stopping_a_visit_cancels_its_deferred_timer(loop):
    loop.begin()
    loop.now = 101
    loop.offline()
    panel = panel_presence.ContactPresencePanelMixin()
    panel.main_window = loop.owner
    panel._stop_contact_presence()
    assert not [timer for timer in loop.timers if timer.active]
    loop.advance(110)
    assert len(loop.reads) == 1


def test_timer_allocation_failure_does_not_leave_a_future_retry(loop, monkeypatch):
    loop.begin()
    loop.now = 101
    def fail(*args):
        raise RuntimeError("synthetic allocation")
    monkeypatch.setattr(coordinator.wx, "CallLater", fail)
    with pytest.raises(RuntimeError, match="allocation"):
        loop.offline()
    loop.advance(1000)
    assert len(loop.reads) == 1 and not loop.workers


def test_cancelled_old_callback_cannot_consume_a_same_scope_replacement():
    from core.contact_presence_refresh import DeferredPresenceRefresh
    calls, timers = [], []
    def later(delay, fn):
        timer = SimpleNamespace(Stop=lambda: None, callback=fn, delay=delay)
        timers.append(timer)
        return timer
    pending = DeferredPresenceRefresh()
    pending.schedule(("visit",), 0.0011, later, calls.append)
    old = timers[-1]
    assert old.delay == 2  # Round upwards: never call just before the cooldown ends.
    pending.cancel()
    pending.schedule(("visit",), 1, later, calls.append)
    old.callback()
    assert calls == []
    timers[-1].callback()
    assert calls == [("visit",)]


def test_destroyed_timer_does_not_prevent_cancellation():
    from core.contact_presence_refresh import DeferredPresenceRefresh
    callbacks = []
    def stopped():
        raise RuntimeError("destroyed wx timer")
    pending = DeferredPresenceRefresh()
    pending.schedule(("visit",), 1,
                     lambda delay, fn: (callbacks.append(fn) or SimpleNamespace(Stop=stopped)),
                     lambda scope: pytest.fail("cancelled timer delivered"))
    pending.cancel()
    callbacks[0]()


def test_a_newer_offline_event_requires_an_observation_after_that_event(loop):
    loop.begin()
    loop.now = 101
    loop.offline()
    presence.merge(loop.owner, PN, {"lastSeen": 1700000000})
    loop.now = 102
    loop.offline()  # Retains the timestamp, but does not refresh its observation time.
    loop.answer = {"lastKnownPresence": "unavailable", "lastSeen": 1700000300}
    loop.advance(110)
    assert presence.cached(loop.owner, PN)["lastSeen"] == 1700000300
