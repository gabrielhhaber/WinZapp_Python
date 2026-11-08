"""An empty, narrowed conversation list says why it is empty.

A filter, a WhatsApp list or a search matching nothing used to leave the list
with no row, so a screen reader said nothing on reaching it. It now shows one
row (core/chat_list_placeholder.py) that is not a conversation: chats_list
stays empty under it, and the row is not rewritten on every refresh.
Also the empty-row texts of a channel and of the Channels window.
"""

import types

import pytest

from core.chat_list_placeholder import empty_chat_list_key
from core.chat_notices import empty_conversation_key
from main import MainWindow
from tests.test_chat_list_incremental_update import _FakeListCtrl


class _List(_FakeListCtrl):
    def Append(self, row):
        self.calls.append(("Append", row[0]))
        self.rows.append(row[0])

    def SetItemState(self, *args):
        pass

    def SetFocus(self):
        self.calls.append(("SetFocus", None))


class _I18n:
    def t(self, key):
        return f"<{key}>"


class _Field:
    def __init__(self, value=""):
        self.value = value

    def GetValue(self):
        return self.value


def _chat(jid, unread=0):
    return {"remoteJid": jid, "unreadCount": unread,
            "messages": {"messages": {"records": [{"key": {"id": "m"}}]}}}


def _window(chats, conv_filter="all", search=""):
    panel = types.SimpleNamespace(
        search_field=_Field(search), _conv_filter=conv_filter, _wa_list_id=None,
        _all_chats_list=list(chats), _all_chat_names=[c["remoteJid"] for c in chats],
        chats_list=list(chats), chat_names=[c["remoteJid"] for c in chats],
        conversations_list=_List(), _preserved_focused_jid=None, conversation=None,
        _displayed_jids=None, SetFocus=lambda: None)
    win = types.SimpleNamespace(
        conversations_panel=panel, i18n=_I18n(), _lid_to_phone={},
        _initial_sync_running=False,
        _search_normalization_mode=lambda: "none",
        _wa_list_filter_identities=lambda: None,
        is_chat_locked=lambda jid: False,
        _build_chat_item_text=lambda chat, name: name,
        _allow_ui_focus_changes=lambda: False,
        _conversation_search_candidates=None)
    win._apply_chat_rows_incrementally = types.MethodType(
        MainWindow._apply_chat_rows_incrementally, win)
    win.add_chats_to_ui = types.MethodType(MainWindow.add_chats_to_ui, win)
    return win, panel


@pytest.fixture(autouse=True)
def _no_focus(monkeypatch):
    import main as main_module
    monkeypatch.setattr(main_module.wx.Window, "FindFocus", staticmethod(lambda: None))


@pytest.mark.parametrize("search, conv_filter, wa_list, key", [
    ("joão", "unread", None, "chat_list_no_search_results"),
    ("", "unread", None, "chat_list_no_unread"),
    ("", "groups", None, "chat_list_no_groups"),
    ("", "individual", None, "chat_list_no_individual"),
    ("", "all", "list-1", "chat_list_empty_list"),
    ("", "all", None, ""),
    ("", None, None, ""),
])
def test_the_row_names_what_narrowed_the_list(search, conv_filter, wa_list, key):
    assert empty_chat_list_key(search, conv_filter, wa_list) == key


def test_an_empty_filter_shows_one_row_that_is_not_a_chat():
    win, panel = _window([_chat("a@s.whatsapp.net")], conv_filter="unread")
    win.add_chats_to_ui()
    assert panel.conversations_list.rows == ["<chat_list_no_unread>"]
    assert panel.chats_list == [] and panel._displayed_jids == []


def test_the_row_is_not_rewritten_on_every_refresh():
    win, panel = _window([_chat("a@s.whatsapp.net")], conv_filter="unread")
    win.add_chats_to_ui()
    panel.conversations_list.calls.clear()
    win.add_chats_to_ui()
    assert panel.conversations_list.calls == []
    assert panel.conversations_list.rows == ["<chat_list_no_unread>"]


def test_the_row_goes_when_a_chat_matches_again():
    chat = _chat("a@s.whatsapp.net")
    win, panel = _window([chat], conv_filter="unread")
    win.add_chats_to_ui()
    chat["unreadCount"] = 2
    win.add_chats_to_ui()
    assert panel.conversations_list.rows == ["a@s.whatsapp.net"]
    assert panel.chats_list == [chat] and panel._chat_list_placeholder == ""


def test_a_search_with_no_match_says_so():
    win, panel = _window([_chat("a@s.whatsapp.net")], search="zzz")
    win.add_chats_to_ui()
    assert panel.conversations_list.rows == ["<chat_list_no_search_results>"]


def test_the_unfiltered_list_keeps_its_rows():
    win, panel = _window([_chat("a@s.whatsapp.net"), _chat("b@g.us")])
    win.add_chats_to_ui()
    assert panel.conversations_list.rows == ["a@s.whatsapp.net", "b@g.us"]


def test_a_channel_with_no_updates_says_updates():
    assert empty_conversation_key("120363000000000001@newsletter") == "no_updates_in_channel"
    assert empty_conversation_key("5511999999999@s.whatsapp.net") == "no_messages_in_conversation"
    assert empty_conversation_key(None) == "no_messages_in_conversation"


def test_the_channels_window_says_why_its_list_is_empty():
    from ui.dialogs.channels import FIND, MINE, empty_list_text
    i18n = _I18n()
    assert empty_list_text(MINE, False, False, i18n) == "<loading>"
    assert empty_list_text(MINE, True, False, i18n) == "<channels_none>"
    assert empty_list_text(FIND, True, False, i18n) == "<loading>"
    assert empty_list_text(FIND, True, True, i18n) == "<channels_nothing_found>"
