"""The pinned messages window (issue #848): the message list's own menu and
shortcuts on each pin, the conversation's selection, rows without the pin
marker. Every widget is a recording stub; no wx window is created.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import wx

import ui.dialogs.pinned_messages_dialog as dialog_module
from core import pinned_list
from core.shortcut_catalog import SHORTCUTS
from ui.conversation_panel.message_list_proxy import MessageListProxyMixin, command_id
from ui.dialogs.pinned_messages_dialog import PinnedMessagesDialog

JID = "123@g.us"


def message(mid, text=None):
    return {"key": {"id": mid, "remoteJid": JID}, "pinInChat": True,
            "messageType": "conversation",
            "message": {"conversation": text or f"Content {mid}"}}


# ── core/pinned_list.py ─────────────────────────────────────────────────────

class TestKeys:
    def test_every_key_the_window_reads_is_in_the_catalog(self):
        ids = {s.id for s in SHORTCUTS}
        wanted = (pinned_list.IN_PLACE + pinned_list.LEAVING
                  + pinned_list.SELECTION_KEYS + (pinned_list.AI_ACTION,))
        assert [sid for sid in wanted if sid not in ids] == []

    def test_conversation_level_keys_are_left_to_the_window(self):
        """Esc closes the window; recording, search or closing the chat are
        not message actions and never reach the panel from here."""
        every = pinned_list.IN_PLACE + pinned_list.LEAVING
        for sid in ("messages.ID_ESC", "messages.CTRL_W", "messages.ID_CTRL_R",
                    "messages.ID_CTRL_SHIFT_F", "messages.ID_CTRL_SHIFT_L"):
            assert sid not in every

    def test_the_actions_of_the_conversation_leave_the_window(self):
        assert pinned_list.key_action(lambda sid: sid == "messages.ID_ALT_R") == (
            "leave", "messages.ID_ALT_R")
        assert pinned_list.key_action(lambda sid: sid == "messages.ID_ALT_E") == (
            "leave", "messages.ID_ALT_E")

    def test_copy_unpin_and_delete_run_in_place(self):
        for sid in ("messages.ID_CTRL_C", "messages.ID_CTRL_SHIFT_P", "messages.ID_DELETE_MSG"):
            assert pinned_list.key_action(lambda s, sid=sid: s == sid) == ("run", sid)

    def test_selection_keys_come_first_and_unknown_keys_pass(self):
        assert pinned_list.key_action(lambda s: True)[0] == "select"
        assert pinned_list.key_action(lambda s: False) is None
        assert pinned_list.key_action(lambda s: s == pinned_list.AI_ACTION) == (
            "ai", pinned_list.AI_ACTION)

    def test_command_of(self):
        assert pinned_list.command_of("messages.ID_QUICK_REACTION[3]") == "ID_QUICK_REACTION[3]"


class TestRows:
    def test_each_pin_once_without_the_deleted_ones(self):
        rows = pinned_list.visible_pins([message("a"), message("b"), message("a"),
                                         {"key": {}}], {"b"})
        assert [m["key"]["id"] for m in rows] == ["a"]

    def test_toggle_all_selects_the_rest_then_clears(self):
        assert pinned_list.toggle_all(["a", "b"], {"a"}) == (["b"], [])
        assert pinned_list.toggle_all(["a", "b"], {"a", "b", "x"}) == ([], ["a", "b"])
        assert pinned_list.toggle_all([], set()) == ([], [])

    def test_selection_within_narrows_then_returns_the_rest(self):
        holder = SimpleNamespace(selected_messages={"a", "x", "y"})
        with pinned_list.selection_within(holder, ["a", "b", ""]) as outside:
            assert holder.selected_messages == {"a"}
            assert outside == {"x", "y"}
            holder.selected_messages.discard("a")
        assert holder.selected_messages == {"x", "y"}

    def test_selection_within_restores_even_when_the_action_raises(self):
        holder = SimpleNamespace(selected_messages={"x"})
        with pytest.raises(RuntimeError):
            with pinned_list.selection_within(holder, ["a"]):
                raise RuntimeError
        assert holder.selected_messages == {"x"}

    def test_kept_index_follows_the_message_or_its_place(self):
        rows = [message("a"), message("b")]
        assert pinned_list.kept_index(rows, "b", 0) == 1
        assert pinned_list.kept_index(rows, "gone", 5) == 1
        assert pinned_list.kept_index([], "a", 0) == -1


# ── ui/conversation_panel/message_list_proxy.py ─────────────────────────────

class _List:
    def __init__(self, selected=-1):
        self.selected = {selected} if selected >= 0 else set()
        self.focused = selected

    def GetFirstSelected(self):
        return min(self.selected) if self.selected else -1

    def Select(self, index, on=True):
        (self.selected.add if on else self.selected.discard)(index)

    def Focus(self, index):
        self.focused = index


class ProxyPanel(MessageListProxyMixin):
    ID_ALT_SHIFT_D = 501
    ID_QUICK_REACTION = [600 + d for d in range(10)]

    def __init__(self, rows, selected=-1):
        self._sorted_messages = list(rows)
        self.messages_list = _List(selected)
        self.seen = []
        self.handler = SimpleNamespace(ProcessEvent=self._process)

    def _process(self, event):
        index = self.messages_list.GetFirstSelected()
        self.seen.append((event.GetId(), index, self._suppress_selection_side_effects))
        return True

    def GetEventHandler(self):
        return self.handler

    def _find_index_by_msg_id(self, mid):
        return next((i for i, m in enumerate(self._sorted_messages)
                     if mid and m["key"].get("id") == mid), -1)

    def _resident_message_index(self, msg):
        return self._find_index_by_msg_id(msg["key"]["id"])

    def _message_context_menu(self, index, leave=None):
        return ("menu", index, leave)


class TestProxy:
    def test_command_ids_resolve_plain_and_numbered_attributes(self):
        panel = ProxyPanel([])
        assert command_id(panel, "ID_ALT_SHIFT_D") == 501
        assert command_id(panel, "ID_QUICK_REACTION[3]") == 603
        assert command_id(panel, "ID_MISSING") is None
        assert command_id(panel, "ID_QUICK_REACTION[12]") is None

    def test_a_command_runs_with_the_message_selected_and_the_old_selection_returns(self):
        panel = ProxyPanel([message("a"), message("b")], selected=0)
        assert panel.run_message_command(message("b"), "ID_ALT_SHIFT_D") is True
        assert panel.seen == [(501, 1, True)]
        assert panel.messages_list.selected == {0}
        assert panel.messages_list.focused == 0
        assert panel._suppress_selection_side_effects is False

    def test_the_selection_comes_back_by_identity_after_rows_move(self):
        """A delete removes rows above: the old selection is found by id."""
        panel = ProxyPanel([message("x"), message("a"), message("b")], selected=1)

        def delete_first(event):
            panel._sorted_messages.pop(0)
            panel.messages_list.selected = {1}   # what the list shows now
            return True

        panel.handler.ProcessEvent = delete_first
        panel.forward_message_menu_event(message("b"), 77)
        assert panel.messages_list.selected == {0}       # "a", now first

    def test_a_selected_row_without_an_id_comes_back(self):
        """The unread separator has no message id: it is found as the same row."""
        separator = {"key": {}, "messageType": "unread_separator"}
        panel = ProxyPanel([message("a"), separator, message("b")], selected=1)
        panel.run_message_command(message("b"), "ID_ALT_SHIFT_D")
        assert panel.messages_list.selected == {1}
        assert panel.messages_list.focused == 1

    def test_a_message_that_cannot_be_shown_runs_nothing(self):
        panel = ProxyPanel([message("a")])
        assert panel.run_message_command(message("zzz"), "ID_ALT_SHIFT_D") is False
        assert panel.run_message_command(message("a"), "ID_UNKNOWN") is False
        assert panel.seen == []
        assert panel.message_context_menu_for(message("zzz")) is None

    def test_the_menu_is_the_lists_own_for_that_row(self):
        panel = ProxyPanel([message("a"), message("b")])
        leave = object()
        assert panel.message_context_menu_for(message("b"), leave=leave) == ("menu", 1, leave)


# ── ui/dialogs/pinned_messages_dialog.py ────────────────────────────────────

class _ListBox:
    def __init__(self):
        self.items, self.selection, self.sets = [], -1, 0

    def Set(self, items):
        self.items, self.sets = list(items), self.sets + 1

    def GetSelection(self):
        return self.selection

    def SetSelection(self, index):
        self.selection = index

    def GetString(self, row):
        return self.items[row]

    def SetString(self, row, text):
        self.items[row] = text

    def Freeze(self):
        pass

    def Thaw(self):
        pass


class Panel:
    def __init__(self, pins, records=None):
        self._pinned_messages = list(pins)
        records = list(pins) if records is None else records
        self.conversation = {"messages": {"messages": {"records": records}}}
        self._sorted_messages = records
        self.selected_messages = set()
        self.run_message_command = Mock(return_value=True)
        self.forward_message_menu_event = Mock(return_value=True)
        self._toggle_message_selection = Mock()
        self._refresh_message_rows_by_ids = Mock()
        self.selection_sound = SimpleNamespace(play=Mock())
        self._selection_mode_announcement = lambda word, was, now: word
        self.show_message_in_folder = Mock()
        self._on_ai_action = Mock()

    def _find_index_by_msg_id(self, mid):
        return next((i for i, m in enumerate(self._sorted_messages)
                     if m["key"]["id"] == mid), -1)

    def _resident_message_index(self, msg):
        return self._find_index_by_msg_id(msg["key"]["id"])

    def _render_message_line(self, msg, pin_marker=True):
        marker = "📌 " if pin_marker else ""
        selected = " selected" if msg["key"]["id"] in self.selected_messages else ""
        return marker + msg["message"]["conversation"] + selected


class Window:
    """PinnedMessagesDialog's methods on a stub: no wx.Dialog is built."""
    for _name in ("_current", "_row_text", "_refresh", "_selected", "_record_ids", "_go",
                  "_leave", "_run", "_on_char_hook", "_selection_key", "_add_to_selection",
                  "_toggle_all", "_leave_menu", "_on_menu", "close_for_lock", "_end_for_lock",
                  "_on_context_menu"):
        locals()[_name] = getattr(PinnedMessagesDialog, _name)

    def __init__(self, panel):
        self._panel = panel
        self._mw = SimpleNamespace(output=Mock())
        self._i18n = SimpleNamespace(t=lambda key: key)
        self._rows, self._gone, self._leaving = [], set(), False
        self._in_action = self._locked = False
        self.IsModal = Mock(return_value=True)
        self._menu_message = self._menu_go_id = None
        self.target = self.then = None
        self._list = _ListBox()
        self._go_button = Mock()
        self.EndModal = Mock()
        self._refresh(0)


