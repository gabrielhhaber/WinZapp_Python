"""Native filter/manager handlers against recording stubs; never construct wx."""

from types import SimpleNamespace
import pytest
import wx

from core.chat_lists import ListSnapshot, WhatsAppList
from main_window.chat_list import ChatListMixin
from main_window import chat_list as rendering
from ui.conversation_panel.chat_lists import WhatsAppListFilterMixin
from ui.dialogs.chat_lists import WhatsAppListsDialog
from ui.dialogs import chat_lists as manager
from tests.test_whatsapp_chat_lists_jobs import Window, PN, LID, LOCKED, OTHER


class Choice:
    def __init__(self, items=(), selection=0):
        self.items, self.selection, self.frozen, self.writes = list(items), selection, 0, 0
    def GetItems(self): return self.items
    def GetSelection(self): return self.selection
    def SetSelection(self, selection): self.selection = selection
    def SetItems(self, items): self.items = list(items); self.writes += 1
    def Freeze(self): self.frozen += 1
    def Thaw(self): self.frozen -= 1


class Rows:
    def __init__(self):
        self.rows, self.focused, self.selected, self.frozen, self.focus_moves = [], -1, set(), 0, 0
    def GetItemCount(self): return len(self.rows)
    def GetItemText(self, index, col=0): return self.rows[index]
    def GetFocusedItem(self): return self.focused
    def SetItem(self, index, col, text): self.rows[index] = text
    def DeleteAllItems(self): self.rows.clear(); self.focused = -1
    def Append(self, texts): self.rows.append(texts[0])
    def Freeze(self): self.frozen += 1
    def Thaw(self): self.frozen -= 1
    def Focus(self, index): self.focused = index
    def Select(self, index, on=True): self.selected.add(index) if on else self.selected.discard(index)
    def IsSelected(self, index): return index in self.selected
    def EnsureVisible(self, index): pass
    def SetFocus(self): self.focus_moves += 1
    def SetItemState(self, *args): pass


class FilterWindow(Window, ChatListMixin):
    def __init__(self):
        super().__init__()
        del self.archived_conversations_panel
        self.i18n = SimpleNamespace(t=lambda key: key)
        self.conversations_panel.conversations_list = Rows()
        self.conversations_panel.search_field = SimpleNamespace(GetValue=lambda: "")
        self.conversations_panel.conversation = None
        self.conversations_panel._conv_filter = "all"
        self.conversations_panel._wa_list_id = "42"
        self._initial_sync_running = True
    def _search_normalization_mode(self): return "off"
    def _build_chat_item_text(self, chat, name): return name
    def _allow_ui_focus_changes(self): return False
    def _apply_chat_rows_incrementally(self, *args): return False


@pytest.fixture
def filter_window(monkeypatch):
    # Do not ask wx for desktop focus; a recording sentinel owns it.
    sentinel = object()
    monkeypatch.setattr(rendering.wx, "Window", SimpleNamespace(FindFocus=lambda: sentinel))
    return FilterWindow()


def test_list_filter_uses_remote_members_and_never_shows_locked_chat(filter_window):
    window = filter_window
    window.add_chats_to_ui()
    assert window.conversations_panel.chat_names == ["Ada"]
    assert window.conversations_panel.chats_list == [{"remoteJid": PN}]
    assert window.conversations_panel.conversations_list.rows == ["Ada"]
    assert window.conversations_panel.conversations_list.frozen == 0


def test_list_filter_combines_with_search_and_four_existing_tabs(filter_window):
    window = filter_window
    window.conversations_panel.search_field = SimpleNamespace(GetValue=lambda: "Bea")
    window.add_chats_to_ui()
    assert window.conversations_panel.chats_list == []
    window.conversations_panel.search_field = SimpleNamespace(GetValue=lambda: "")
    window.conversations_panel._conv_filter = "groups"
    window.add_chats_to_ui()
    assert window.conversations_panel.chats_list == []


def test_unchanged_rows_still_update_backing_list_after_unfiltered_sync_overwrites_it(filter_window):
    window = filter_window
    window.add_chats_to_ui()
    window.conversations_panel.chats_list = window.conversations_panel._all_chats_list[:]
    window.add_chats_to_ui()
    assert window.conversations_panel.chat_names == ["Ada"]
    assert window.conversations_panel.chats_list == [{"remoteJid": PN}]


