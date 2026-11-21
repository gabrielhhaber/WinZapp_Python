"""Chat selection in the archived and locked lists.

Both lists select chats through the very code the conversations list runs
(ChatListSelectionMixin): same keys, same announcements, same sound, same
settings. What is specific to them — how rows are repainted, which mass actions
exist — is tested here. ArchivedConversationsPanel and LockedConversationsPanel
are wx.Panels and cannot be built without a wx.App, so the methods are bound
onto plain stubs, as in tests/test_selection_mode.py.
"""

from unittest.mock import Mock

import wx

from ui.chat_lock import LockedConversationsPanel
from ui.conversation_panel.archived_panel import ArchivedConversationsPanel
from ui.conversation_panel.chat_list_selection import ChatListSelectionMixin
from ui.conversations import ConversationsPanel

A = "a@s.whatsapp.net"
B = "b@s.whatsapp.net"
C = "c@s.whatsapp.net"


class _I18n:
    def t(self, key):
        return f"[{key}]"


class _MainWindow:
    def __init__(self, ui=None):
        self.settings = {"user_interface": dict(ui or {})}
        self.i18n = _I18n()
        self.outputs = []
        self.unarchived = []
        self.unlocked = []
        self.phone_locked = set()
        self.read_batches = []
        self.unread = []
        self._refresh_archived_chats_in_ui = Mock()
        self.add_chats_to_ui = Mock()

    def output(self, text, interrupt=False):
        self.outputs.append(text)

    def unarchive_chat(self, jid):
        self.unarchived.append(jid)

    def unlock_chat(self, jid):
        self.unlocked.append(jid)

    def is_chat_locked(self, jid):
        """Still locked unless unlock_chat() lifted it; a chat the phone locked
        stays locked (see phone_locked), which is the case unlock_chat() cannot
        undo."""
        return jid in self.phone_locked or jid not in self.unlocked

    def mark_conversations_as_read(self, jids, force=False):
        self.read_batches.append(list(jids))

    def mark_conversation_as_unread(self, jid):
        self.unread.append(jid)

    def _last_msg_preview(self, chat):
        return ""


class _List:
    def __init__(self, focused=0, count=0):
        self._focused = focused
        self.texts = {}
        self._count = count

    def GetFocusedItem(self):
        return self._focused

    def Focus(self, idx):
        self._focused = idx

    def Select(self, idx, on=True):
        pass

    def EnsureVisible(self, idx):
        pass

    def GetItemCount(self):
        return self._count

    def GetItemText(self, idx, col=0):
        return self.texts.get(idx, "")

    def SetItem(self, idx, col, text):
        self.texts[idx] = text


def _key(code, ctrl=False, shift=False, alt=False):
    event = Mock()
    event.GetKeyCode.return_value = code
    event.ControlDown.return_value = ctrl
    event.ShiftDown.return_value = shift
    event.AltDown.return_value = alt
    return event