class Key:
    def __init__(self, shortcut_id=None, code=0, modifiers=False):
        self.shortcut_id, self.code, self.modifiers = shortcut_id, code, modifiers
        self.Skip = Mock()

    def GetKeyCode(self):
        return self.code

    def HasAnyModifiers(self):
        return self.modifiers


@pytest.fixture
def keys(monkeypatch):
    """The window's list has focus; a key "is" the shortcut it names."""
    def press(window, shortcut_id=None, code=0, modifiers=False):
        monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: window._list))
        monkeypatch.setattr(dialog_module, "matches",
                            lambda owner, event, sid: sid == event.shortcut_id)
        event = Key(shortcut_id, code, modifiers)
        window._on_char_hook(event)
        return event
    return press


class TestWindowRows:
    def test_rows_have_no_pin_marker_and_keep_line_breaks(self):
        window = Window(Panel([message("a", "first\nsecond")]))
        assert window._list.items == ["first\nsecond"]
        window._go_button.Enable.assert_called_with(True)

    def test_rows_use_the_conversations_own_copy(self):
        pin = message("a", "server copy")
        window = Window(Panel([pin], records=[message("a", "live copy")]))
        assert window._list.items == ["live copy"]

    def test_no_pins_shows_the_empty_text_and_disables_go(self):
        window = Window(Panel([]))
        assert window._list.items == ["pinned_messages_empty"]
        window._go_button.Enable.assert_called_with(False)
        assert window._selected() is None


