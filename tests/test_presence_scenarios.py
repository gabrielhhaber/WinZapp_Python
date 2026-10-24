"""Offline transition/race matrix; timers, workers and controls are plain stubs."""
from itertools import product

import pytest

from core import contact_presence as model
from core.online_presence import OnlinePresenceSender
from main_window.identity import IdentityMixin
from main_window.contact_presence import ContactPresenceMixin
from tests.test_contact_presence_freshness import Owner, PN, LID, scheduled
from tests.test_online_presence_order import Window, workers
from tests.test_presence_speech_gating import _Stub, _Timer, GROUP_JID


@pytest.mark.parametrize("initial,following,online", tuple(product(
    ("composing", "recording", "available", "unavailable"),
    ("composing", "recording", "paused", "unavailable"), (True, False, None))))
def test_transition_replaces_activity_and_preserves_independent_visibility(initial, following, online):
    owner = Owner()
    owner._lid_to_phone[LID], owner._phone_to_lid[PN] = PN, LID
    model.merge(owner, PN, {"lastKnownPresence": initial, "isOnline": online}, now=100)
    data = {"lastKnownPresence": following, "isOnline": online, "eventTimestamp": 1700000002}
    latest = model.merge(owner, LID, data, now=105)
    assert model.cached(owner, PN, now=105, fresh=False) is latest
    if following in ("composing", "recording"):
        assert model.activity(model.cached(owner, PN, now=114.999)) == following
        assert not model.activity(model.cached(owner, PN, now=115))
        assert latest["lastSeen"] is None
    else:
        assert not model.activity(latest)
    # An unknown flag may inherit a separately observed state, but activity alone
    # cannot establish online. Only the initial available observation can do so.
    expected_online = online is True or (online is None and initial == "available" and following != "unavailable")
    assert model.visible_online(latest) is expected_online
    stale = model.merge(owner, PN, {"lastKnownPresence": "recording", "eventTimestamp": 1700000001}, now=106)
    assert stale is latest


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("replacement", ["recording", "paused", "reconnect"])
def test_already_queued_old_expiry_cannot_clear_a_new_activity(monkeypatch, group, replacement):
    import main_window.chat_events as events
    from main_window.contact_presence import ContactPresenceMixin
    clock, timers = [100], []
    monkeypatch.setattr(model.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(events.wx, "CallLater", lambda ms, fn: timers.append(fn) or _Timer())
    chat = GROUP_JID if group else PN
    owner = _Stub(conversation_jid=chat)
    owner._refresh_open_contact_presence_note = lambda: None
    owner.on_presence_update(chat, {PN: {"lastKnownPresence": "composing", "isOnline": False}})
    old_expiry = timers[-1]
    clock[0] = 105
    if replacement == "reconnect":
        ContactPresenceMixin._invalidate_contact_presence(owner)
    elif replacement == "paused":
        owner.on_presence_update(chat, {PN: {"lastKnownPresence": "paused"}})
    owner.on_presence_update(chat, {PN: {"lastKnownPresence": "recording", "isOnline": False}})
    current_timer = owner._presence_timers[(chat, PN)]
    clock[0] = 110
    old_expiry()  # Stop() cannot retract a callback already queued by the GUI.
    assert owner._composing_chats[chat][PN] == "recording"
    assert owner._presence_timers[(chat, PN)] is current_timer
    assert model.activity(model.cached(owner, PN, chat=chat if group else None)) == "recording"
    clock[0] = 115
    timers[-1]()
    assert owner._composing_chats[chat] == {}


@pytest.mark.parametrize("change", ["shutdown", "disconnect", "mapping", "close", "blank", "switch", "replace_panel", "live_event"])
def test_queued_profile_activity_cannot_cross_lifecycle_or_identity(monkeypatch, change):
    import main_window.identity as identity
    owner, callbacks, deliveries = Owner(), [], []
    owner._fetch_contact_presence = lambda jid: {"lastKnownPresence": "recording", "isOnline": False}
    owner.on_presence_update = lambda *args, **kwargs: deliveries.append((args, kwargs))
    monkeypatch.setattr(identity.wx, "CallAfter", callbacks.append)
    IdentityMixin.get_last_seen(owner, LID if change == "mapping" else PN)
    if change == "shutdown":
        owner._shutting_down = True
    elif change == "disconnect":
        owner._wa_connected = False
    elif change == "mapping":
        owner._lid_to_phone[LID], owner._phone_to_lid[PN] = PN, LID
    elif change == "close":
        owner.conversations_panel.conversation = None
    elif change == "blank":
        owner.conversations_panel.conversation = {"remoteJid": ""}
    elif change == "switch":
        owner.conversations_panel.conversation = {"remoteJid": "789@s.whatsapp.net"}
    elif change == "replace_panel":
        from types import SimpleNamespace
        owner.conversations_panel = SimpleNamespace(conversation={"remoteJid": PN}, _contact_presence_visit=1)
    else:
        model.merge(owner, PN, {"lastKnownPresence": "composing", "isOnline": False})
    callbacks.pop()()
    assert deliveries == []


def test_profile_activity_accepted_once_in_current_visit_without_recursive_query(monkeypatch):
    import main_window.identity as identity
    owner, callbacks, deliveries = Owner(), [], []
    owner._fetch_contact_presence = lambda jid: {"lastKnownPresence": "composing", "isOnline": False}
    owner.on_presence_update = lambda *args, **kwargs: deliveries.append((args, kwargs))
    monkeypatch.setattr(identity.wx, "CallAfter", callbacks.append)
    IdentityMixin.get_last_seen(owner, PN)
    callbacks.pop()()
    assert deliveries == [((PN, {PN: {"lastKnownPresence": "composing", "isOnline": False}}), {"refresh_offline": False})]


@pytest.mark.parametrize("state", ["composing", "recording"])
@pytest.mark.parametrize("online", [True, False, None])
def test_all_views_expire_activity_without_reviving_hidden_online_or_old_timestamp(monkeypatch, state, online):
    from tests.test_contact_presence_views import views
    from main_window.chat_events import ChatEventsMixin
    from ui.conversation_panel.conversation_info import ConversationInfoMixin
    from ui.dialogs.conversation_data_dialog import ConversationDataDialog
    owner = Owner()
    model.merge(owner, PN, {"lastKnownPresence": "unavailable", "lastSeen": 1700000000}, now=99)
    model.merge(owner, PN, {"lastKnownPresence": state, "isOnline": online}, now=100)
    monkeypatch.setattr(model.time, "monotonic", lambda: 110)
    panel, dialog, button, text, _ = views(owner)
    ConversationInfoMixin._refresh_presence_note(panel, PN)
    announcement = ChatEventsMixin._presence_announcement_for_chat(owner, panel.conversation)
    ConversationDataDialog._populate_personal_unsafe(dialog, {"lastSeenTs": 1700000000})
    assert announcement == ("Online" if online else "Unknown")
    assert ("Online" in button.text) is (online is True)
    assert ("Online" in text.text) is (online is True)
    assert "Seen" not in text.text and "indicator" not in text.text


def test_window_policy_full_boolean_matrix_never_sends_online_with_any_inhibitor(workers):
    # Complete 2**8 finite grid, not sampled cases. No WinEvent or wx instance.
    flags = ("_wa_connected", "token", "_shutting_down", "background_mode",
             "_window_hidden", "shown", "iconized", "active")
    for values in product((False, True), repeat=len(flags)):
        window = Window()
        for flag, value in zip(flags, values):
            setattr(window, flag, ("synthetic" if value else "") if flag == "token" else value)
        window._sync_online_presence()
        while workers:
            workers.pop(0)()
        connected, token, shutdown, background, hidden, shown, minimized, active = values
        if not connected or not token or shutdown:
            assert window.sent == [] and not window._presence_timer.running
        elif background or hidden or not shown or minimized or not active:
            assert window.sent == ["unavailable"] and window._presence_timer.running
        else:
            assert window.sent == ["available"] and window._presence_timer.running


def test_all_short_pending_sequences_keep_only_latest_state_and_its_context(workers):
    for length in range(1, 5):
        for sequence in product(("available", "unavailable", None), repeat=length):
            sent = []
            sender = OnlinePresenceSender(lambda state, **kw: sent.append((state, kw["diagnostic"])))
            for index, state in enumerate(sequence):
                sender.submit(state, diagnostic=index if state else None)
            while workers:
                workers.pop(0)()
            assert sent == ([] if sequence[-1] is None else [(sequence[-1], length - 1)])


@pytest.mark.parametrize("state", ["composing", "recording", "unavailable"])
@pytest.mark.parametrize("later_event", [False, True])
def test_http_worker_to_real_event_pipeline_is_single_delivery_without_query_loop(scheduled, monkeypatch, state, later_event):
    import main_window.chat_events as events
    from core.contact_presence_query import PresenceRead
    _, pending_workers, callbacks = scheduled
    class EventOwner(_Stub, ContactPresenceMixin):
        pass
    owner = EventOwner(conversation_jid=PN)
    owner.wpp_server, owner.wpp_port, owner.token = "http://invalid.test", 1, "synthetic"
    owner._wa_connected = True
    owner.subscribe_presence = lambda jid: None
    owner._query_contact_presence = lambda jid: PresenceRead(
        "ready", {"lastKnownPresence": state, "isOnline": False})
    timers = []
    watchdogs = []
    def call_later(ms, fn):
        timer = SimpleNamespace(stopped=False)
        timer.Stop = lambda: setattr(timer, "stopped", True)
        if ms == 10_000:
            timers.append(fn)
        else:
            assert ms == 30_000
            watchdogs.append(timer)
        return timer
    from types import SimpleNamespace
    monkeypatch.setattr(events.wx, "CallLater", call_later)
    owner._refresh_open_contact_presence()
    assert len(pending_workers) == 1
    pending_workers.pop()()
    if later_event:
        owner.on_presence_update(PN, {PN: {"lastKnownPresence": "recording", "isOnline": False}})
    callbacks.pop()()
    assert not pending_workers and not callbacks
    assert model.activity(model.cached(owner, PN)) == ("recording" if later_event else
                                                      state if state != "unavailable" else "")
    assert len(owner.speak_output.outputs) == (1 if later_event or state != "unavailable" else 0)
    assert len(timers) == (1 if later_event or state != "unavailable" else 0)
    assert len(watchdogs) == 1 and watchdogs[0].stopped


@pytest.mark.parametrize("state", ["composing", "recording"])
def test_group_activity_cannot_overwrite_or_suppress_private_chat_activity(monkeypatch, state):
    import main_window.chat_events as events
    monkeypatch.setattr(events.wx, "CallLater", lambda *args: _Timer())
    owner = _Stub(conversation_jid=GROUP_JID)
    model.merge(owner, PN, {"lastKnownPresence": "unavailable", "lastSeen": 1700000000})
    private = model.cached(owner, PN, fresh=False)
    owner.on_presence_update(GROUP_JID, {PN: {"lastKnownPresence": state, "isOnline": False}})
    assert model.cached(owner, PN, fresh=False) is private
    assert owner._composing_chats[GROUP_JID][PN] == state
    owner.conversations_panel.conversation = {"remoteJid": PN}
    owner.on_presence_update(PN, {PN: {"lastKnownPresence": state, "isOnline": False}})
    assert len(owner.speak_output.outputs) == 2  # One start in each distinct conversation.


@pytest.mark.parametrize("with_timestamp", [False, True])
def test_same_participant_in_two_groups_has_independent_order_and_announcements(monkeypatch, with_timestamp):
    import main_window.chat_events as events
    monkeypatch.setattr(events.wx, "CallLater", lambda *args: _Timer())
    owner = _Stub(conversation_jid=GROUP_JID)
    other = "56789-1234@g.us"
    owner.on_presence_update(GROUP_JID, {PN: {"lastKnownPresence": "composing", **({"eventTimestamp": 1700000010} if with_timestamp else {})}})
    owner.conversations_panel.conversation = {"remoteJid": other}
    action = "recording" if with_timestamp else "composing"
    owner.on_presence_update(other, {PN: {"lastKnownPresence": action, **({"eventTimestamp": 1700000005} if with_timestamp else {})}})
    assert owner._composing_chats[GROUP_JID][PN] == "composing"
    assert owner._composing_chats[other][PN] == action
    assert len(owner.speak_output.outputs) == 2


@pytest.mark.parametrize("raw,expected", [("typing", "composing"), ("recording_audio", "recording")])
@pytest.mark.parametrize("group", [False, True])
def test_actual_socket_normalization_to_event_cache_preserves_chat_and_activity(monkeypatch, raw, expected, group):
    import core.websocket_client as transport
    import main_window.chat_events as events
    from tests.test_contact_presence_connection import Peer
    callbacks = []
    monkeypatch.setattr(transport.wx, "CallAfter", lambda fn, *a, **kw: callbacks.append(lambda: fn(*a, **kw)))
    monkeypatch.setattr(events.wx, "CallLater", lambda *args: _Timer())
    chat = GROUP_JID if group else PN
    owner = _Stub(conversation_jid=chat)
    owner._lid_to_phone[LID], owner._phone_to_lid[PN] = PN, LID
    peer = Peer()
    peer.main_window = owner
    payload = {"session": "synthetic", "id": {"_serialized": GROUP_JID if group else LID},
               "isGroup": False, "state": raw, "isOnline": False, "t": 1700000000000}
    if group:
        payload["participants"] = [{"id": {"_serialized": LID}, "state": raw}]
    peer.on_wpp_presence_changed(payload)
    assert len(callbacks) == 1
    callbacks.pop()()
    assert owner._composing_chats[chat][PN] == expected
    assert len(owner.speak_output.outputs) == 1
    entry = model.cached(owner, PN, chat=GROUP_JID if group else None)
    assert model.activity(entry) == expected and entry["lastSeen"] is None
    if group:
        assert model.cached(owner, PN) == {}
    else:
        assert entry["isOnline"] is False