class _ArchivedStub:
    _handle_chat_selection_key = ArchivedConversationsPanel._handle_chat_selection_key
    _clear_chat_list_filters = lambda self: False  # Esc-clears-filters has its own tests
    _toggle_chat_selection = ArchivedConversationsPanel._toggle_chat_selection
    _select_chat_at = ArchivedConversationsPanel._select_chat_at
    _all_chat_jids = ArchivedConversationsPanel._all_chat_jids
    _chat_selection_visible = ArchivedConversationsPanel._chat_selection_visible
    _announce_chat_selected = ArchivedConversationsPanel._announce_chat_selected
    _selection_mode_enabled = ArchivedConversationsPanel._selection_mode_enabled
    _escape_clears_selection_enabled = ArchivedConversationsPanel._escape_clears_selection_enabled
    _selection_mode_announcement = ArchivedConversationsPanel._selection_mode_announcement
    _bulk_shortcuts_enabled = ArchivedConversationsPanel._bulk_shortcuts_enabled
    _run_bulk_chat_action = ArchivedConversationsPanel._run_bulk_chat_action
    _repaint_chat_selection = ArchivedConversationsPanel._repaint_chat_selection
    _on_arch_list_key_down = ArchivedConversationsPanel._on_arch_list_key_down
    _on_arch_row_focused = ArchivedConversationsPanel._on_arch_row_focused
    _on_chat_row_focused_sound = ArchivedConversationsPanel._on_chat_row_focused_sound
    _on_mass_unarchive_chats = ArchivedConversationsPanel._on_mass_unarchive_chats
    _on_accel_bulk_unarchive_chats = ArchivedConversationsPanel._on_accel_bulk_unarchive_chats
    _on_mass_mark_read_chats = ArchivedConversationsPanel._on_mass_mark_read_chats
    _on_mass_mark_unread_chats = ArchivedConversationsPanel._on_mass_mark_unread_chats
    _on_accel_bulk_read_chats = ArchivedConversationsPanel._on_accel_bulk_read_chats
    _on_accel_toggle_read_selection = ArchivedConversationsPanel._on_accel_toggle_read_selection
    _prune_stale_chat_selection = ArchivedConversationsPanel._prune_stale_chat_selection
    _on_accel_unarchive = ArchivedConversationsPanel._on_accel_unarchive
    _on_accel_delete = ArchivedConversationsPanel._on_accel_delete

    def __init__(self, ui=None, jids=(A, B, C), focused=0):
        self.main_window = _MainWindow(ui)
        self.chats_list = [{"remoteJid": j} for j in jids]
        self.conversations_list = _List(focused, len(jids))
        self.selected_chats = set()
        self.selection_sound = Mock()
        self.opened = []
        self._deleted = []
        self._on_mass_delete_chats = lambda e: self._deleted.append("bulk")

    def on_conversation_selected(self, event):
        self.opened.append(event.GetIndex())

    def _selected_chat_from_list(self):
        return self.chats_list[self.conversations_list.GetFocusedItem()]

    def _on_unarchive(self, jid):
        self.main_window.unarchived.append(("single", jid))

    def _on_delete(self, jid):
        self._deleted.append(("single", jid))


class TestSameCodeAsTheConversationsList:
    """The point of the shared mixin: one implementation, three lists."""

    NAMES = [
        "_handle_chat_selection_key", "_toggle_chat_selection", "_select_chat_at",
        "_all_chat_jids", "_chat_selection_visible", "_announce_chat_selected",
        "_selection_mode_enabled", "_selection_mode_announcement",
        "_bulk_shortcuts_enabled", "_run_bulk_chat_action",
        "_on_mass_mark_read_chats", "_on_mass_mark_unread_chats",
        "_on_mass_clear_chats", "_on_mass_delete_chats", "_append_chat_mass_menu",
    ]

    def test_archived_list_runs_the_same_functions(self):
        for name in self.NAMES:
            assert getattr(ArchivedConversationsPanel, name) is getattr(ConversationsPanel, name), name

    def test_locked_list_runs_the_same_functions(self):
        for name in self.NAMES:
            assert getattr(LockedConversationsPanel, name) is getattr(ConversationsPanel, name), name

    def test_both_inherit_the_shared_mixin(self):
        assert issubclass(ArchivedConversationsPanel, ChatListSelectionMixin)
        assert issubclass(LockedConversationsPanel, ChatListSelectionMixin)
        assert issubclass(ConversationsPanel, ChatListSelectionMixin)


