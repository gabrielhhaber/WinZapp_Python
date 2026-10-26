"""Missing paint lifecycle/interleaving cases; synthetic queues and clocks only."""
import itertools
from types import SimpleNamespace

import pytest

from core.contact_presence import cached, merge
from main_window import presence_ui
from main_window.presence_ui import PresenceUiUpdates, request_presence_ui
from tests.test_presence_ui_load import PresenceOwner
from tests.test_presence_ui_batching import burst, active_panel, drain, push
from tests.test_typing_row import CONTACT, _msg, dismiss_typing_row_for_message, typing_row_shown


@pytest.mark.parametrize("phase", ["views", "row"])
@pytest.mark.parametrize("replacement", ["epoch", "cache"])
def test_reentrant_replacement_preserves_new_work_for_the_next_turn(burst, monkeypatch, phase, replacement):
    owner, callbacks, _, _, _ = burst
    first, second, third = CONTACT, "456@s.whatsapp.net", "789@s.whatsapp.net"
    renderer, rows = owner._presence_ui, []
    def replace():
        if replacement == "epoch": owner._contact_presence_epoch = 1
        else: owner._presence_cache = {}
        renderer.request(third)
    def paint(jid):
        rows.append(jid)
        if jid == first and phase == "row": replace()
    owner._refresh_chat_row_in_list = paint
    def views(window, *args):
        if phase == "views" and not getattr(owner, "replaced", False):
            owner.replaced = True
            replace()
    monkeypatch.setattr(presence_ui, "_refresh_views", views)
    renderer.request(first)
    renderer.request(second)
    callbacks.pop(0)()
    assert rows == ([first] if phase == "row" else [])
    assert len(callbacks) == 1
    drain(callbacks)
    assert rows == ([first, third] if phase == "row" else [third])


@pytest.mark.parametrize("phase", ["views", "row"])
def test_shutdown_during_paint_stops_the_rest_of_that_turn(burst, monkeypatch, phase):
    owner, callbacks, _, _, _ = burst
    rows = []
    def paint(jid):
        rows.append(jid)
        owner._shutting_down = True
    owner._refresh_chat_row_in_list = paint
    if phase == "views":
        monkeypatch.setattr(presence_ui, "_refresh_views", lambda *args: setattr(owner, "_shutting_down", True))
    for index in range(30): owner._presence_ui.request(str(index))
    callbacks.pop(0)()
    assert rows == (["0"] if phase == "row" else [])
    assert callbacks == []


def test_a_reentrant_repeat_is_not_painted_twice_in_the_same_turn(burst):
    owner, callbacks, _, _, _ = burst
    rows, renderer = [], owner._presence_ui
    def paint(jid):
        rows.append(jid)
        if len(rows) == 1: renderer.request(jid)
    owner._refresh_chat_row_in_list = paint
    renderer.request(CONTACT)
    renderer.request("456@s.whatsapp.net")
    callbacks.pop(0)()
    assert rows == [CONTACT, "456@s.whatsapp.net"]
    drain(callbacks)
    assert rows == [CONTACT, "456@s.whatsapp.net", CONTACT]


def test_a_broken_observer_does_not_abort_other_consumers_or_rows(burst, monkeypatch):
    owner, callbacks, _, _, _ = burst
    panel = active_panel(owner)
    notes = []
    def broken(*args): raise RuntimeError("synthetic observer destroyed")
    panel.refresh_typing_row = broken
    panel._refresh_presence_note = notes.append
    push(owner, "composing")
    drain(callbacks)
    assert notes == [CONTACT] and owner.rows == [CONTACT]


def test_shutdown_inside_personal_observer_does_not_touch_the_panel(burst, monkeypatch):
    owner, callbacks, _, _, _ = burst
    panel = active_panel(owner)
    panel.refresh_typing_row = lambda *args: pytest.fail("Panel touched after shutdown")
    monkeypatch.setattr(presence_ui, "notify_personal_presence", lambda window: setattr(window, "_shutting_down", True))
    push(owner, "composing")
    drain(callbacks)
    assert owner.rows == []


@pytest.mark.parametrize("state", ["available", "composing", "recording"])
def test_shutdown_rejects_new_cache_timer_speech_and_ui_work(burst, state):
    owner, callbacks, views, timers, speech = burst
    active_panel(owner)
    owner._shutting_down = True
    push(owner, state)
    assert owner._presence_cache == {}
    assert callbacks == views == timers == speech == []


def test_owned_expiry_after_shutdown_only_retires_its_internal_state(burst):
    owner, callbacks, views, timers, _ = burst
    push(owner, "composing")
    drain(callbacks)
    views.clear()
    owner.rows.clear()
    owner._shutting_down = True
    timers[-1].callback()
    assert callbacks == views == owner.rows == []
    assert not owner._presence_timers and not owner._composing_chats[CONTACT]


