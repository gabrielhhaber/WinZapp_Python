"""The Contacts panel of the main navigation (issue #770): core/contacts_tab.py
and ContactsPanel's handlers on a plain stub (no window)."""
from types import SimpleNamespace

import pytest

from core.contacts_tab import contact_rows, enter_action, matching_rows, row_label
from ui.contacts_panel import ContactsPanel

PHONE = "5511999990000@s.whatsapp.net"
LID = "123456789012345@lid"


def number(jid):
    return "+" + jid.split("@")[0]


def test_one_row_per_person_by_the_number_and_by_name():
    contacts = {
        PHONE: {"name": "Bia", "isMyContact": True},
        LID: {"name": "Bia", "isMyContact": True},                         # the same person
        "551199990000@s.whatsapp.net": {"name": "Bia", "isSaved": True},   # 8-digit form
        "5522988880000@s.whatsapp.net": {"name": "ana", "syncToAddressbook": True},
        "777@lid": {"name": "Caio", "isMyContact": True},                  # no number known
        "5533977770000@s.whatsapp.net": {"name": "Not saved"},
    }
    rows = contact_rows(contacts, {LID: PHONE}, lambda jid: "", number)
    assert rows == [("5522988880000@s.whatsapp.net", "ana", "+5522988880000"),
                    (PHONE, "Bia", "+5511999990000"),
                    ("777@lid", "Caio", "")]


def test_a_mapped_lid_alone_is_shown_by_its_number():
    rows = contact_rows({LID: {"name": "Bia", "isMyContact": True}}, {LID: PHONE},
                        lambda jid: "", number)
    assert rows == [(PHONE, "Bia", "+5511999990000")]


def test_row_labels_never_show_a_jid():
    assert row_label("Bia", "+55 11 99999-0000", "?") == "Bia, +55 11 99999-0000"
    assert row_label("", "", "Unknown") == "Unknown"
    assert row_label("+55 11", "+55 11", "?") == "+55 11"


def test_the_search_looks_in_names_and_in_numbers():
    rows = [("1", "Ana Souza", "+55 11 99999-0000"), ("2", "Bia", "+55 21 98888-0000")]
    assert matching_rows(rows, "souza") == rows[:1]
    assert matching_rows(rows, "21 9888") == rows[1:]
    assert matching_rows(rows, "") == rows
    assert matching_rows(rows, "x1") == []


@pytest.mark.parametrize("settings, action", [
    ({}, "open"), ({"contacts_tab": {"enter_action": "call"}}, "call"),
    ({"contacts_tab": {"enter_action": "nonsense"}}, "open"), ({"contacts_tab": "bad"}, "open"),
])
def test_enter_follows_the_stored_choice(settings, action):
    assert enter_action(settings) == action


class Panel:
    _do_enter_action = ContactsPanel._do_enter_action
    _on_open = ContactsPanel._on_open
    _on_call = ContactsPanel._on_call
    _on_video = ContactsPanel._on_video
    _selected = ContactsPanel._selected

    def __init__(self, action):
        self.done = []
        self._shown = [(PHONE, "Bia", "+55")]
        self._list = SimpleNamespace(GetSelection=lambda: 0)
        self._mw = SimpleNamespace(
            settings={"contacts_tab": {"enter_action": action}},
            restore_deleted_chat=lambda jid: self.done.append(("restore", jid)),
            navigate_to_conversation_jid=lambda jid, name: self.done.append(("open", jid, name)),
            start_voice_call=lambda jid, name: self.done.append(("call", jid, name)),
            start_video_call=lambda jid, name: self.done.append(("video", jid, name)))
        self._on_info = lambda: self.done.append("info")


@pytest.mark.parametrize("action, done", [
    ("open", [("restore", PHONE), ("open", PHONE, "Bia")]),
    ("call", [("call", PHONE, "Bia")]),
    ("video", [("video", PHONE, "Bia")]),
    ("info", ["info"]),
])
def test_enter_on_a_contact_does_the_chosen_action(action, done):
    panel = Panel(action)
    panel._do_enter_action()
    assert panel.done == done


def test_the_panel_is_one_of_the_main_windows_side_panels():
    from main_window.content_panels import SIDE_PANELS
    assert "contacts_panel" in SIDE_PANELS


def test_the_navigation_list_offers_contacts(monkeypatch):
    from ui import navigation
    monkeypatch.setattr(navigation, "navigation_shortcut_label", lambda mw, key, label: label)
    rows = []
    nav_list = SimpleNamespace(
        GetFocusedItem=lambda: -1, Freeze=lambda: None, Thaw=lambda: None,
        DeleteAllItems=lambda: rows.clear(), Append=lambda row: rows.append(row[0]),
        Focus=lambda i: None, Select=lambda i: None)
    panel = SimpleNamespace(
        nav_list=nav_list, refresh_archived_label=lambda: None,
        main_window=SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key)))
    navigation.NavigationPanel.rebuild_items(panel)
    assert "contacts" in panel._nav_keys
    assert "contacts_nav" in rows


class InfoPanel:
    _on_info = ContactsPanel._on_info
    _selected = ContactsPanel._selected

    def __init__(self, hidden, pin_ok, stored):
        self.opened = []
        self.unlocks = []
        self._shown = [(PHONE, "Bia", "+55")]
        self._list = SimpleNamespace(GetSelection=lambda: 0)
        self._mw = SimpleNamespace(
            is_chat_hidden_by_vault=lambda jid: hidden and jid == stored,
            unlock_chat_lock_vault=lambda show_panel=True: self.unlocks.append(show_panel) or pin_ok)
        self.stored = stored

    def reload(self):
        pass

    def __bool__(self):
        return True


@pytest.mark.parametrize("pin_ok, opened", [(False, 0), (True, 1)])
def test_info_on_a_hidden_chats_contact_asks_for_the_pin(monkeypatch, pin_ok, opened):
    from ui.dialogs import conversation_data_dialog, new_conversation
    stored = "5511999990000@lid"
    monkeypatch.setattr(new_conversation.NewConversationDialog, "_find_existing_chat",
                        staticmethod(lambda mw, jid: (jid, {"remoteJid": stored})))
    shown = []

    class Dialog:
        def __init__(self, mw, chat):
            shown.append(chat["remoteJid"])
        ShowModal = Destroy = lambda self: None
    monkeypatch.setattr(conversation_data_dialog, "ConversationDataDialog", Dialog)
    panel = InfoPanel(True, pin_ok, stored)
    panel._on_info()
    assert panel.unlocks == [False]
    assert len(shown) == opened