class TestArchivedKeys:
    def test_ctrl_space_selects_plays_the_sound_and_announces_the_mode(self):
        stub = _ArchivedStub()
        stub._on_arch_list_key_down(_key(wx.WXK_SPACE, ctrl=True))
        assert stub.selected_chats == {A}
        stub.selection_sound.play.assert_called_once()
        stub.main_window._refresh_archived_chats_in_ui.assert_called_once()
        assert stub.main_window.outputs == ["[selected]. [selection_mode_on]"]

    def test_plain_space_opens_the_chat_while_nothing_is_selected(self):
        stub = _ArchivedStub(focused=1)
        stub.conversations_list.Select = Mock()
        stub._on_arch_list_key_down(_key(wx.WXK_SPACE))
        assert stub.opened == [1]
        assert stub.selected_chats == set()

    def test_plain_space_keeps_selecting_once_a_selection_exists(self):
        stub = _ArchivedStub(focused=1)
        stub.selected_chats = {A}
        stub._on_arch_list_key_down(_key(wx.WXK_SPACE))
        assert stub.selected_chats == {A, B}
        assert stub.opened == []

    def test_setting_off_makes_space_open_the_chat_again(self):
        stub = _ArchivedStub(ui={"space_selects_in_selection_mode": False}, focused=1)
        stub.selected_chats = {A}
        stub._on_arch_list_key_down(_key(wx.WXK_SPACE))
        assert stub.selected_chats == {A}
        assert stub.opened == [1]

    def test_shift_down_extends_the_selection(self):
        stub = _ArchivedStub(focused=0)
        stub._on_arch_list_key_down(_key(wx.WXK_DOWN, shift=True))
        assert stub.selected_chats == {B}
        assert stub.conversations_list.GetFocusedItem() == 1

    def test_shift_end_selects_everything_below(self):
        stub = _ArchivedStub(focused=1)
        stub._on_arch_list_key_down(_key(wx.WXK_END, shift=True))
        assert stub.selected_chats == {B, C}

    def test_ctrl_shift_space_selects_all_then_clears(self):
        stub = _ArchivedStub()
        stub._on_arch_list_key_down(_key(wx.WXK_SPACE, ctrl=True, shift=True))
        assert stub.selected_chats == {A, B, C}
        stub._on_arch_list_key_down(_key(wx.WXK_SPACE, ctrl=True, shift=True))
        assert stub.selected_chats == set()

    def test_focusing_a_selected_row_plays_the_sound(self):
        stub = _ArchivedStub()
        stub.selected_chats = {B}
        for index in (0, 1):
            event = Mock()
            event.GetIndex.return_value = index
            stub._on_arch_row_focused(event)
        stub.selection_sound.play.assert_called_once()


class TestArchivedMassActions:
    def test_unarchive_selected_acts_on_every_chat_and_clears(self):
        stub = _ArchivedStub()
        stub.selected_chats = {A, C}
        stub._on_mass_unarchive_chats(None)
        assert sorted(stub.main_window.unarchived) == [A, C]
        assert stub.selected_chats == set()
        assert stub.main_window.outputs == ["[success_unarchive]"]

    def test_the_dedicated_shortcut_is_inert_without_a_selection(self):
        stub = _ArchivedStub()
        stub._on_accel_bulk_unarchive_chats(None)
        assert stub.main_window.unarchived == []
        assert stub.main_window.outputs == ["[bulk_no_chat_selection]"]

    def test_mark_read_and_unread_selected(self):
        stub = _ArchivedStub()
        stub.selected_chats = {A, B}
        stub._on_mass_mark_read_chats(None)
        assert sorted(stub.main_window.read_batches[0]) == [A, B]
        stub.selected_chats = {C}
        stub._on_mass_mark_unread_chats(None)
        assert stub.main_window.unread == [C]

    def test_unarchive_shortcut_is_bulk_only_with_the_setting_and_a_selection(self):
        stub = _ArchivedStub(focused=1)
        stub.selected_chats = {A, B}
        stub._on_accel_unarchive(None)
        assert sorted(stub.main_window.unarchived) == [A, B]

        single = _ArchivedStub(focused=1)
        single._on_accel_unarchive(None)
        assert single.main_window.unarchived == [("single", B)]

        off = _ArchivedStub(ui={"bulk_action_shortcuts": False}, focused=1)
        off.selected_chats = {A, B}
        off._on_accel_unarchive(None)
        assert off.main_window.unarchived == [("single", B)]

    def test_delete_shortcut_goes_bulk_with_a_selection(self):
        stub = _ArchivedStub()
        stub.selected_chats = {A}
        stub._on_accel_delete(None)
        assert stub._deleted == ["bulk"]

    def test_toggle_read_shortcut_marks_the_selection_read_or_unread(self):
        stub = _ArchivedStub()
        stub.chats_list[0]["unreadCount"] = 3
        stub.selected_chats = {A}
        stub._on_accel_toggle_read_selection(None, lambda e: None)
        assert stub.main_window.read_batches == [[A]]


