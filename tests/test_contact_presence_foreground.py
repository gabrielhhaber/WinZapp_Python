"""Focus, active personal dialog and activity expiry: fake UI/HTTP/workers only."""
from types import MethodType, SimpleNamespace
import threading
import weakref
import gc

import pytest

from core import contact_presence as presence
from main_window.chat_events import ChatEventsMixin
from main_window.window_lifecycle import WindowLifecycleMixin
from tests.test_contact_presence_deferred_refresh import Clockwork
from tests.test_contact_presence_freshness import PN, LID
from tests.test_contact_presence_views import I18n
from tests.test_personal_presence_updates import Text
from tests.test_presence_speech_gating import _Stub
from ui.dialogs.conversation_data_dialog import ConversationDataDialog


@pytest.fixture
def loop(monkeypatch):
    import main_window.chat_events as events
    import main_window.contact_presence as coordinator
    import ui.dialogs.personal_presence_reader as reader
    loop = Clockwork(monkeypatch)
    monkeypatch.setattr(events, "wx", coordinator.wx)
    monkeypatch.setattr(reader, "wx", SimpleNamespace(
        EVT_ACTIVATE=reader.wx.EVT_ACTIVATE, CallAfter=coordinator.wx.CallAfter))
    loop.owner._sync_online_presence = lambda: None
    loop.owner._refresh_accounts_menu_if_stale = lambda: None
    return loop


def use_events(loop):
    owner = loop.owner
    stub = _Stub(conversation_jid=PN)
    for name in ("_composing_chats", "_presence_pushname_map", "contacts", "chats", "db",
                 "settings", "speak_output", "_resolve_jid_name", "_refresh_chat_row_in_list"):
        setattr(owner, name, getattr(stub, name))
    owner.on_presence_update = MethodType(ChatEventsMixin.on_presence_update, owner)
    owner.i18n = I18n()
    return owner


class Dialog:
    """Weak-referenceable widget stand-in; never constructs a wx object."""
    def __init__(self, owner, jid=PN):
        self._mw, self._jid, self._i18n, self._name = owner, jid, I18n(), "Contact"
        from ui.dialogs.personal_presence_reader import capture_personal_reader_visit
        capture_personal_reader_visit(self)
        self._closed, self._info_ctrl = threading.Event(), Text(windows=True)
        self.active = self.shown = True
        self.handlers = []

    def IsActive(self):
        return self.active

    def IsShown(self):
        return self.shown

    def Bind(self, kind, handler):
        self.handlers.append((kind, handler))

    def Unbind(self, kind, handler):
        self.handlers.remove((kind, handler))

    def populate(self):
        ConversationDataDialog._populate_personal_unsafe(self, {"aboutText": "Biography"})


def test_focus_return_reads_missing_snapshot_without_waiting_for_tick(loop):
    loop.flags["active"] = False
    loop.ready()
    loop.begin()
    assert not loop.reads
    loop.flags["active"] = True
    WindowLifecycleMixin._apply_window_activate(loop.owner, True)
    loop.drain()
    assert len(loop.reads) == 1
    assert presence.cached(loop.owner, PN)["lastSeen"] == 1700000000


def test_activity_expiry_reads_missing_snapshot_without_an_offline_push(loop):
    owner = use_events(loop)
    loop.ready()
    owner.on_presence_update(PN, {PN: {"lastKnownPresence": "composing", "isOnline": False}})
    assert not loop.reads
    loop.advance(110)
    assert len(loop.reads) == 1
    assert presence.cached(owner, PN)["lastSeen"] == 1700000000
    assert len(owner.speak_output.outputs) == 1


def test_active_matching_personal_dialog_keeps_current_contact_readable(loop):
    use_events(loop)
    loop.flags["active"] = False
    loop.ready()
    dialog = Dialog(loop.owner)
    dialog.populate()
    loop.begin()
    assert len(loop.reads) == 1
    assert "Seen" in dialog._info_ctrl.text
    assert dialog._info_ctrl.focuses == dialog._info_ctrl.events == 1


@pytest.mark.parametrize("flag", ["hidden", "background", "minimized", "not_shown", "inactive",
                                "panel_hidden", "detail_hidden", "disconnected", "shutdown", "closed", "group"])
