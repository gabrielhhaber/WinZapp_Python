"""Switching to a chat panel, through the real methods of every entry point.

A plain panel switch (Alt+1, Alt+4, the navigation list, the locked-chats
panel) must never make an open conversation visible and never load anything:
showing it again on every Alt+1 <-> Alt+4 was a perceptible delay (#339 showed
it again on return, #346 only made that cheaper). The conversation stays open
but hidden; only an explicit ask (Alt+M, Alt+2, Alt+3)
or opening a chat brings it back, in the panel it belongs to.

The real MainWindow / NavigationPanel / ConversationsPanel methods run against
widgets that only record Show/Hide/SetFocus, so the call order is the shipped
one. Rules pinned, for origin {main, archived, locked, none} x target panel:
  * after every plain switch the detail pane is hidden, the conversation is
    still open, and focus is on that panel's chat list (never a message list);
  * a switch costs Show/Hide + focus: no populate, no request, no thread, no
    CallAfter, however many times it repeats;
  * an explicit reveal shows the conversation in the panel it belongs to,
    whichever panel was on screen, and a hidden conversation is not "in view".

No window is created.
"""

import itertools
from unittest.mock import MagicMock

import pytest

from core.conversation_view import (
    ARCHIVED, LOCKED, MAIN, conversation_in_view, panel_layout,
)
from main import MainWindow
from ui.conversation_panel import conversation_navigation as nav_module
from ui.conversation_panel.conversation_navigation import ConversationNavigationMixin
from ui.conversation_panel.panel_visibility import ConversationPanelVisibilityMixin
from ui.navigation import NavigationPanel

A = "a@s.whatsapp.net"
B = "b@s.whatsapp.net"
G = "g@g.us"


class _Widget:
    def __init__(self, name, log, shown=False):
        self.name, self.log, self.shown = name, log, shown

    def Show(self, show=True):
        self.shown = bool(show)
        self.log.append((self.name, "Show", bool(show)))

    def Hide(self):
        self.shown = False
        self.log.append((self.name, "Hide"))

    def IsShown(self):
        return self.shown

    def Layout(self):
        pass

    def SetFocus(self):
        self.log.append((self.name, "SetFocus"))

    def Focus(self, idx):
        pass

    def Select(self, idx, on=True):
        pass

    def EnsureVisible(self, idx):
        pass

    def GetItemCount(self):
        return 3

    def GetFocusedItem(self):
        return 0

    def GetItemText(self, idx):
        return "row"


class _ListPanel(_Widget):
    """Archived / locked list panel: restore_selection() focuses its list."""

    def restore_selection(self):
        self.log.append((self.name, "SetFocus"))


class _Note:
    note = "last seen"

    def GetNote(self):
        return self.note

    def SetNote(self, text):
        self.note = text

    def SetLabel(self, text):
        pass


class _MW:
    """MainWindow stand-in: the real entry-point methods, recording widgets."""

    on_alt_1 = MainWindow.on_alt_1
    on_alt_4 = MainWindow.on_alt_4
    show_locked_chats_panel = MainWindow.show_locked_chats_panel
    _ensure_conversations_panel_visible = MainWindow._ensure_conversations_panel_visible
    _on_global_alt2 = MainWindow._on_global_alt2
    _on_global_alt3 = MainWindow._on_global_alt3
    _on_global_focus_messages = MainWindow._on_global_focus_messages

    def __init__(self, log):
        self.log = log
        self.requests = []          # every network-facing call, by name
        self.chats = {A: {"remoteJid": A}, B: {"remoteJid": B},
                      G: {"remoteJid": G}}
        self.locked = set()
        self.settings = {}
        self._chat_lock_vault = None
        self._chat_lock_unlocked = True
        self._locked_chat_rows = ([], [])
        self.content_panel = _Widget("content_panel", log, True)
        self.archived_conversations_panel = _ListPanel("archived_list_panel", log)
        self.locked_conversations_panel = _ListPanel("locked_list_panel", log)
        self.locked_conversations_panel.set_all_chats = lambda c, n: None
        self.status_panel = _Widget("status_panel", log)
        self.calls_panel = _Widget("calls_panel", log)
        self.db = MagicMock()
        self.db.get_messages.return_value = []
        self.db.get_message_count.return_value = 0
        self.i18n = MagicMock()
        self.conversations_panel = _Panel(self, log)

    def lock_chat_vault(self, **kwargs):
        pass

    def touch_chat_lock_timeout(self):
        pass

    def is_chat_locked(self, jid):
        return jid in self.locked

    def chat_display_name(self, chat):
        return "name"

    def _is_group_send_restricted(self, chat):
        return False

    def _note_conversation_opened(self, jid):
        self.requests.append("note_opened")

    def subscribe_presence(self, jid):
        self.requests.append("subscribe_presence")

    def get_group_info_recent(self, jid):
        self.requests.append("group_info")
        return {}

    def mark_conversation_as_read(self, jid):
        self.requests.append("mark_read")

    def __getattr__(self, name):
        value = MagicMock()
        setattr(self, name, value)
        return value