class TestStaleSelectionIsForgotten:
    def test_a_chat_that_left_the_list_is_dropped_from_the_selection(self):
        stub = _ArchivedStub()
        stub.selected_chats = {A, "gone@s.whatsapp.net"}
        stub._prune_stale_chat_selection(stub.chats_list)
        assert stub.selected_chats == {A}

    def test_an_emptied_list_ends_selection_mode(self):
        stub = _ArchivedStub()
        stub.selected_chats = {A}
        stub._prune_stale_chat_selection([])
        assert stub.selected_chats == set()


class _LockedStub:
    _row_text = LockedConversationsPanel._row_text
    _repaint_chat_selection = LockedConversationsPanel._repaint_chat_selection
    _on_mass_unlock_chats = LockedConversationsPanel._on_mass_unlock_chats
    _on_accel_bulk_unlock_chats = LockedConversationsPanel._on_accel_bulk_unlock_chats
    _run_bulk_chat_action = LockedConversationsPanel._run_bulk_chat_action

    def __init__(self, ui=None, selected=()):
        self.main_window = _MainWindow(ui)
        self.chats_list = [{"remoteJid": A}, {"remoteJid": B}]
        self.chat_names = ["Ana", "Beto"]
        self.selected_chats = set(selected)
        self.conversations_list = _List(0, 2)


class TestLockedRows:
    def test_a_selected_row_carries_the_marker_at_the_end_by_default(self):
        stub = _LockedStub(selected={A})
        assert stub._row_text(stub.chats_list[0], "Ana") == "Ana [selected_suffix]"
        assert stub._row_text(stub.chats_list[1], "Beto") == "Beto"

    def test_the_marker_follows_the_configured_position(self):
        stub = _LockedStub(ui={"selected_announcement_position": "start"}, selected={A})
        assert stub._row_text(stub.chats_list[0], "Ana").startswith("[selected_suffix]")

    def test_repaint_rewrites_only_what_changed_in_place(self):
        stub = _LockedStub(selected={B})
        stub.conversations_list.texts = {0: "Ana", 1: "Beto"}
        stub._repaint_chat_selection()
        assert stub.conversations_list.texts == {0: "Ana", 1: "Beto [selected_suffix]"}

    def test_repaint_leaves_a_mismatched_list_alone(self):
        stub = _LockedStub(selected={A})
        stub.conversations_list = _List(0, 5)
        stub._repaint_chat_selection()
        assert stub.conversations_list.texts == {}


class TestLockedMassActions:
    def test_unlock_selected_unlocks_each_chat_and_says_so_once(self):
        stub = _LockedStub(selected={A, B})
        stub._on_mass_unlock_chats(None)
        assert sorted(stub.main_window.unlocked) == [A, B]
        assert stub.selected_chats == set()
        assert stub.main_window.outputs == ["[chat_lock_chats_unlocked]"]

    def test_a_chat_only_the_phone_can_unlock_is_not_announced_as_unlocked(self):
        """unlock_chat() already said why; claiming "chats unlocked" on top of it
        would be false while one of them is still hidden."""
        stub = _LockedStub(selected={A, B})
        stub.main_window.phone_locked = {B}
        stub._on_mass_unlock_chats(None)
        assert sorted(stub.main_window.unlocked) == [A, B]
        assert stub.main_window.outputs == []

    def test_the_dedicated_shortcut_is_inert_without_a_selection(self):
        stub = _LockedStub()
        stub._on_accel_bulk_unlock_chats(None)
        assert stub.main_window.unlocked == []
        assert stub.main_window.outputs == ["[bulk_no_chat_selection]"]


class _SearchField:
    def GetValue(self):
        return ""


