"""Profile presence reads: bounded fake calls, late routing and privacy races."""
from types import SimpleNamespace

import pytest

from core import contact_presence as presence
from main_window.identity import IdentityMixin
import main_window.identity as identity
import main_window.profile_presence as implementation
from tests.test_contact_presence_freshness import Owner, PN, LID


@pytest.fixture
def profile(monkeypatch):
    owner, callbacks, calls, clock = Owner(), [], [], [100.0]
    monkeypatch.setattr(identity, "wx", SimpleNamespace(CallAfter=callbacks.append))
    monkeypatch.setattr(presence, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(implementation, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def fetch(jid):
        calls.append(jid)
        return {"lastKnownPresence": "unavailable", "isOnline": False, "lastSeen": 1700000000}
    owner._fetch_contact_presence = fetch
    return owner, callbacks, calls, clock


@pytest.mark.parametrize("field,value", [("token", "successor"), ("wpp_port", 2), ("wpp_server", "http://other.invalid")])
def test_profile_reply_from_changed_routing_is_not_returned_or_applied(profile, field, value):
    owner, callbacks, calls, _ = profile
    fetch = owner._fetch_contact_presence
    def changed(jid):
        answer = fetch(jid)
        setattr(owner, field, value)
        return answer
    owner._fetch_contact_presence = changed
    assert IdentityMixin.get_last_seen(owner, PN) is None
    for callback in callbacks:
        callback()
    assert presence.cached(owner, PN, fresh=False) == {}


def test_stale_explicit_denial_blocks_the_profile_http_path(profile):
    owner, callbacks, calls, clock = profile
    presence.merge(owner, PN, {"restricted": True, "isOnline": False})
    clock[0] = 300
    assert IdentityMixin.get_last_seen(owner, PN) is None
    assert calls == [] and callbacks == []


def test_profile_has_only_two_physical_read_slots_even_across_connection_epochs(profile):
    owner, callbacks, calls, _ = profile
    base = owner._fetch_contact_presence
    def nested(jid):
        base(jid)
        if jid == PN:
            owner._contact_presence_epoch = 1
            IdentityMixin.get_last_seen(owner, "789@s.whatsapp.net")
        elif jid.startswith("789"):
            owner._contact_presence_epoch = 2
            IdentityMixin.get_last_seen(owner, "999@s.whatsapp.net")
        return None
    owner._fetch_contact_presence = nested
    IdentityMixin.get_last_seen(owner, PN)
    assert calls == [PN, "789@s.whatsapp.net"]


@pytest.mark.parametrize("delay,accepted", [(29.999, True), (30, False), (300, False)])
def test_profile_deadline_rejects_late_return_and_its_ui_delivery(profile, delay, accepted):
    owner, callbacks, _, clock = profile
    base = owner._fetch_contact_presence
    def delayed(jid):
        answer = base(jid)
        clock[0] += delay
        return answer
    owner._fetch_contact_presence = delayed
    answer = IdentityMixin.get_last_seen(owner, PN)
    assert (answer == 1700000000) is accepted
    for callback in callbacks:
        callback()
    assert bool(presence.cached(owner, PN, fresh=False)) is accepted


@pytest.mark.parametrize("change", ["deadline", "token", "port", "server", "epoch", "visit", "mapping", "live", "denial", "shutdown", "disconnect"])
def test_queued_profile_result_rechecks_ownership_before_ui_commit(profile, change):
    owner, callbacks, _, clock = profile
    assert IdentityMixin.get_last_seen(owner, PN) == 1700000000
    before = presence.cached(owner, PN, fresh=False)
    if change == "deadline":
        clock[0] = 130
    elif change in ("token", "port", "server"):
        setattr(owner, {"token": "token", "port": "wpp_port", "server": "wpp_server"}[change], "successor")
    elif change == "epoch":
        owner._contact_presence_epoch = 1
    elif change == "visit":
        owner.conversations_panel._contact_presence_visit += 1
    elif change == "mapping":
        owner._lid_to_phone[LID], owner._phone_to_lid[PN] = PN, LID
    elif change in ("live", "denial"):
        before = presence.merge(owner, PN, {"isOnline": change == "live", "restricted": change == "denial"})
    else:
        setattr(owner, "_shutting_down" if change == "shutdown" else "_wa_connected", change == "shutdown")
    callbacks.pop()()
    after = presence.cached(owner, PN, fresh=False)
    assert after == before
    if before:
        assert after is before
    assert not owner._profile_presence_work.pending


@pytest.mark.parametrize("state", ["available", "composing", "recording"])
def test_profile_uses_available_activity_without_a_read(profile, state):
    owner, callbacks, calls, _ = profile
    presence.merge(owner, PN, {"lastKnownPresence": state, "isOnline": state == "available"})
    assert IdentityMixin.get_last_seen(owner, PN) is None
    assert not calls and not callbacks


@pytest.mark.parametrize("change", ["shutdown", "disconnected", "empty_token", "group", "invalid"])
def test_ineligible_profile_request_does_not_dispatch(profile, change):
    owner, callbacks, calls, _ = profile
    jid = PN
    if change == "shutdown":
        owner._shutting_down = True
    elif change == "disconnected":
        owner._wa_connected = False
    elif change == "empty_token":
        owner.token = ""
    else:
        jid = "123@g.us" if change == "group" else "bad@lid"
    assert IdentityMixin.get_last_seen(owner, jid) is None
    assert not calls and not callbacks


def test_reciprocal_alias_cannot_duplicate_a_profile_read(profile):
    owner, callbacks, calls, _ = profile
    owner._lid_to_phone[LID], owner._phone_to_lid[PN] = PN, LID
    base = owner._fetch_contact_presence
    def nested(jid):
        answer = base(jid)
        assert IdentityMixin.get_last_seen(owner, LID) is None
        return answer
    owner._fetch_contact_presence = nested
    assert IdentityMixin.get_last_seen(owner, PN) == 1700000000
    assert calls == [PN]


def test_completed_jobs_waiting_for_ui_are_bounded_and_expired_callbacks_are_inert(profile):
    owner, callbacks, calls, clock = profile
    for jid in (PN, "789@s.whatsapp.net", "999@s.whatsapp.net"):
        IdentityMixin.get_last_seen(owner, jid)
    assert calls == [PN, "789@s.whatsapp.net"] and len(callbacks) == 2
    old = list(callbacks)
    clock[0] = 131
    IdentityMixin.get_last_seen(owner, "999@s.whatsapp.net")
    for callback in old:
        callback()
    assert presence.cached(owner, PN, fresh=False) == {}
    assert presence.cached(owner, "789@s.whatsapp.net", fresh=False) == {}
    assert len(calls) == 3


def test_expired_running_profile_jobs_are_never_released_by_reconnection(profile):
    owner, _, calls, clock = profile
    work = implementation.profile_work(owner)
    one, two = work.claim("profile", [PN]), work.claim("profile", ["789@s.whatsapp.net"])
    for epoch in range(100):
        owner._invalidate_contact_presence()
        clock[0] += 30
        IdentityMixin.get_last_seen(owner, PN)
    assert not calls and work.running == {one, two}
    one.finish()
    assert IdentityMixin.get_last_seen(owner, PN) == 1700000000
    assert calls == [PN]


@pytest.mark.parametrize("fault", ["fetch", "dispatch"])
def test_profile_failure_retires_physical_and_pending_reservations(profile, monkeypatch, fault):
    owner, callbacks, calls, _ = profile
    def fail(*args):
        raise RuntimeError("synthetic private failure")
    if fault == "fetch":
        owner._fetch_contact_presence = fail
    else:
        monkeypatch.setattr(identity, "wx", SimpleNamespace(CallAfter=fail))
    assert IdentityMixin.get_last_seen(owner, PN) is None
    for callback in callbacks:
        callback()
    assert not owner._profile_presence_work.running and not owner._profile_presence_work.pending


def test_contact_profile_is_delivered_without_waiting_for_presence_http(profile, monkeypatch):
    owner, _, calls, _ = profile
    owner.get_profile_about = lambda jid: "Synthetic biography"
    def must_not_wait(jid):
        pytest.fail("Contact profile must not wait for last-seen HTTP")
    owner.get_last_seen = must_not_wait
    monkeypatch.setattr(identity, "api_get", lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: {"status": "success", "response": {"name": "Synthetic name"}}))
    answer = IdentityMixin.get_contact_profile(owner, PN)
    assert answer["aboutText"] == "Synthetic biography"
    assert answer["response"]["name"] == "Synthetic name"
    assert not calls


def make_dialog(owner, monkeypatch, callbacks, *, standalone=True):
    from tests.test_contact_presence_foreground import Dialog as DialogStub
    import ui.dialogs.conversation_data_dialog as module
    if standalone:
        owner.conversations_panel.conversation = {"remoteJid": "789@s.whatsapp.net"}
    dialog = DialogStub(owner)
    dialog._is_group = False
    dialog._load_media_history = lambda: None
    dialog._load_common_groups = lambda: None
    dialog._populate_personal = lambda data: setattr(dialog, "displayed", data)
    owner.get_contact_profile = lambda jid: {"response": {"name": "Synthetic"}}
    owner.get_last_seen = lambda jid: IdentityMixin.get_last_seen(owner, jid)
    monkeypatch.setattr(module, "wx", SimpleNamespace(CallAfter=lambda fn, *args:
        callbacks.append(lambda: fn(*args))))
    return dialog


def test_dialog_posts_profile_before_a_stalled_standalone_presence_read(profile, monkeypatch):
    from ui.dialogs.conversation_data_dialog import ConversationDataDialog
    owner, callbacks, calls, clock = profile
    dialog = make_dialog(owner, monkeypatch, callbacks)
    base = owner._fetch_contact_presence
    def stalled(jid):
        assert len(callbacks) == 1
        callbacks.pop(0)()
        assert dialog.displayed["response"]["name"] == "Synthetic"
        clock[0] += 300
        return base(jid)
    owner._fetch_contact_presence = stalled
    ConversationDataDialog._fetch_data(dialog)
    assert calls == [PN]
    assert presence.cached(owner, PN, fresh=False) == {}


def test_current_dialog_defers_to_existing_active_chat_reader(profile, monkeypatch):
    from ui.dialogs.conversation_data_dialog import ConversationDataDialog
    owner, callbacks, calls, _ = profile
    dialog = make_dialog(owner, monkeypatch, callbacks, standalone=False)
    ConversationDataDialog._fetch_data(dialog)
    assert len(callbacks) == 1 and calls == []


@pytest.mark.parametrize("change", ["closed", "epoch", "routing", "visit", "panel"])
def test_late_dialog_profile_cannot_start_presence_for_a_replacement_context(profile, monkeypatch, change):
    import main_window.profile_presence as module
    owner, callbacks, calls, _ = profile
    dialog = make_dialog(owner, monkeypatch, callbacks)
    if change == "closed":
        dialog._closed.set()
    elif change == "epoch":
        owner._contact_presence_epoch = 1
    elif change == "routing":
        owner.token = "successor"
    elif change == "visit":
        owner.conversations_panel._contact_presence_visit += 1
    else:
        owner.conversations_panel = SimpleNamespace(**vars(owner.conversations_panel))
    module.refresh_dialog_presence(dialog)
    assert not calls and not callbacks


def test_group_model_data_never_enters_the_profile_presence_reader(profile):
    owner, callbacks, calls, _ = profile
    assert IdentityMixin.get_last_seen(owner, "123@g.us") is None
    assert not calls and not callbacks


def test_touched_profile_path_logs_shapes_without_contact_content(profile, monkeypatch, caplog):
    import logging
    owner, _, _, _ = profile
    owner.get_profile_about = lambda jid: "PRIVATE_BIO"
    monkeypatch.setattr(identity, "api_get", lambda *a, **kw: SimpleNamespace(status_code=200,
        json=lambda: {"status": "success", "response": {"name": "PRIVATE_NAME", "secret": "PRIVATE_BODY"}}))
    with caplog.at_level(logging.INFO):
        result = IdentityMixin.get_contact_profile(owner, PN)
    assert result["response"]["name"] == "PRIVATE_NAME"
    assert "response_status=200" in caplog.text
    assert all(value not in caplog.text for value in (PN, "PRIVATE_NAME", "PRIVATE_BIO", "PRIVATE_BODY", owner.token))


def test_exhausted_profile_capacity_cannot_return_an_unguarded_captured_timestamp(profile):
    owner, callbacks, calls, _ = profile
    presence.merge(owner, PN, {"lastKnownPresence": "unavailable", "lastSeen": 1700000000})
    work = implementation.profile_work(owner)
    work.claim("profile", [PN])
    work.claim("profile", ["789@s.whatsapp.net"])
    assert IdentityMixin.get_last_seen(owner, PN) is None
    assert not calls and not callbacks