class _Panel(ConversationPanelVisibilityMixin):
    """ConversationsPanel: real visibility + navigation methods, widgets that
    record. populate_messages and the reaction backfill are counted."""

    _restore_conversation_selection = ConversationNavigationMixin._restore_conversation_selection
    navigate_to_conversation = ConversationNavigationMixin.navigate_to_conversation
    _open_focus_target = ConversationNavigationMixin._open_focus_target
    _conversation_note_text = ConversationNavigationMixin._conversation_note_text
    _message_label_text = ConversationNavigationMixin._message_label_text
    _apply_composer_permissions = lambda self, jid, conv: None
    _fetch_and_update_profile = lambda self, conv: None
    _fetch_group_participants = lambda self, jid: None

    def __init__(self, mw, log):
        self.main_window = mw
        self.log = log
        self.conversation = None
        self._conversation_origin = None
        self._group_participants_cache = []
        self._last_list_focus_jid = ""
        self._last_open_jid = ""
        self.chats_list = [{"remoteJid": A}]
        self._sorted_messages = []
        self._outgoing_virtual_messages = {}
        self._msg_temp_bookmarks = set()
        self.selected_messages = set()
        self._current_audio_id = None
        self._audio_stream = None
        self._pending_mentions = []
        self._pending_mention_display_names = {}
        self.search_field = MagicMock()
        self.search_field.GetValue.return_value = ""
        self.counts = {"populate": 0, "backfill": 0}
        self.panel_shown = True
        self.conversation_panel = _Widget("detail", log)
        self.conversations_label = _Widget("own_label", log, True)
        self.conversations_list = _Widget("own_list", log, True)
        self.messages_list = _Widget("messages_list", log, True)
        self.message_field = _Widget("message_field", log, True)
        self.message_field.IsEnabled = lambda: True
        self.message_field.IsEditable = lambda: True
        self._conv_data_btn = _Note()

    # ConversationsPanel is a wx.Panel; its own Show/Layout record too.
    def Show(self, show=True):
        self.panel_shown = bool(show)
        self.log.append(("panel", "Show", bool(show)))

    def Hide(self):
        self.Show(False)

    def IsShown(self):
        return self.panel_shown

    def Layout(self):
        pass

    def populate_messages(self, preserve_focus=False):
        self.counts["populate"] += 1

    def _backfill_reactions_for_open_conversation(self):
        self.counts["backfill"] += 1

    # What Alt+2 / Alt+3 / Alt+M do once the pane is shown: put focus in the
    # messages.
    def _no_conversation_open_announced(self):
        return self.conversation is None

    def _on_accel_jump_last(self, event):
        if not self._no_conversation_open_announced():
            self.messages_list.SetFocus()

    _on_accel_jump_unread = _on_accel_jump_last
    _on_accel_focus_list = _on_accel_jump_last

    def _focused_msg_id(self):
        return "msg-4"

    def _is_separator(self, msg):
        return False

    def _sync_pending_document_gauge(self):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = MagicMock()
        setattr(self, name, value)
        return value