class _ArchivedRefreshWindow:
    """Just what MainWindow._refresh_archived_chats_in_ui() reads."""

    _refresh_archived_chats_in_ui = None  # bound by _bind_refresh()

    def __init__(self, selected, ui=None):
        self.i18n = _I18n()
        self.settings = {"user_interface": dict(ui or {})}
        chats = [{"remoteJid": A}, {"remoteJid": B}]
        self.archived_conversations_panel = type("P", (), {})()
        panel = self.archived_conversations_panel
        panel._all_chats_list = chats
        panel._all_chat_names = ["Ana", "Beto"]
        panel.chats_list = chats
        panel.chat_names = ["Ana", "Beto"]
        panel._displayed_jids = [A, B]
        panel._conv_filter = "all"
        panel.search_field = _SearchField()
        panel.selected_chats = set(selected)
        panel.conversations_list = _List(0, 2)
        panel.conversations_list.texts = {0: "Ana", 1: "Beto"}

    def _search_normalization_mode(self):
        return "off"

    def _last_msg_preview(self, chat):
        return ""


def _bind_refresh():
    from main import MainWindow
    _ArchivedRefreshWindow._refresh_archived_chats_in_ui = MainWindow._refresh_archived_chats_in_ui
    _ArchivedRefreshWindow._filter_archived_chats = staticmethod(MainWindow._filter_archived_chats)


class TestArchivedRowsCarryTheMarker:
    def test_selected_row_is_suffixed_in_place_like_the_conversations_list(self):
        _bind_refresh()
        window = _ArchivedRefreshWindow(selected={B})
        window._refresh_archived_chats_in_ui()
        assert window.archived_conversations_panel.conversations_list.texts == {
            0: "Ana", 1: "Beto [selected_suffix]"}

    def test_the_configured_position_is_respected(self):
        _bind_refresh()
        window = _ArchivedRefreshWindow(
            selected={A}, ui={"selected_announcement_position": "start"})
        window._refresh_archived_chats_in_ui()
        assert window.archived_conversations_panel.conversations_list.texts[0] == \
            "[selected_suffix] Ana"

    def test_unselecting_removes_the_marker(self):
        _bind_refresh()
        window = _ArchivedRefreshWindow(selected=set())
        window.archived_conversations_panel.conversations_list.texts[1] = "Beto [selected_suffix]"
        window._refresh_archived_chats_in_ui()
        assert window.archived_conversations_panel.conversations_list.texts[1] == "Beto"


class TestEscapeClearsTheChatSelection:
    """"Esc desmarca" (default on) clears a chat list's selection the way it
    clears a conversation's: reported as Esc ignoring the setting there."""

    def test_esc_clears_and_announces_when_the_setting_is_on(self):
        stub = _ArchivedStub()
        stub.selected_chats = {A, B}
        assert stub._handle_chat_selection_key(_key(wx.WXK_ESCAPE)) is True
        assert stub.selected_chats == set()
        stub.main_window._refresh_archived_chats_in_ui.assert_called_once()
        assert stub.main_window.outputs == ["[all_unselected]. [selection_mode_off]"]

    def test_the_setting_off_leaves_the_selection_and_the_key_alone(self):
        stub = _ArchivedStub(ui={"escape_clears_selection": False})
        stub.selected_chats = {A}
        assert stub._handle_chat_selection_key(_key(wx.WXK_ESCAPE)) is False
        assert stub.selected_chats == {A} and stub.main_window.outputs == []

    def test_nothing_selected_keeps_what_esc_did_before(self):
        stub = _ArchivedStub()
        assert stub._handle_chat_selection_key(_key(wx.WXK_ESCAPE)) is False
        assert stub.main_window.outputs == []

    def test_a_modified_esc_is_not_taken(self):
        stub = _ArchivedStub()
        stub.selected_chats = {A}
        for event in (_key(wx.WXK_ESCAPE, ctrl=True), _key(wx.WXK_ESCAPE, shift=True),
                      _key(wx.WXK_ESCAPE, alt=True)):
            assert stub._handle_chat_selection_key(event) is False
        assert stub.selected_chats == {A}