def test_focus_request_rechecks_actual_window_and_conversation(loop, flag):
    owner = loop.owner
    if flag in ("hidden", "background", "shutdown"):
        setattr(owner, {"hidden": "_window_hidden", "background": "background_mode", "shutdown": "_shutting_down"}[flag], True)
    elif flag in ("minimized", "not_shown", "inactive"):
        loop.flags[{"minimized": "iconized", "not_shown": "shown", "inactive": "active"}[flag]] = flag == "minimized"
    elif flag == "panel_hidden":
        owner.conversations_panel.IsShown = lambda: False
    elif flag == "detail_hidden":
        owner.conversations_panel.conversation_panel = SimpleNamespace(IsShown=lambda: False)
    elif flag == "disconnected":
        owner._wa_connected = False
    elif flag == "closed":
        owner.conversations_panel.conversation = None
    elif flag == "group":
        owner.conversations_panel.conversation = {"remoteJid": "123@g.us"}
    WindowLifecycleMixin._apply_window_activate(owner, True)
    loop.drain()
    assert not loop.reads and not loop.workers


@pytest.mark.parametrize("data", [
    {"lastKnownPresence": "available", "isOnline": True},
    {"lastKnownPresence": "unavailable", "lastSeen": 1700000000},
    {"lastKnownPresence": "composing", "isOnline": False},
    {"lastKnownPresence": "recording", "isOnline": False},
    {"lastKnownPresence": "unavailable", "restricted": True},
])
def test_focus_return_does_not_refetch_usable_or_restricted_data(loop, data):
    presence.merge(loop.owner, PN, data)
    WindowLifecycleMixin._apply_window_activate(loop.owner, True)
    loop.drain()
    assert not loop.reads


def test_focus_burst_reuses_existing_cooldown_and_single_followup(loop):
    loop.begin()
    loop.now = 101
    loop.offline()
    for _ in range(100):
        WindowLifecycleMixin._apply_window_activate(loop.owner, True)
    assert len([t for t in loop.timers if t.active]) == 1
    assert loop.timers[-1].at == 110
    loop.ready()
    loop.advance(110)
    assert len(loop.reads) == 2
    loop.advance(900)
    assert len(loop.reads) == 2


def test_focus_loss_does_not_create_a_read(loop):
    WindowLifecycleMixin._apply_window_activate(loop.owner, False)
    loop.drain()
    assert not loop.reads


@pytest.mark.parametrize("change", ["other_contact", "group", "hidden", "inactive", "closed", "stopped", "reopened", "new_panel", "destroyed"])
def test_modal_reader_cannot_authorize_an_unrelated_or_old_context(loop, change):
    from ui.dialogs.personal_presence import stop_personal_presence
    dialog = Dialog(loop.owner)
    dialog.populate()
    loop.flags["active"] = False
    if change == "other_contact":
        dialog._jid = "999@s.whatsapp.net"
        dialog.populate()
    elif change == "group":
        dialog._jid = "123@g.us"
        dialog.populate()
    elif change == "hidden":
        dialog.shown = False
    elif change == "inactive":
        dialog.active = False
    elif change == "closed":
        dialog._closed.set()
    elif change == "stopped":
        stop_personal_presence(dialog)
    elif change == "reopened":
        loop.owner.conversations_panel._contact_presence_visit += 1
    elif change == "new_panel":
        loop.owner.conversations_panel = SimpleNamespace(**vars(loop.owner.conversations_panel))
    else:
        def destroyed():
            raise RuntimeError("synthetic destroyed widget")
        dialog.IsActive = destroyed
    loop.begin()
    assert not loop.reads


def test_modal_reader_accepts_only_reciprocal_alias_mapping(loop):
    use_events(loop)
    loop.flags["active"] = False
    owner = loop.owner
    owner._lid_to_phone[LID], owner._phone_to_lid[PN] = PN, LID
    dialog = Dialog(owner, LID)
    dialog.populate()
    loop.ready()
    loop.drain()
    assert len(loop.reads) == 1
    assert "Seen" in dialog._info_ctrl.text


def test_one_way_alias_mapping_does_not_expand_modal_read_scope(loop):
    loop.flags["active"] = False
    loop.owner._lid_to_phone[LID] = PN
    dialog = Dialog(loop.owner, LID)
    dialog.populate()
    loop.begin()
    assert not loop.reads