@pytest.fixture
def world(monkeypatch):
    log = []
    queued = []
    started = []

    class _Thread:
        def __init__(self, target=None, args=(), daemon=None):
            self.target = target

        def start(self):
            started.append(getattr(self.target, "__name__", str(self.target)))

    monkeypatch.setattr(nav_module.threading, "Thread", _Thread)
    monkeypatch.setattr(nav_module.wx, "CallAfter", lambda fn, *a: queued.append((fn, a)))
    import ui.conversation_panel.panel_visibility as pv
    if hasattr(pv, "wx"):  # the switch itself must not defer anything
        monkeypatch.setattr(pv.wx, "CallAfter", lambda fn, *a: queued.append((fn, a)))
    mw = _MW(log)
    mw.started, mw.queued = started, queued
    return mw


def _open(mw, jid, origin):
    """Open a chat the way its panel's list does, then settle."""
    panel = mw.conversations_panel
    if origin in (ARCHIVED, LOCKED):
        panel.conversations_list.Hide()
        panel.conversations_label.Hide()
    panel.navigate_to_conversation(mw.chats[jid], origin=origin)
    mw.queued.clear()


def _enter(mw, target):
    """Every real way of making `target` the visible chat panel."""
    nav = type("_Nav", (), {"main_window": mw, "_nav_keys": [
        "conversations", "archived", "locked", "status", "calls", "settings"]})()
    nav.on_nav_item_selected = lambda e: NavigationPanel.on_nav_item_selected(nav, e)
    idx = {MAIN: 0, ARCHIVED: 1, LOCKED: 2}[target]
    event = type("_E", (), {"GetIndex": lambda self: idx})()
    return {
        MAIN: [("alt1", lambda: mw.on_alt_1(None)), ("nav", lambda: nav.on_nav_item_selected(event))],
        ARCHIVED: [("alt4", lambda: mw.on_alt_4(None)), ("nav", lambda: nav.on_nav_item_selected(event))],
        LOCKED: [("locked", lambda: mw.show_locked_chats_panel())],
    }[target]


def _focus_calls(mw):
    return [e[0] for e in mw.log if e[1] == "SetFocus"]


def _work(mw):
    """Everything a switch must not cost: rebuilds, requests, threads, queued
    callbacks, database reads."""
    return (dict(mw.conversations_panel.counts), list(mw.requests),
            list(mw.started), len(mw.queued), mw.db.get_messages.call_count)


CHAT_LIST_FOCUS = {MAIN: "own_list", ARCHIVED: "archived_list_panel",
                   LOCKED: "locked_list_panel"}
ORIGINS = [MAIN, ARCHIVED, LOCKED, None]
TARGETS = [MAIN, ARCHIVED, LOCKED]
ENTRIES = [(MAIN, "alt1"), (MAIN, "nav"), (ARCHIVED, "alt4"),
           (ARCHIVED, "nav"), (LOCKED, "locked")]


class TestPanelLayoutRule:
    @pytest.mark.parametrize("origin,target", list(itertools.product(ORIGINS, TARGETS)))
    def test_staying_keeps_the_detail_only_in_the_panel_it_belongs_to(self, origin, target):
        layout = panel_layout(origin, target, True, keep=True)
        assert layout["detail"] == (origin == target)
        assert layout == panel_layout(origin, target, True, reveal=True) or origin != target
        assert not panel_layout(origin, target, False, keep=True)["detail"]

    @pytest.mark.parametrize("origin,target", list(itertools.product(ORIGINS, TARGETS)))
    def test_a_plain_switch_never_shows_the_detail(self, origin, target):
        layout = panel_layout(origin, target, True)
        assert layout["detail"] is False
        assert layout["panel"] == (target == MAIN)
        assert layout["own_list"] is True
        assert layout["list_panel"] == (target != MAIN)

    @pytest.mark.parametrize("origin,target", list(itertools.product(ORIGINS, TARGETS)))
    def test_a_reveal_shows_it_only_in_its_own_panel(self, origin, target):
        layout = panel_layout(origin, target, True, reveal=True)
        assert layout["detail"] == (origin == target)
        assert layout["panel"] == (target == MAIN or origin == target)
        assert layout["list_panel"] == (target != MAIN)
        # the main list is hidden only when the conversation sits under another list
        assert layout["own_list"] == (target == MAIN or origin != target)
        assert not panel_layout(origin, target, False, reveal=True)["detail"]


