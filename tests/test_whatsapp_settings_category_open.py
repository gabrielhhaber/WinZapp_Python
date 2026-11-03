"""Window-free checks for opening the WhatsApp settings category: the menu
entry routes through open_settings, which selects the category, and each
button's window reads WhatsApp only the first time it opens."""

import types

import wx
from main_window.shortcuts import ShortcutsMixin
from main_window.whatsapp_privacy import WhatsAppPrivacyMixin
from ui.dialogs.settings_dialog import SettingsDialog
from ui.dialogs.settings_pages import SubPageDialog


def test_menu_entry_opens_settings_on_the_whatsapp_category():
    calls = []
    stub = types.SimpleNamespace(open_settings=lambda **kw: calls.append(kw))
    WhatsAppPrivacyMixin.open_whatsapp_settings(stub)
    assert calls == [{"whatsapp": True}]


class _FakeDialog:
    instances = []

    def __init__(self, parent):
        self.events = []
        _FakeDialog.instances.append(self)

    def show_whatsapp_settings(self):
        self.events.append("whatsapp")

    def ShowModal(self):
        self.events.append("modal")

    def Destroy(self):
        self.events.append("destroy")


def _open_settings_stub():
    return types.SimpleNamespace(
        _chat_lock_unlocked=True,
        lock_chat_vault=lambda **kw: None,
        _cancel_chat_lock_timeout=lambda: None,
        touch_chat_lock_timeout=lambda: None,
    )


def test_open_settings_selects_the_category_only_when_asked(monkeypatch):
    monkeypatch.setattr("ui.dialogs.settings_dialog.SettingsDialog", _FakeDialog)
    _FakeDialog.instances.clear()
    stub = _open_settings_stub()
    ShortcutsMixin.open_settings(stub, whatsapp=True)
    ShortcutsMixin.open_settings(stub)
    with_category, plain = _FakeDialog.instances
    assert with_category.events == ["whatsapp", "modal", "destroy"]
    assert plain.events == ["modal", "destroy"]


def test_show_whatsapp_settings_selects_its_page_and_focuses_the_list(monkeypatch):
    selected, focused = [], []
    page = object()
    notebook = types.SimpleNamespace(
        FindPage=lambda p: 7 if p is page else wx.NOT_FOUND,
        SetSelection=selected.append,
        category_list=types.SimpleNamespace(SetFocus=lambda: focused.append(True)),
    )
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a: fn(*a))
    stub = types.SimpleNamespace(_notebook=notebook, _whatsapp_page=page)
    SettingsDialog.show_whatsapp_settings(stub)
    assert selected == [7] and focused == [True]


def _sub_page(first_open):
    events = []
    stub = types.SimpleNamespace(
        _first_open=first_open,
        _build=None,
        CentreOnParent=lambda: events.append("centre"),
        ShowModal=lambda: events.append("modal"),
    )
    return stub, events


def test_first_open_hook_runs_once_before_the_window_shows():
    stub, events = _sub_page(lambda: events.append("load"))
    SubPageDialog.open(stub)
    SubPageDialog.open(stub)
    assert events == ["centre", "load", "modal", "centre", "modal"]


def test_a_window_without_a_hook_just_shows():
    stub, events = _sub_page(None)
    SubPageDialog.open(stub)
    assert events == ["centre", "modal"]