def test_request_after_shutdown_never_posts_or_updates_typing_dismissal(burst):
    owner, callbacks, views, _, _ = burst
    owner._shutting_down = True
    request_presence_ui(owner, CONTACT, row=True, fresh=(CONTACT,))
    assert callbacks == views == []


@pytest.mark.parametrize("payload", [None, False, 0, 3.5, "bad", [], ()])
def test_invalid_outer_presence_shapes_are_ignored_before_logging(burst, payload):
    owner, callbacks, _, timers, speech = burst
    owner.on_presence_update(CONTACT, payload)
    assert owner._presence_cache == {} and callbacks == timers == speech == []


@pytest.mark.parametrize("jid", [None, False, 123, b"contact", (), {}])
def test_invalid_chat_identity_types_do_not_enter_the_pipeline(burst, jid):
    owner, callbacks, _, timers, _ = burst
    owner.on_presence_update(jid, {CONTACT: {"lastKnownPresence": "composing"}})
    assert owner._presence_cache == {} and callbacks == timers == []


@pytest.mark.parametrize("participant", [None, 123, b"contact"])
def test_invalid_participant_identity_types_are_skipped(burst, participant):
    owner, callbacks, _, timers, _ = burst
    owner.on_presence_update(CONTACT, {participant: {"lastKnownPresence": "composing"}})
    assert owner._presence_cache == {} and timers == []
    drain(callbacks)


@pytest.mark.parametrize("state", [None, True, 1, [], {}])
@pytest.mark.parametrize("denied", [False, True])
def test_malformed_activity_never_grants_data_but_an_explicit_denial_clears_it(burst, state, denied):
    owner, callbacks, _, timers, _ = burst
    old = merge(owner, CONTACT, {"lastKnownPresence": "unavailable", "lastSeen": 1700000000})
    owner.on_presence_update(CONTACT, {CONTACT: {"lastKnownPresence": state, "restricted": denied}})
    current = cached(owner, CONTACT, fresh=False)
    if denied:
        assert current["restricted"] is True and current["lastSeen"] is None
        assert current["isOnline"] is None
    else:
        assert current is old
    assert timers == []
    drain(callbacks)


def test_slow_row_work_yields_on_time_as_well_as_on_row_count(burst):
    owner, callbacks, _, _, _ = burst
    now, rows = [0.0], []
    owner._presence_ui.clock = lambda: now[0]
    def slow_paint(jid):
        rows.append(jid)
        now[0] += 0.003  # Synthetic elapsed work; no sleeping or actual widgets.
    owner._refresh_chat_row_in_list = slow_paint
    for index in range(20): owner._presence_ui.request(str(index))
    callbacks.pop(0)()
    assert 1 <= len(rows) <= 2 and len(callbacks) == 1
    drain(callbacks)
    assert len(rows) == 20 and len(set(rows)) == 20


STATES = {
    "online": {"lastKnownPresence": "available", "isOnline": True},
    "offline": {"lastKnownPresence": "unavailable", "isOnline": False, "lastSeen": 1700000000},
    "typing": {"lastKnownPresence": "composing", "isOnline": False},
    "recording": {"lastKnownPresence": "recording", "isOnline": True},
    "denied": {"lastKnownPresence": "unavailable", "restricted": True, "lastSeen": None},
    "invalidated": {"lastKnownPresence": "", "invalidated": True},
}


def matrix_owner(queued):
    callbacks, spoken = [], []
    owner = PresenceOwner()
    owner.IsShown, owner.IsIconized, owner.IsActive = lambda: True, lambda: False, lambda: True
    owner._resolve_jid_name = lambda *args, **kwargs: "Contact"
    owner.i18n = SimpleNamespace(t=lambda key: key + " {name}")
    owner.speak_output = SimpleNamespace(output=spoken.append)
    panel = active_panel(owner)
    if queued:
        owner._presence_ui = PresenceUiUpdates(owner, callbacks.append, clock=lambda: 0)
    return owner, panel, callbacks, spoken


