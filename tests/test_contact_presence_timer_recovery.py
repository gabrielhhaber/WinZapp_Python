"""Worker-start faults must not kill the one-shot presence refresh chain.

Real mixin methods, queued fake timers/workers; no App, sleep or provider account.
"""
from types import MethodType, SimpleNamespace

import pytest

from core.contact_presence import cached, is_subscribed
from main_window.identity import IdentityMixin
import main_window.identity as identity
import main_window.contact_presence as coordinator
from ui.conversation_panel.contact_presence import ContactPresencePanelMixin
import ui.conversation_panel.contact_presence as ticker
from tests.test_contact_presence_freshness import Owner, PN, LID


@pytest.fixture
def loop(monkeypatch):
    after, timers = [], []

    class Timer:
        def __init__(self, delay, callback):
            assert delay == 30_000
            self.callback, self.stopped = callback, False
            timers.append(self)

        def Stop(self):
            self.stopped = True

        def fire(self):
            assert not self.stopped
            self.stopped = True  # CallLater is one-shot, not a repeating timer.
            self.callback()

    def call_after(callback):
        after.append(callback)

    monkeypatch.setattr(ticker, "wx", SimpleNamespace(CallAfter=call_after, CallLater=Timer))
    monkeypatch.setattr(coordinator, "wx", SimpleNamespace(CallAfter=call_after))
    return SimpleNamespace(after=after, timers=timers)


class TestTimerRecovery:
    @pytest.mark.parametrize("stage", ["initial", "running"])
    @pytest.mark.parametrize("phase", ["construct", "start"])
    @pytest.mark.parametrize("kind", ["subscription", "http"])
    def test_next_tick_recovers_real_refresh_after_worker_failure(
            self, loop, monkeypatch, kind, phase, stage):
        owner = Owner()
        panel = ContactPresencePanelMixin()
        panel.conversation = {"remoteJid": PN}
        panel._refresh_presence_note = owner.notes.append
        panel.IsShown = lambda: True
        panel.main_window = owner
        owner.conversations_panel = panel
        owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
        owner._subscribed_presence_cache = {}
        owner.wpp_server, owner.wpp_port, owner.token = "http://invalid.test", 1, "synthetic"
        owner.subscribe_presence = MethodType(IdentityMixin.subscribe_presence, owner)
        owner._fetch_contact_presence = lambda jid: {"lastSeen": 1000}
        state = {"fail": stage == "initial", "now": 100}
        workers, posts = [], []

        def thread_factory(worker_kind):
            class Thread:
                def __init__(self, target, daemon):
                    assert daemon is True
                    self.target = target
                    if state["fail"] and kind == worker_kind and phase == "construct":
                        raise RuntimeError("synthetic_worker_start_failure")

                def start(self):
                    if state["fail"] and kind == worker_kind:
                        raise RuntimeError("synthetic_worker_start_failure")
                    if worker_kind == "subscription":
                        self.target()
                    else:
                        workers.append(self.target)

            return SimpleNamespace(Thread=Thread)

        monkeypatch.setattr(identity, "threading", thread_factory("subscription"))
        monkeypatch.setattr(coordinator, "threading", thread_factory("http"))
        monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: state["now"]))
        monkeypatch.setattr(coordinator, "time", SimpleNamespace(monotonic=lambda: state["now"]))
        monkeypatch.setattr(identity, "api_post", lambda url, **kw:
                            posts.append(kw["json"]["phone"])
                            or SimpleNamespace(status_code=200, json=lambda: {"status": "success"}))

        def finish_http():
            assert len(workers) == 1
            workers.pop(0)()
            assert len(loop.after) == 1
            loop.after.pop(0)()
            assert owner._contact_presence_inflight is False

        panel._start_contact_presence()
        tick = loop.after.pop(0)
        if stage == "running":
            tick()
            finish_http()
            state.update(fail=True, now=130)
            if kind == "subscription":
                owner._subscribed_presence_cache = {}  # A new connection needs resubscription.
            tick = loop.timers[-1].fire

        before_timers = len(loop.timers)
        with pytest.raises(RuntimeError, match="synthetic_worker_start_failure"):
            tick()
        assert not getattr(owner, "_contact_presence_inflight", False)
        assert is_subscribed(owner, PN) is (kind == "http")
        assert workers == loop.after == []
        assert len(loop.timers) == before_timers + 1
        assert panel._contact_presence_timer is loop.timers[-1]

        before_posts = len(posts)
        state.update(fail=False, now=state["now"] + 30)
        loop.timers[-1].fire()  # Recovery comes from the timer, not a direct refresh call.
        finish_http()
        assert cached(owner, PN)["lastSeen"] == 1000
        assert is_subscribed(owner, PN) and is_subscribed(owner, LID)
        assert len(posts) == before_posts + (2 if kind == "subscription" else 0)

        successful_posts = len(posts)
        state["now"] += 30
        loop.timers[-1].fire()
        finish_http()
        assert len(posts) == successful_posts  # Successful subscriptions remain reusable.
        assert panel._contact_presence_timer is loop.timers[-1]
        assert not loop.timers[-1].stopped

    @pytest.mark.parametrize("stage", ["initial", "running"])
    @pytest.mark.parametrize("fails", [False, True])
    @pytest.mark.parametrize("change", ["stop", "close", "switch", "restart", "shutdown"])
    def test_refresh_cannot_rearm_a_finished_visit(self, loop, stage, fails, change):
        panel = ContactPresencePanelMixin()
        panel.conversation = {"remoteJid": PN}
        state = {"change": stage == "initial"}
        calls = []

        def refresh():
            calls.append(True)
            if not state["change"]:
                return
            if change == "stop":
                panel._stop_contact_presence()
            elif change == "close":
                panel.conversation = None
            elif change == "switch":
                panel._stop_contact_presence()
                panel.conversation = {"remoteJid": "789@s.whatsapp.net"}
            elif change == "restart":
                panel._start_contact_presence()
            else:
                panel.main_window._shutting_down = True
            if fails:
                raise RuntimeError("synthetic_refresh_failure")

        panel.main_window = SimpleNamespace(_refresh_open_contact_presence=refresh)
        panel._start_contact_presence()
        tick = loop.after.pop(0)
        if stage == "running":
            tick()
            state["change"] = True
            tick = loop.timers[-1].fire
        before_timers = len(loop.timers)
        if fails:
            with pytest.raises(RuntimeError, match="synthetic_refresh_failure"):
                tick()
        else:
            tick()
        assert len(loop.timers) == before_timers

        if change == "restart":
            assert panel._contact_presence_timer is None
            assert len(loop.after) == 1
            state["change"] = False
            loop.after.pop(0)()
            assert len(loop.timers) == before_timers + 1
            assert panel._contact_presence_timer is loop.timers[-1]
        else:
            assert loop.after == []

        # Even an already-delivered callback from the old visit must do no work.
        previous_calls, previous_timers = len(calls), len(loop.timers)
        if stage == "running":
            loop.timers[0].callback()
            assert len(calls) == previous_calls
            assert len(loop.timers) == previous_timers