def test_modal_reactivation_requests_once_and_close_invalidates_queued_callback(loop):
    from ui.dialogs.personal_presence import stop_personal_presence
    dialog = Dialog(loop.owner)
    dialog.active = False
    dialog.populate()
    loop.flags["active"] = False
    loop.drain()
    dialog.active = True
    skipped = []
    event = SimpleNamespace(GetActive=lambda: True, Skip=lambda: skipped.append(True))
    assert len(dialog.handlers) == 1
    dialog.handlers[0][1](event)
    stop_personal_presence(dialog)
    loop.drain()
    assert not loop.reads and skipped == [True] and not dialog.handlers


def test_modal_activation_delivers_to_the_existing_reader_and_is_silent(loop):
    owner = use_events(loop)
    loop.flags["active"] = False
    dialog = Dialog(owner)
    dialog.active = False
    dialog.populate()
    loop.drain()
    assert not loop.reads
    loop.ready()
    dialog.active = True
    dialog.handlers[0][1](SimpleNamespace(GetActive=lambda: True, Skip=lambda: None))
    loop.drain()
    assert len(loop.reads) == 1 and "Seen" in dialog._info_ctrl.text
    assert dialog._info_ctrl.focuses == dialog._info_ctrl.events == 1
    assert not owner.speak_output.outputs


def test_modal_rerender_has_one_handler_and_does_not_retain_the_dialog(loop):
    dialog = Dialog(loop.owner)
    dialog.active = False
    dialog.populate()
    dialog.populate()
    assert len(dialog.handlers) == 1
    reference = weakref.ref(dialog)
    del dialog
    gc.collect()
    loop.drain()
    assert reference() is None and not loop.owner._personal_presence_views


@pytest.mark.parametrize("state", ["composing", "recording"])
@pytest.mark.parametrize("gate", ["other_contact", "group", "inactive", "denied", "online", "hidden", "disconnected"])
def test_activity_expiry_does_not_expand_scope_or_override_visibility(loop, state, gate):
    owner = use_events(loop)
    jid = "123@g.us" if gate == "group" else "999@s.whatsapp.net" if gate == "other_contact" else PN
    data = {"lastKnownPresence": state, "isOnline": gate == "online"}
    if gate == "denied":
        data["restricted"] = True
    owner.on_presence_update(jid, {jid: data})
    if gate == "inactive":
        loop.flags["active"] = False
    elif gate == "hidden":
        owner._window_hidden = True
    elif gate == "disconnected":
        owner._wa_connected = False
    loop.advance(110)
    assert not loop.reads


@pytest.mark.parametrize("trigger", ["focus", "modal", "expiry", "tick"])
def test_stale_explicit_denial_never_authorizes_new_request(loop, trigger):
    owner = use_events(loop)
    presence.merge(owner, PN, {"lastKnownPresence": "unavailable", "restricted": True})
    loop.now = 300
    if trigger == "focus":
        WindowLifecycleMixin._apply_window_activate(owner, True)
    elif trigger == "modal":
        loop.flags["active"] = False
        dialog = Dialog(owner)
        dialog.populate()
    elif trigger == "expiry":
        owner.on_presence_update(PN, {PN: {"lastKnownPresence": "composing", "isOnline": False}})
        loop.advance(310)
    else:
        owner._refresh_open_contact_presence()
    loop.drain()
    assert not loop.reads


@pytest.mark.parametrize("state", ["composing", "recording"])
def test_http_activity_result_cannot_start_an_autonomous_expiry_poll_loop(loop, state):
    owner = use_events(loop)
    loop.answer = {"lastKnownPresence": state, "isOnline": False}
    owner.on_presence_update(PN, {PN: {"lastKnownPresence": state, "isOnline": False}})
    loop.advance(150)
    assert len(loop.reads) == 1
    assert not [timer for timer in loop.timers if timer.active]


def test_newer_activity_owns_expiry_and_only_one_missing_data_read(loop):
    owner = use_events(loop)
    loop.ready()
    owner.on_presence_update(PN, {PN: {"lastKnownPresence": "composing", "isOnline": False}})
    old = next(t for t in loop.timers if t.active)
    loop.now = 105
    owner.on_presence_update(PN, {PN: {"lastKnownPresence": "recording", "isOnline": False}})
    loop.now = 110
    old.callback()  # Already queued before Stop; cannot trigger a read.
    loop.drain()
    assert not loop.reads
    loop.advance(115)
    assert len(loop.reads) == 1