@pytest.mark.parametrize("scope", ["private", "alias", "group"])
@pytest.mark.parametrize("middle", ["message", "hide", "switch", "replace"])
@pytest.mark.parametrize("before,after", tuple(itertools.product(STATES, repeat=2)))
def test_all_state_pairs_and_interleavings_preserve_sequential_semantics(
        burst, monkeypatch, scope, middle, before, after):
    # 6 x 6 states x 4 interleavings x 3 identities. Compare actual ordered
    # handlers with immediate paint and with queued paint; no expected-state
    # implementation is copied into the test.
    import core.contact_presence as model
    monkeypatch.setattr(model.time, "monotonic", lambda: 100)
    outcomes = []
    for queued in (False, True):
        owner, panel, callbacks, spoken = matrix_owner(queued)
        chat = "111-222@g.us" if scope == "group" else CONTACT
        participant = CONTACT
        if scope == "alias":
            participant = chat = "123@lid"
            owner._lid_to_phone[chat], owner._phone_to_lid[CONTACT] = CONTACT, chat
        panel.conversation = {"remoteJid": "111-222@g.us" if scope == "group" else CONTACT}
        owner.on_presence_update(chat, {participant: dict(STATES[before])}, refresh_offline=False)
        if middle == "message":
            dismiss_typing_row_for_message(panel, {
                "key": {"fromMe": False, "participant": CONTACT}})
        elif middle == "hide":
            owner._window_hidden = True
        elif middle == "switch":
            panel.conversation = {"remoteJid": "999@s.whatsapp.net"}
        else:
            owner._contact_presence_epoch = 1
            owner._presence_cache = {}
            owner._composing_chats.clear()
            for timer in owner._presence_timers.values(): timer.Stop()
            owner._presence_timers.clear()
        owner.on_presence_update(chat, {participant: dict(STATES[after])}, refresh_offline=False)
        drain(callbacks)
        outcomes.append((owner._presence_cache, owner._composing_chats,
                         getattr(panel, "_typing_row_dismissed", set()),
                         panel.messages_list.rows, panel.messages_list.focused, spoken))
    assert outcomes[0] == outcomes[1]


def test_two_windows_keep_independent_pending_state_and_privacy(burst):
    first, _, first_queue, _ = matrix_owner(True)
    second, _, second_queue, _ = matrix_owner(True)
    push(first, "unavailable", restricted=True, lastSeen=None)
    push(second, "available", restricted=False)
    drain(second_queue)
    assert first_queue
    drain(first_queue)
    assert cached(first, CONTACT)["restricted"] is True
    assert cached(second, CONTACT)["isOnline"] is True


@pytest.mark.parametrize("part", ["personal", "typing", "note", "row"])
def test_render_errors_report_only_the_class_not_private_exception_text(burst, monkeypatch, caplog, part):
    owner, callbacks, _, _, _ = burst
    panel = active_panel(owner)
    secret = "synthetic-private-name-and-payload"
    def broken(*args): raise RuntimeError(secret)
    if part == "personal": monkeypatch.setattr(presence_ui, "notify_personal_presence", broken)
    elif part == "typing": panel.refresh_typing_row = broken
    elif part == "note": panel._refresh_presence_note = broken
    else: owner._refresh_chat_row_in_list = broken
    push(owner, "composing")
    drain(callbacks)
    assert "RuntimeError" in caplog.text and secret not in caplog.text


@pytest.mark.parametrize("state,expected", [("typing", "composing"), ("recording_audio", "recording")])
@pytest.mark.parametrize("wid", [False, True])
def test_actual_duplicate_group_wire_events_keep_scope_and_one_announcement_without_node(burst, monkeypatch, state, expected, wid):
    from core.websocket_client import WebSocketClient
    import core.websocket_client as transport
    owner, _, _, _, speech = burst
    panel = active_panel(owner)
    group = "111-222@g.us"
    panel.conversation = {"remoteJid": group}
    callbacks = []
    owner._presence_ui = PresenceUiUpdates(owner, callbacks.append, clock=lambda: 0)
    monkeypatch.setattr(transport.wx, "CallAfter", callbacks.append)
    peer = SimpleNamespace(main_window=owner, _presence_delivery_generation=0,
                           _belongs_to_this_session=lambda info: True)
    event = {"id": {"_serialized": group} if wid else group,
             "isGroup": False, "state": state,
             "participants": [{"id": {"_serialized": CONTACT} if wid else CONTACT, "state": state}]}
    WebSocketClient.on_wpp_presence_changed(peer, event)
    WebSocketClient.on_wpp_presence_changed(peer, event)
    assert len(callbacks) == 2  # Original ordered data delivery is retained.
    drain(callbacks)
    assert cached(owner, CONTACT) == {}
    assert cached(owner, CONTACT, chat=group)["lastKnownPresence"] == expected
    assert len(speech) == 1 and owner.rows == [group]