def test_changed_membership_repaints_using_current_snapshot_without_switching_filter(filter_window):
    window = filter_window
    window.add_chats_to_ui()
    window._wa_lists_snapshot = ListSnapshot((WhatsAppList("42", "Friends", frozenset({OTHER})),), True)
    window.add_chats_to_ui()
    assert window.conversations_panel.chat_names == ["Bea"]
    assert window.conversations_panel._wa_list_id == "42"


class FilterPanel(WhatsAppListFilterMixin):
    def __init__(self):
        self.main_window = Window()
        self.main_window.i18n = SimpleNamespace(t=lambda key: key)
        self.refreshes = 0
        self.main_window.add_chats_to_ui = lambda: setattr(self, "refreshes", self.refreshes + 1)
        self._wa_list_ids, self._wa_list_id = [None, "42"], "42"
        self._wa_list_choice = Choice(["wa_lists_all", "Friends"], 1)
        self.chats_list, self.conversations_list = [{"remoteJid": PN}], Rows()


def test_unchanged_choice_is_never_rewritten_or_given_keyboard_focus():
    panel = FilterPanel()
    panel._refresh_wa_list_choices()
    panel._on_wa_list_choice(None)
    assert panel._wa_list_choice.writes == 0
    assert panel.conversations_list.focus_moves == 0
    assert panel._wa_list_id == "42"


def test_remote_deletion_resets_selected_filter_by_id_not_duplicate_display_name():
    panel = FilterPanel()
    panel.main_window._wa_lists_snapshot = ListSnapshot((WhatsAppList("99", "Friends", frozenset()),), True)
    panel._refresh_wa_list_choices()
    assert panel._wa_list_id is None and panel._wa_list_ids == [None, "99"]
    assert panel._wa_list_choice.GetSelection() == 0 and panel._wa_list_choice.frozen == 0


def test_delete_requires_named_confirmation_and_defaults_to_no(monkeypatch):
    requests, prompts = [], []
    item = WhatsAppList("42", "Friends", frozenset({PN}))
    stub = SimpleNamespace(_selected_list=lambda: item,
        _mw=SimpleNamespace(i18n=SimpleNamespace(t=lambda key: "Delete {name}?")),
        _submit_list_command=requests.append)
    def refuse(message, title, flags, **kwargs):
        prompts.append((message, flags))
        return wx.NO
    monkeypatch.setattr(manager.wx, "MessageBox", refuse)
    WhatsAppListsDialog._delete_list(stub, None)
    assert requests == [] and prompts[0][0] == "Delete Friends?"
    assert prompts[0][1] & wx.NO_DEFAULT
    monkeypatch.setattr(manager.wx, "MessageBox", lambda *args, **kwargs: wx.YES)
    WhatsAppListsDialog._delete_list(stub, None)
    assert requests == [{"action": "remove", "id": "42"}]


def test_closed_manager_ignores_late_callback_and_speech():
    callbacks, writes, announcements = [], [], []
    stub = SimpleNamespace(_closed=False, _busy=False,
        _refresh_list_manager=lambda: None, _status=SimpleNamespace(SetLabel=writes.append),
        _mw=SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key),
                            _request_wa_lists=lambda callback, command: callbacks.append(callback),
                            output=announcements.append))
    WhatsAppListsDialog._submit_list_command(stub)
    stub._closed = True
    callbacks[0](SimpleNamespace(outcome="loaded"))
    assert writes == ["wa_lists_loading"] and announcements == []


@pytest.mark.parametrize("mode", ["cancel", "new_lock", "accept"])
def test_member_picker_cancel_or_stale_lock_cannot_write_and_accept_preserves_hidden_members(monkeypatch, mode):
    window, requests, shown = Window(), [], []
    window.i18n = SimpleNamespace(t=lambda key: key)
    window.output = lambda text: None
    item = WhatsAppList("42", "Friends", frozenset({LID, LOCKED, "900000000000999@lid"}))
    class Picker:
        def ShowModal(self):
            if mode == "new_lock":
                window.locked.add(PN)
            return wx.ID_CANCEL if mode == "cancel" else wx.ID_OK
        def GetSelections(self):
            return [0]
        def Destroy(self):
            pass
    def picker(parent, prompt, title, names):
        shown.extend(names)
        return Picker()
    monkeypatch.setattr(manager.wx, "MultiChoiceDialog", picker)
    stub = SimpleNamespace(_selected_list=lambda: item, _mw=window,
                           _submit_list_command=requests.append)
    WhatsAppListsDialog._change_list_members(stub, "removeChats")
    assert shown == ["Ada"]
    assert requests == ([{"action": "removeChats", "id": "42", "chatIds": [LID]}]
                        if mode == "accept" else [])