class TestTheMenuOnTheWindowsSelection:
    def test_the_menu_sees_only_the_windows_part_of_the_selection(self):
        """With only messages outside the window selected, the menu offered
        "Selected messages" and its items ran on nothing, silently."""
        panel = Panel([message("a")], records=[message("a"), message("x")])
        panel.selected_messages.add("x")
        seen = []
        panel.message_context_menu_for = (
            lambda msg, leave=None: seen.append(set(panel.selected_messages)))
        Window(panel)._on_context_menu(None)
        assert seen == [set()]
        assert panel.selected_messages == {"x"}


class TestClosingForALock:
    """ui/conversation_panel/pinned_messages.py: which lock closes the window."""

    def _panel(self, hidden=False):
        from ui.conversation_panel.pinned_messages import PinnedMessagesMixin
        closed = []
        panel = SimpleNamespace(
            _pinned_window=(SimpleNamespace(close_for_lock=lambda: closed.append(True)), JID),
            main_window=SimpleNamespace(is_chat_hidden_by_vault=lambda jid: hidden))
        return lambda jids=None: PinnedMessagesMixin.close_locked_pinned_window(panel, jids), closed

    def test_the_vault_closes_a_chat_it_hides(self):
        close, closed = self._panel(hidden=True)
        close()
        assert closed == [True]

    def test_a_phone_lock_closes_its_own_chat_with_the_vault_open(self):
        close, closed = self._panel(hidden=False)
        close({"other@g.us"})
        assert closed == []
        close({JID})
        assert closed == [True]