def test_followup_post_failure_retains_rows_and_can_resume_on_a_new_event(burst):
    owner, callbacks, _, _, _ = burst
    renderer, posts = owner._presence_ui, []
    def post(callback):
        posts.append(True)
        if len(posts) == 2: raise RuntimeError("synthetic scheduler stopped")
        callbacks.append(callback)
    renderer.post = post
    for index in range(20): renderer.request(str(index))
    with pytest.raises(RuntimeError): callbacks.pop(0)()
    assert len(owner.rows) == 16 and not renderer._pending
    renderer.request("later")
    drain(callbacks)
    assert len(owner.rows) == len(set(owner.rows)) == 21


@pytest.mark.parametrize("group", [False, True])
def test_own_message_does_not_dismiss_the_other_persons_queued_activity(burst, group):
    owner, callbacks, _, _, _ = burst
    panel = active_panel(owner)
    chat = "111-222@g.us" if group else CONTACT
    panel.conversation = {"remoteJid": chat}
    owner.on_presence_update(chat, {CONTACT: {"lastKnownPresence": "composing"}})
    dismiss_typing_row_for_message(panel, {"key": {"fromMe": True, "participant": CONTACT}})
    drain(callbacks)
    assert typing_row_shown(panel)


def test_group_message_dismisses_only_its_author_before_a_shared_paint(burst):
    owner, callbacks, _, _, _ = burst
    panel = active_panel(owner)
    group, other = "111-222@g.us", "456@s.whatsapp.net"
    panel.conversation = {"remoteJid": group}
    owner.on_presence_update(group, {CONTACT: {"lastKnownPresence": "composing"},
                                    other: {"lastKnownPresence": "recording"}})
    dismiss_typing_row_for_message(panel, {"key": {"fromMe": False, "participant": CONTACT}})
    drain(callbacks)
    text = panel.messages_list.rows[-1]
    assert "recording_text" in text and "typing_text" not in text


def test_reciprocal_alias_learned_before_paint_uses_the_current_conversation(burst):
    owner, callbacks, _, _, speech = burst
    panel = active_panel(owner)
    lid = "123@lid"
    push(owner, "composing", jid=lid)
    owner._lid_to_phone[lid], owner._phone_to_lid[CONTACT] = CONTACT, lid
    drain(callbacks)
    assert typing_row_shown(panel)
    assert cached(owner, CONTACT)["lastKnownPresence"] == "composing"
    assert speech == []  # Rendering never replays a delayed announcement.


def test_shutdown_during_one_group_event_stops_later_participants_and_db_work(burst):
    owner, callbacks, _, timers, speech = burst
    panel = active_panel(owner)
    group, other = "111-222@g.us", "456@s.whatsapp.net"
    panel.conversation = {"remoteJid": group}
    owner.contacts = {CONTACT: {"pushName": "First"}, other: {"pushName": "Second"}}
    owner.db = SimpleNamespace(queue_presence_names=lambda *args: pytest.fail("DB touched after shutdown"))
    def announce(text):
        speech.append(text)
        owner._shutting_down = True
    owner.speak_output.output = announce
    owner.on_presence_update(group, {CONTACT: {"lastKnownPresence": "composing"},
                                    other: {"lastKnownPresence": "recording"}})
    assert len(speech) == len(timers) == 1 and not callbacks
    assert other not in owner._composing_chats[group]


def test_one_large_group_event_keeps_participant_scope_with_one_dirty_row(burst, record_testsuite_property):
    owner, callbacks, _, _, speech = burst
    panel = active_panel(owner)
    group = "111-222@g.us"
    panel.conversation = {"remoteJid": group}
    entries = {f"{1000 + i}@s.whatsapp.net": {"lastKnownPresence": "composing"} for i in range(256)}
    owner.on_presence_update(group, entries, refresh_offline=False)
    assert set(owner._composing_chats[group]) == set(entries)
    assert all(cached(owner, jid) == {} for jid in entries)
    assert len(callbacks) == 1
    drain(callbacks)
    assert owner.rows == [group]
    record_testsuite_property("group_participants", len(entries))
    record_testsuite_property("existing_spoken_transitions", len(speech))


def test_repeated_known_names_do_not_repeat_metadata_admissions(burst, record_testsuite_property):
    owner, callbacks, _, _, _ = burst
    writes = []
    owner.db = SimpleNamespace(queue_presence_names=lambda value: writes.append(dict(value)))
    jids = [f"{1000 + i}@s.whatsapp.net" for i in range(32)]
    owner.contacts = {jid: {"pushName": "Contact"} for jid in jids}
    for jid in jids: push(owner, "available", jid=jid)
    drain(callbacks)
    learned = len(writes)
    assert learned and set(writes[-1]) == set(jids)
    for jid in jids: push(owner, "unavailable", jid=jid)
    drain(callbacks)
    assert len(writes) == learned
    record_testsuite_property("metadata_admissions_when_learning_32_names", learned)
