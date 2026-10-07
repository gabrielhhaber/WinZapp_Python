"""A subscription worker that cannot start must not leave a live-subscription marker.

Unbound methods and synthetic workers/responses only; no App, account or network.
"""
from types import MethodType, SimpleNamespace

import pytest

from core.contact_presence import cached, is_subscribed
from main_window.identity import IdentityMixin
import main_window.identity as identity
import main_window.contact_presence as coordinator
from tests.test_contact_presence_freshness import Owner, PN, LID, scheduled


def broken_thread(phase, before_failure=lambda: None):
    class Thread:
        def __init__(self, target, daemon):
            assert daemon is True
            if phase == "construct":
                fail()

        def start(self):
            fail()

    def fail():
        before_failure()
        raise RuntimeError("synthetic_subscription_thread_failure")

    return SimpleNamespace(Thread=Thread)


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_thread_failure_clears_attempted_phone_and_lid(monkeypatch, phase):
    owner = Owner()
    owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    owner._subscribed_presence_cache = {"789@s.whatsapp.net": 50}
    monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: 100))
    monkeypatch.setattr(identity, "threading", broken_thread(phase))
    with pytest.raises(RuntimeError, match="synthetic_subscription_thread_failure"):
        IdentityMixin.subscribe_presence(owner, PN)
    assert owner._subscribed_presence_cache == {"789@s.whatsapp.net": 50}
    assert not is_subscribed(owner, PN) and not is_subscribed(owner, LID)


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_thread_failure_preserves_recent_alias_not_attempted(monkeypatch, phase):
    owner = Owner()
    owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    owner._subscribed_presence_cache = {LID: 95}
    monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: 100))
    monkeypatch.setattr(identity, "threading", broken_thread(phase))
    with pytest.raises(RuntimeError, match="synthetic_subscription_thread_failure"):
        IdentityMixin.subscribe_presence(owner, PN)
    assert owner._subscribed_presence_cache == {LID: 95}
    assert is_subscribed(owner, PN)  # The paired LID's existing subscription is live.


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_thread_failure_preserves_target_replaced_by_newer_attempt(monkeypatch, phase):
    owner = Owner()
    owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    owner._subscribed_presence_cache = {}
    monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: 100))
    def newer_attempt():
        owner._subscribed_presence_cache[PN] = 111
    monkeypatch.setattr(identity, "threading", broken_thread(phase, newer_attempt))
    with pytest.raises(RuntimeError, match="synthetic_subscription_thread_failure"):
        IdentityMixin.subscribe_presence(owner, PN)
    assert owner._subscribed_presence_cache == {PN: 111}
    assert is_subscribed(owner, PN)


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_active_refresh_recovers_subscription_and_keeps_success_once(scheduled, monkeypatch, phase):
    owner, workers, callbacks = scheduled
    owner._phone_to_lid[PN], owner._lid_to_phone[LID] = LID, PN
    owner._subscribed_presence_cache = {}
    owner.wpp_server, owner.wpp_port, owner.token = "http://invalid.test", 1, "synthetic"
    owner.subscribe_presence = MethodType(IdentityMixin.subscribe_presence, owner)
    state = {"fail": True, "now": 100}
    posts = []

    class SubscriptionThread:
        def __init__(self, target, daemon):
            self.target = target
            if state["fail"] and phase == "construct":
                raise RuntimeError("synthetic_subscription_thread_failure")

        def start(self):
            if state["fail"]:
                raise RuntimeError("synthetic_subscription_thread_failure")
            self.target()

    monkeypatch.setattr(identity, "time", SimpleNamespace(time=lambda: state["now"]))
    # Separate fake factories: the subscription and HTTP workers are distinct.
    monkeypatch.setattr(identity, "threading", SimpleNamespace(Thread=SubscriptionThread))
    monkeypatch.setattr(identity, "api_post", lambda url, **kw: posts.append(kw["json"]["phone"])
                        or SimpleNamespace(status_code=200, json=lambda: {"status": "success"}))
    with pytest.raises(RuntimeError, match="synthetic_subscription_thread_failure"):
        owner._refresh_open_contact_presence()
    assert not is_subscribed(owner, PN)
    assert not getattr(owner, "_contact_presence_inflight", False)
    assert workers == callbacks == posts == []

    state.update(fail=False, now=111)
    monkeypatch.setattr(coordinator.time, "monotonic", lambda: state["now"])
    owner._refresh_open_contact_presence()
    assert set(posts) == {"123@c.us", LID}
    assert is_subscribed(owner, PN) and len(workers) == 1
    workers.pop()()
    callbacks.pop()()
    assert cached(owner, PN)["lastSeen"] == 1000
    assert owner._contact_presence_inflight is False

    state["now"] = 141
    owner._refresh_open_contact_presence()
    assert len(posts) == 2  # Igor's successful-subscription gate remains effective.
    assert len(workers) == 1
    workers.pop()()
    callbacks.pop()()
    assert owner._contact_presence_inflight is False