class TestEveryEntryPointByOriginAndTarget:
    @pytest.mark.parametrize("origin", ORIGINS)
    @pytest.mark.parametrize("target,label", ENTRIES)
    def test_a_real_switch_hides_the_conversation_and_staying_keeps_it(self, world, origin, target, label):
        panel = world.conversations_panel
        _open(world, A, origin or MAIN)
        panel._conversation_origin = origin   # None: opened some other way
        world.log.clear()
        staying = origin == target            # already in the panel being asked for

        dict(_enter(world, target))[label]()

        assert panel.conversation is not None  # hidden or kept, never closed
        list_panel = {ARCHIVED: world.archived_conversations_panel,
                      LOCKED: world.locked_conversations_panel}.get(target)
        if staying:
            assert panel.conversation_panel.shown, (label, origin, target)
            assert conversation_in_view(panel)
            assert panel.panel_shown
        else:
            assert not panel.conversation_panel.shown, (label, origin, target)
            assert not conversation_in_view(panel)
            if target == MAIN:
                assert panel.panel_shown and panel.conversations_list.shown
                assert not world.archived_conversations_panel.shown
            else:
                assert not panel.panel_shown, "a conversation pane stayed on screen"
        if list_panel is not None:
            assert list_panel.shown
        focused = _focus_calls(world)
        assert focused and focused[-1] == CHAT_LIST_FOCUS[target]
        assert "messages_list" not in focused and "message_field" not in focused

    @pytest.mark.parametrize("target,label", ENTRIES)
    def test_without_a_conversation(self, world, target, label):
        world.status_panel.shown = True
        world.log.clear()

        dict(_enter(world, target))[label]()

        panel = world.conversations_panel
        assert not panel.conversation_panel.shown
        assert panel.panel_shown == (target == MAIN)
        assert not world.status_panel.shown
        assert _focus_calls(world)[-1] == CHAT_LIST_FOCUS[target]
        assert not world.queued

    def test_returning_to_the_origin_panel_does_not_show_it_again(self, world):
        # The old rule ("shown again on return") deliberately reversed.
        for origin, away, back in ((MAIN, "on_alt_4", "on_alt_1"),
                                   (ARCHIVED, "on_alt_1", "on_alt_4")):
            fresh = _MW([])
            fresh.started, fresh.queued = world.started, world.queued
            _open(fresh, A, origin)
            getattr(fresh, away)(None)
            getattr(fresh, back)(None)
            assert not fresh.conversations_panel.conversation_panel.shown
            assert fresh.conversations_panel.conversation is not None


class TestASwitchCostsNothingButShowHide:
    @pytest.mark.parametrize("origin", [MAIN, ARCHIVED, LOCKED])
    def test_repeated_cycles_cost_no_populate_no_request_no_thread(self, world, origin):
        _open(world, G, origin)
        before = _work(world)
        for _ in range(4):
            world.on_alt_4(None)
            world.on_alt_1(None)
            world.show_locked_chats_panel()
            world.on_alt_1(None)
            dict(_enter(world, ARCHIVED))["nav"]()
            dict(_enter(world, MAIN))["nav"]()
        assert _work(world) == before
        assert not world.conversations_panel.conversation_panel.shown

    def test_the_message_list_is_untouched_by_a_cycle(self, world):
        _open(world, A, ARCHIVED)
        world.log.clear()
        world.on_alt_1(None)
        world.on_alt_4(None)
        assert not any(e[0] in ("messages_list", "message_field") for e in world.log)

    def test_the_hidden_conversation_is_not_in_view(self, world):
        _open(world, A, MAIN)
        assert conversation_in_view(world.conversations_panel)
        world.on_alt_4(None)
        assert not conversation_in_view(world.conversations_panel)
        world.on_alt_1(None)
        assert not conversation_in_view(world.conversations_panel)