def test_modal_expiry_updates_text_and_native_selection_without_extra_speech(loop):
    owner = use_events(loop)
    owner.on_presence_update(PN, {PN: {"lastKnownPresence": "recording", "isOnline": False}})
    dialog = Dialog(owner)
    dialog.populate()
    text = dialog._info_ctrl
    text.SetSelection(1, 4)
    loop.flags["active"] = False
    loop.ready()
    loop.advance(110)
    assert len(loop.reads) == 1 and "Seen" in text.text
    assert text.GetSelection() == (1, 4) and text.GetInsertionPoint() == 4
    assert text.focuses == text.events == 1
    assert len(owner.speak_output.outputs) == 1


@pytest.mark.parametrize("outcome", ["pending", "withheld", "error", "malformed", "unsupported"])
def test_expiry_terminal_or_pending_result_has_no_automatic_followup(loop, outcome):
    from core.contact_presence_query import PresenceRead
    owner = use_events(loop)
    owner.subscribe_presence = lambda jid, **kw: owner.subscriptions.append((jid, kw))
    owner._query_contact_presence = lambda jid: loop.reads.append((loop.now, jid)) or PresenceRead(outcome)
    owner.on_presence_update(PN, {PN: {"lastKnownPresence": "composing", "isOnline": False}})
    loop.advance(1000)
    assert len(loop.reads) == 1


def test_focus_and_modal_demands_cannot_bypass_the_two_physical_get_slots(loop):
    from tests.test_contact_presence_worker_recovery import callbacks_only
    callbacks_only(loop)
    dialog = Dialog(loop.owner)
    dialog.populate()
    loop.owner._refresh_open_contact_presence()
    loop.advance(160)
    for _ in range(100):
        WindowLifecycleMixin._apply_window_activate(loop.owner, True)
        dialog._personal_presence_view.reader.request()
    assert len(loop.workers) == 2
    assert len(loop.owner._contact_presence_work.running) == 2
    assert not loop.reads


@pytest.mark.parametrize("pair,expected", [
    ((PN, PN), True), ((PN.replace("@s.whatsapp.net", "@c.us"), PN), True),
    ((LID, PN), False), (("123@g.us", "123@g.us"), False),
    (("status@broadcast", PN), False), (("bad@lid", "bad@lid"), False),
])
def test_reader_identity_match_is_private_and_normalized(loop, pair, expected):
    from core.contact_presence_refresh import same_read_contact
    assert same_read_contact(loop.owner, *pair) is expected


@pytest.mark.parametrize("allow", ["modern", "legacy_timestamp"])
def test_authoritative_permission_restoration_releases_denied_reader(loop, allow):
    owner = loop.owner
    presence.merge(owner, PN, {"restricted": True, "isOnline": False})
    loop.now = 300
    data = ({"restricted": False, "isOnline": False} if allow == "modern" else
            {"lastKnownPresence": "unavailable", "lastSeen": 1700000000})
    presence.merge(owner, PN, data)
    assert not presence.cached(owner, PN, fresh=False)["restricted"]
    if allow == "legacy_timestamp":
        loop.now = 391  # A valid observation satisfies demand until it expires.
    loop.ready()
    WindowLifecycleMixin._apply_window_activate(owner, True)
    loop.drain()
    assert len(loop.reads) == 1


def test_active_dialog_does_not_mark_own_presence_online(loop, monkeypatch):
    import core.online_presence as online
    monkeypatch.setattr(online, "threading", SimpleNamespace(Lock=threading.Lock, Thread=lambda target, **kw:
        SimpleNamespace(start=lambda: loop.workers.append(target))))
    from tests.test_online_presence_order import Timer
    owner = loop.owner
    owner.background_mode = False
    owner._presence_timer = Timer()
    sent = []
    owner._send_presence = lambda state, **kwargs: sent.append(state)
    dialog = Dialog(owner)
    dialog.active = False
    dialog.populate()
    loop.drain()
    loop.flags["active"] = False
    dialog.active = True
    WindowLifecycleMixin._sync_online_presence(owner)
    loop.drain()
    assert sent == ["unavailable"]


@pytest.mark.parametrize("populate_first", [False, True])
def test_old_dialog_cannot_acquire_a_reopened_visit_when_populated_again(loop, populate_first):
    dialog = Dialog(loop.owner)
    panel = loop.owner.conversations_panel
    dialog._personal_presence_visit = id(panel), panel._contact_presence_visit
    if populate_first:
        dialog.populate()
    panel._contact_presence_visit += 1
    dialog.populate()
    loop.flags["active"] = False
    loop.begin()
    assert not loop.reads