class TestTheVaultLocks:
    """MainWindow.lock_chat_vault() closes the window of a chat it hides."""

    def test_the_rows_go_and_the_window_closes(self):
        window = Window(Panel([message("a")]))
        window.close_for_lock()
        assert window._list.items == [] and window._rows == []
        window._go_button.Enable.assert_called_with(False)
        window.EndModal.assert_called_once_with(wx.ID_CANCEL)

    def test_during_an_action_the_close_waits_for_it_to_return(self, keys):
        """The action's own window may be the running modal loop; wx can only
        end that one."""
        panel = Panel([message("a"), message("b")])
        window = Window(panel)
        during = []

        def lock_meanwhile(msg, command):
            window.close_for_lock()
            during.append(window.EndModal.call_count)
        panel.run_message_command.side_effect = lock_meanwhile
        keys(window, "messages.ID_CTRL_SHIFT_E")
        assert during == [0]
        window.EndModal.assert_called_once_with(wx.ID_CANCEL)
        assert window._list.items == []


class TestWindowKeys:
    def test_enter_closes_on_the_message(self, keys):
        window = Window(Panel([message("a"), message("b")]))
        window._list.selection = 1
        keys(window, code=wx.WXK_RETURN)
        assert window.target["key"]["id"] == "b"
        window.EndModal.assert_called_once_with(wx.ID_OK)

    def test_a_message_action_runs_with_the_window_open(self, keys):
        panel = Panel([message("a")])
        window = Window(panel)
        keys(window, "messages.ID_CTRL_C")
        panel.run_message_command.assert_called_once_with(message("a"), "ID_CTRL_C")
        window.EndModal.assert_not_called()

    def test_an_action_reaches_only_the_windows_part_of_the_selection(self, keys):
        """Messages selected in the conversation and not pinned used to be
        what Ctrl+Shift+P, Delete or Ctrl+C on a pin acted on."""
        panel = Panel([message("a"), message("b")],
                      records=[message("a"), message("b"), message("x")])
        panel.selected_messages.update({"b", "x"})
        window = Window(panel)
        seen = []
        panel.run_message_command.side_effect = (
            lambda msg, command: seen.append(set(panel.selected_messages)))
        keys(window, "messages.ID_CTRL_SHIFT_P")
        assert seen == [{"b"}]
        assert panel.selected_messages == {"b", "x"}
        panel._refresh_message_rows_by_ids.assert_called_with(["x"])

    def test_an_action_clearing_the_selection_keeps_the_outside_part(self, keys):
        panel = Panel([message("a")], records=[message("a"), message("x")])
        panel.selected_messages.update({"a", "x"})
        window = Window(panel)
        panel.run_message_command.side_effect = (
            lambda msg, command: panel.selected_messages.clear())
        keys(window, "messages.ID_DELETE_MSG")
        assert panel.selected_messages == {"x"}

    def test_unpinning_takes_the_row_out_without_rewriting_it_twice(self, keys):
        panel = Panel([message("a"), message("b")])
        window = Window(panel)
        panel.run_message_command.side_effect = lambda msg, command: panel._pinned_messages.pop(0)
        keys(window, "messages.ID_CTRL_SHIFT_P")
        assert window._list.items == ["Content b"]
        assert window._selected()["key"]["id"] == "b"

    def test_a_deleted_pin_leaves_the_list(self, keys):
        """The pin outlives its message until the next read: the window
        notices the message left the conversation."""
        panel = Panel([message("a"), message("b")])
        window = Window(panel)
        records = panel.conversation["messages"]["messages"]["records"]
        panel.run_message_command.side_effect = lambda msg, command: records.pop(0)
        keys(window, "messages.ID_DELETE_MSG")
        assert [m["key"]["id"] for m in window._rows] == ["b"]

    def test_reply_closes_first_and_runs_on_the_message_there(self, keys):
        panel = Panel([message("a")])
        window = Window(panel)
        keys(window, "messages.ID_ALT_R")
        window.EndModal.assert_called_once_with(wx.ID_OK)
        panel.run_message_command.assert_not_called()
        window.then()
        panel.run_message_command.assert_called_once_with(message("a"), "ID_ALT_R")

    def test_the_ai_action_gets_the_message_and_the_window(self, keys):
        panel = Panel([message("a")])
        window = Window(panel)
        keys(window, pinned_list.AI_ACTION)
        assert panel._on_ai_action.call_args.kwargs == {"message": message("a"), "parent": window}

    def test_other_keys_pass(self, keys):
        window = Window(Panel([message("a")]))
        event = keys(window, None, code=wx.WXK_ESCAPE)
        event.Skip.assert_called_once()

    def test_keys_pass_while_the_list_is_empty(self, keys):
        panel = Panel([])
        window = Window(panel)
        event = keys(window, "messages.ID_CTRL_C")
        event.Skip.assert_called_once()
        panel.run_message_command.assert_not_called()


