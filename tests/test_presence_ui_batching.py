"""Batch paint without reordering cache, denial, speech or message transitions."""
from types import SimpleNamespace

import pytest

from core.contact_presence import cached, merge
import core.contact_presence as presence
from main_window import chat_events, presence_ui
from main_window.presence_ui import PresenceUiUpdates, request_presence_ui
from tests.test_presence_ui_load import PresenceOwner
from tests.test_typing_row import (
    CONTACT, _Panel, dismiss_typing_row_for_message,
    typing_row_shown,
)


class Timer:
    def __init__(self, callback): self.callback, self.stopped = callback, False
    def Stop(self): self.stopped = True


@pytest.fixture
def burst(monkeypatch):
    callbacks, views, timers, speech = [], [], [], []
    owner = PresenceOwner()
    owner.IsShown, owner.IsIconized, owner.IsActive = lambda: True, lambda: False, lambda: True
    owner._resolve_jid_name = lambda *args, **kwargs: "Contact"
    owner.i18n = SimpleNamespace(t=lambda key: key + " {name}")
    owner.speak_output = SimpleNamespace(output=speech.append)
    owner._presence_ui = PresenceUiUpdates(owner, callbacks.append, clock=lambda: 0)
    def later(delay, callback):
        timer = Timer(callback)
        timers.append(timer)
        return timer
    monkeypatch.setattr(chat_events.wx, "CallLater", later)
    monkeypatch.setattr(presence_ui, "notify_personal_presence", views.append)
    return owner, callbacks, views, timers, speech


def drain(callbacks):
    turns = 0
    while callbacks:
        callbacks.pop(0)()
        turns += 1
        assert turns < 100
    return turns


def push(owner, state, *, jid=CONTACT, **fields):
    owner.on_presence_update(jid, {jid: {"lastKnownPresence": state, **fields}})


def test_hundred_online_events_keep_immediate_cache_and_one_view_paint(burst):
    owner, callbacks, views, _, _ = burst
    for index in range(100):
        jid = f"{1000 + index}@s.whatsapp.net"
        push(owner, "available", jid=jid)
        assert cached(owner, jid)["isOnline"] is True
    assert len(callbacks) == 1 and views == [] and owner.rows == []
    assert drain(callbacks) == 1
    assert views == [owner] and owner.rows == []


def test_repeated_typing_paints_one_row_and_speaks_only_the_transition(burst):
    owner, callbacks, views, timers, speech = burst
    for _ in range(100):
        push(owner, "composing")
    assert len(speech) == 0  # No active conversation: data remains silent.
    assert len(callbacks) == 1 and owner.rows == []
    assert all(timer.stopped for timer in timers[:-1])
    drain(callbacks)
    assert owner.rows == [CONTACT] and views == [owner]


def test_many_distinct_typing_rows_yield_between_bounded_paint_turns(burst):
    owner, callbacks, views, _, _ = burst
    for index in range(100):
        push(owner, "composing", jid=f"{1000 + index}@s.whatsapp.net")
    turns = 0
    while callbacks:
        before = len(owner.rows)
        callbacks.pop(0)()
        assert len(owner.rows) - before <= PresenceUiUpdates.ROWS_PER_TURN
        assert len(callbacks) <= 1
        turns += 1
    assert turns == 7 and len(set(owner.rows)) == 100 and views == [owner]


def test_explicit_denial_is_not_lost_before_a_legacy_event_or_ui_flush(burst):
    owner, callbacks, _, _, _ = burst
    merge(owner, CONTACT, {"lastKnownPresence": "unavailable", "lastSeen": 1700000000})
    push(owner, "unavailable", restricted=True, lastSeen=None, eventTimestamp=200)
    push(owner, "unavailable", eventTimestamp=201)
    assert cached(owner, CONTACT)["restricted"] is True
    assert cached(owner, CONTACT)["lastSeen"] is None
    drain(callbacks)
    assert cached(owner, CONTACT)["restricted"] is True


def test_reordered_event_cannot_replace_a_newer_pending_paint_state(burst):
    owner, callbacks, _, _, _ = burst
    push(owner, "available", eventTimestamp=200)
    push(owner, "unavailable", eventTimestamp=100)
    assert cached(owner, CONTACT)["isOnline"] is True
    drain(callbacks)
    assert owner.rows == []


@pytest.mark.parametrize("replace", ["epoch", "cache", "shutdown"])
def test_old_dirty_rows_do_not_survive_invalidation(burst, replace):
    owner, callbacks, views, _, _ = burst
    push(owner, "composing")
    if replace == "epoch": owner._contact_presence_epoch = 1
    elif replace == "cache": owner._presence_cache = {}
    else: owner._shutting_down = True
    drain(callbacks)
    assert views == [] and owner.rows == []


def test_new_epoch_reuses_one_wakeup_without_repainting_old_rows(burst):
    owner, callbacks, views, _, _ = burst
    push(owner, "composing")
    owner._contact_presence_epoch, owner._presence_cache = 1, {}
    other = "456@s.whatsapp.net"
    push(owner, "composing", jid=other)
    assert len(callbacks) == 1
    drain(callbacks)
    assert owner.rows == [other] and views == [owner]