def _go_to(mw, where):
    """Be on `where` (a chat panel or 'status'), the way the user got there."""
    if where == "status":
        mw.conversations_panel.Hide()
        mw.archived_conversations_panel.Hide()
        mw.locked_conversations_panel.Hide()
        mw.status_panel.Show()
    else:
        dict(_enter(mw, where))[{MAIN: "alt1", ARCHIVED: "alt4", LOCKED: "locked"}[where]]()
    mw.log.clear()


def _on_screen_for(mw, origin):
    panel = mw.conversations_panel
    assert panel.conversation_panel.shown and panel.panel_shown
    assert conversation_in_view(panel)
    assert not mw.status_panel.shown
    assert mw.archived_conversations_panel.shown == (origin == ARCHIVED)
    assert mw.locked_conversations_panel.shown == (origin == LOCKED)
    assert panel.conversations_list.shown == (origin == MAIN)


EXPLICIT = {
    "alt_m": lambda mw: mw._on_global_focus_messages(None),
    "alt_2": lambda mw: mw._on_global_alt2(None),
    "alt_3": lambda mw: mw._on_global_alt3(None),
    "list_alt_m": lambda mw: mw.conversations_panel._on_list_focus_messages(None),
    "list_alt_2": lambda mw: mw.conversations_panel._on_list_jump_last(None),
    "list_alt_3": lambda mw: mw.conversations_panel._on_list_jump_unread(None),
}


class TestExplicitRevealsShowTheConversationInItsOwnPanel:
    @pytest.mark.parametrize("command", sorted(EXPLICIT))
    @pytest.mark.parametrize("start", [MAIN, ARCHIVED, LOCKED, "status"])
    @pytest.mark.parametrize("origin", [MAIN, ARCHIVED, LOCKED])
    def test_reveal(self, world, origin, start, command):
        _open(world, A, origin)
        _go_to(world, start)
        before = _work(world)

        EXPLICIT[command](world)

        _on_screen_for(world, origin)
        assert _focus_calls(world)[-1] == "messages_list"
        # revealing costs Show/Hide only: nothing is reloaded or requested
        assert _work(world) == before

    @pytest.mark.parametrize("command", sorted(EXPLICIT))
    def test_with_no_conversation_nothing_appears(self, world, command):
        _go_to(world, ARCHIVED)
        EXPLICIT[command](world)
        assert not world.conversations_panel.conversation_panel.shown
        assert not world.conversations_panel.panel_shown
        assert world.archived_conversations_panel.shown

    def test_a_reveal_then_a_switch_hides_it_again(self, world):
        _open(world, A, MAIN)
        world.on_alt_4(None)
        world._on_global_focus_messages(None)
        _on_screen_for(world, MAIN)
        world.on_alt_4(None)
        assert not world.conversations_panel.conversation_panel.shown