class TestWindowSelection:
    def test_ctrl_space_toggles_the_conversations_selection(self, keys):
        panel = Panel([message("a")])
        window = Window(panel)
        keys(window, pinned_list.SELECT)
        panel._toggle_message_selection.assert_called_once_with(panel._sorted_messages[0])

    def test_shift_down_moves_and_adds_the_next_pin(self, keys):
        panel = Panel([message("a"), message("b")])
        window = Window(panel)
        keys(window, pinned_list.SELECT_NEXT)
        assert window._list.selection == 1
        assert panel.selected_messages == {"b"}
        assert window._list.items == ["Content a", "Content b selected"]
        panel.selection_sound.play.assert_called_once()

    def test_shift_up_at_the_top_does_nothing(self, keys):
        panel = Panel([message("a")])
        window = Window(panel)
        keys(window, pinned_list.SELECT_PREVIOUS)
        assert panel.selected_messages == set()

    def test_ctrl_shift_space_selects_every_pin_then_clears_them(self, keys):
        panel = Panel([message("a"), message("b")])
        panel.selected_messages = {"other"}
        window = Window(panel)
        keys(window, pinned_list.SELECT_ALL)
        assert panel.selected_messages == {"a", "b", "other"}
        keys(window, pinned_list.SELECT_ALL)
        assert panel.selected_messages == {"other"}
        window._mw.output.assert_called_with("unselected", interrupt=True)

    def test_ctrl_enter_shows_the_saved_file(self, keys):
        panel = Panel([message("a")])
        keys(Window(panel), pinned_list.SHOW_FOLDER)
        panel.show_message_in_folder.assert_called_once()


class TestWindowMenu:
    def test_go_to_message_item_closes_on_it(self):
        window = Window(Panel([message("a")]))
        window._menu_message, window._menu_go_id = message("a"), 9
        window._on_menu(SimpleNamespace(GetId=lambda: 9, GetInt=lambda: 0))
        assert window.target == message("a")

    def test_other_items_reach_the_panel_with_the_message(self):
        panel = Panel([message("a")])
        window = Window(panel)
        window._menu_message, window._menu_go_id = message("a"), 9
        window._on_menu(SimpleNamespace(GetId=lambda: 42, GetInt=lambda: 1))
        panel.forward_message_menu_event.assert_called_once_with(message("a"), 42, 1)

    def test_a_click_with_no_menu_open_is_passed_on(self):
        window = Window(Panel([message("a")]))
        event = SimpleNamespace(GetId=lambda: 42, Skip=Mock())
        window._on_menu(event)
        event.Skip.assert_called_once()

    def test_items_of_the_conversation_close_the_window_first(self):
        window = Window(Panel([message("a")]))
        window._menu_message = message("a")
        handler = Mock()
        window._leave_menu(handler)("event")
        window.EndModal.assert_called_once_with(wx.ID_OK)
        handler.assert_not_called()
        window.then()
        handler.assert_called_once_with("event")