def active_panel(owner):
    panel = _Panel()
    panel.main_window = owner
    owner.conversations_panel = panel
    owner.settings = {"user_interface": {"show_typing_row": True}}
    panel._refresh_presence_note = lambda jid: None
    return panel


def test_short_active_typing_transition_is_spoken_before_paint(burst):
    owner, callbacks, _, _, speech = burst
    active_panel(owner)
    push(owner, "composing")
    push(owner, "unavailable")
    assert len(speech) == 1 and callbacks
    drain(callbacks)
    assert not typing_row_shown(owner.conversations_panel)


def test_message_between_presence_and_paint_does_not_resurrect_typing(burst):
    owner, callbacks, _, _, speech = burst
    panel = active_panel(owner)
    push(owner, "composing")
    dismiss_typing_row_for_message(panel, {"key": {"fromMe": False}})
    assert not typing_row_shown(panel)
    drain(callbacks)
    assert not typing_row_shown(panel)
    assert len(speech) == 1
    push(owner, "composing")  # A genuinely later event may show typing again.
    drain(callbacks)
    assert typing_row_shown(panel)
    assert len(speech) == 1


def test_a_row_error_does_not_disable_unread_updates_or_lose_other_rows(burst):
    owner, callbacks, _, _, _ = burst
    seen = []
    def paint(jid):
        seen.append(jid)
        if jid == CONTACT: raise RuntimeError("synthetic widget destroyed")
    owner._refresh_chat_row_in_list = paint
    request_presence_ui(owner, CONTACT, row=True)
    request_presence_ui(owner, "456@s.whatsapp.net", row=True)
    drain(callbacks)
    assert seen == [CONTACT, "456@s.whatsapp.net"]
    titles = []
    owner._update_title = lambda: titles.append(True)
    chat_events.ChatEventsMixin._refresh_chat_row_in_list(owner, CONTACT)
    assert titles == [True]


def test_a_failed_post_can_be_retried_without_losing_dirty_state(burst):
    owner, callbacks, views, _, _ = burst
    renderer = owner._presence_ui
    renderer.post = lambda fn: (_ for _ in ()).throw(RuntimeError("synthetic scheduling failure"))
    with pytest.raises(RuntimeError): renderer.request(CONTACT)
    renderer.post = callbacks.append
    renderer.request(CONTACT)
    drain(callbacks)
    assert owner.rows == [CONTACT] and views == [owner]


def test_offline_event_clears_visible_typing_even_if_cache_expired_first(burst, monkeypatch):
    owner, callbacks, _, _, _ = burst
    monkeypatch.setattr(presence.time, "monotonic", lambda: 100)
    push(owner, "composing")
    drain(callbacks)
    owner.rows.clear()
    monkeypatch.setattr(presence.time, "monotonic", lambda: 111)
    assert cached(owner, CONTACT)["lastKnownPresence"] != "composing"
    push(owner, "unavailable")
    drain(callbacks)
    assert owner.rows == [CONTACT]
    assert owner._composing_chats[CONTACT] == {}


def test_newly_learned_display_name_still_refreshes_a_non_typing_row(burst):
    owner, callbacks, _, _, _ = burst
    owner.contacts[CONTACT] = {"pushName": "Contact"}
    push(owner, "available")
    assert owner._presence_pushname_map[CONTACT] == "Contact"
    drain(callbacks)
    assert owner.rows == [CONTACT]


def test_activity_and_its_expiry_do_not_recompute_unread_titles(burst):
    owner, callbacks, _, timers, _ = burst
    from types import MethodType
    from main_window.chat_events import ChatEventsMixin
    owner._refresh_chat_row_in_list = MethodType(ChatEventsMixin._refresh_chat_row_in_list, owner)
    owner._refresh_presence_row_in_list = MethodType(ChatEventsMixin._refresh_presence_row_in_list, owner)
    owner._update_title = lambda: pytest.fail("Presence must not recalculate unread state")
    owner.on_presence_update(CONTACT, {CONTACT: {"lastKnownPresence": "composing"}},
                             refresh_offline=False)
    drain(callbacks)
    timers[-1].callback()
    drain(callbacks)


def test_real_http_and_socket_pipeline_refreshes_three_views_with_queued_paint(monkeypatch):
    from tests.test_contact_presence_transition_acceptance import pipeline
    loop, deliver, rendered, button = pipeline(monkeypatch)
    loop.owner._presence_ui = PresenceUiUpdates(loop.owner, loop.callbacks.append)
    loop.begin()
    assert rendered() == ("Online", "Online", "Online")
    deliver("denied_event")
    from tests.test_contact_presence_freshness import PN
    assert cached(loop.owner, PN)["restricted"] is True
    assert rendered() == ("Unknown", "Contact", "")
    assert button.focuses == 0