class TestADisplacedConversation:
    """Archived X open, the user opens main Y from the main list: Y is the
    conversation now. X is not kept aside (nothing asks for it explicitly: an
    explicit command always means the open conversation, Y); it returns by
    being opened from its list again."""

    def _displace(self, world):
        _open(world, B, ARCHIVED)
        world.on_alt_1(None)
        _open(world, A, MAIN)

    def test_explicit_commands_reveal_the_open_one_in_its_panel(self, world):
        self._displace(world)
        world.on_alt_4(None)
        world._on_global_focus_messages(None)
        assert world.conversations_panel.conversation["remoteJid"] == A
        _on_screen_for(world, MAIN)

    def test_switches_never_reopen_the_displaced_one(self, world):
        self._displace(world)
        before = _work(world)
        for _ in range(3):
            world.on_alt_4(None)
            world.on_alt_1(None)
        assert _work(world) == before
        assert world.conversations_panel.conversation["remoteJid"] == A
        assert not hasattr(ConversationPanelVisibilityMixin, "_reopen_parked_conversation")

    def test_it_is_opened_again_from_its_own_list(self, world):
        self._displace(world)
        world.on_alt_4(None)
        _open(world, B, ARCHIVED)
        panel = world.conversations_panel
        assert panel.conversation["remoteJid"] == B
        assert panel._conversation_origin == ARCHIVED
        assert panel.conversation_panel.shown

    def test_opening_the_same_chat_from_another_panel_changes_its_owner_and_shows_it(self, world):
        _open(world, A, MAIN)
        world.on_alt_4(None)
        assert not world.conversations_panel.conversation_panel.shown
        _open(world, A, ARCHIVED)
        panel = world.conversations_panel
        assert panel._conversation_origin == ARCHIVED
        assert panel.conversation_panel.shown


class TestPressingTheShortcutOfThePanelYouAreIn:
    """Alt+1 inside a main conversation, Alt+4 inside an archived one: the focus
    goes to the chat list and the conversation stays; only a change of panel
    hides it."""

    @pytest.mark.parametrize("origin,press", [(MAIN, "on_alt_1"), (ARCHIVED, "on_alt_4")])
    def test_the_conversation_stays_and_the_chat_list_gets_focus(self, world, origin, press):
        panel = world.conversations_panel
        _open(world, A, origin)
        before = _work(world)
        world.log.clear()

        getattr(world, press)(None)

        assert panel.conversation_panel.shown and conversation_in_view(panel)
        assert _focus_calls(world)[-1] == CHAT_LIST_FOCUS[origin]
        assert "messages_list" not in _focus_calls(world)
        assert _work(world) == before   # Show/Hide and focus, nothing else

    @pytest.mark.parametrize("origin,press", [(MAIN, "on_alt_1"), (ARCHIVED, "on_alt_4")])
    def test_pressing_it_again_changes_nothing(self, world, origin, press):
        _open(world, A, origin)
        for _ in range(3):
            getattr(world, press)(None)
            assert world.conversations_panel.conversation_panel.shown

    @pytest.mark.parametrize("origin,press", [(MAIN, "on_alt_4"), (ARCHIVED, "on_alt_1"),
                                              (LOCKED, "on_alt_1"), (LOCKED, "on_alt_4")])
    def test_the_other_panels_shortcut_is_a_switch_and_hides_it(self, world, origin, press):
        _open(world, A, origin)

        getattr(world, press)(None)

        assert not world.conversations_panel.conversation_panel.shown
        assert world.conversations_panel.conversation is not None

    @pytest.mark.parametrize("origin,press", [(MAIN, "on_alt_1"), (ARCHIVED, "on_alt_4")])
    def test_coming_back_from_status_is_a_switch_even_to_the_same_panel(self, world, origin, press):
        _open(world, A, origin)
        _go_to(world, "status")

        getattr(world, press)(None)

        assert not world.conversations_panel.conversation_panel.shown

    @pytest.mark.parametrize("origin,target,label", [(MAIN, MAIN, "nav"), (ARCHIVED, ARCHIVED, "nav")])
    def test_choosing_the_panel_you_are_in_from_the_navigation_list_keeps_it(self, world, origin, target, label):
        _open(world, A, origin)

        dict(_enter(world, target))[label]()

        assert world.conversations_panel.conversation_panel.shown

    def test_a_switch_then_the_same_shortcut_again_still_hides_it(self, world):
        _open(world, A, MAIN)
        world.on_alt_4(None)
        world.on_alt_1(None)
        world.on_alt_1(None)
        assert not world.conversations_panel.conversation_panel.shown
