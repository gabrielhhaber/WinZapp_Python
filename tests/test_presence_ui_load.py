"""Presence bursts must not rescan unread state or rewrite unchanged UI names.

Real mixin methods on plain objects; no wx.App, widgets, network or timers.
"""
from types import SimpleNamespace

import pytest

from core.contact_presence import cached
from main_window.chat_events import ChatEventsMixin
from main_window.window_chrome import WindowChromeMixin
from ui.navigation import NavigationPanel


class Window:
    _update_title = WindowChromeMixin._update_title

    def __init__(self):
        self.chats = {}
        self._deleted_chats = set()
        self.offline_mode = False
        self._tray_status = ""
        self.title = "WinZapp"
        self.writes = []
        self.i18n = SimpleNamespace(t=lambda key: key)

    def _format_title(self, unread):
        return "WinZapp" + (f" ({unread})" if unread else "")

    def is_chat_archived(self, jid): return False
    def is_chat_locked(self, jid): return False
    def GetTitle(self): return self.title

    def SetTitle(self, title):
        self.writes.append(title)
        self.title = title


def test_unchanged_title_never_reaches_the_native_setter():
    window = Window()
    for _ in range(100):
        window._update_title()
    assert window.writes == []


def test_real_unread_and_status_changes_still_update_the_title():
    window = Window()
    window.chats["123@s.whatsapp.net"] = {
        "unreadCount": 2, "messages": {"messages": {"records": [{}]}}}
    window._update_title()
    window._update_title()
    window._tray_status = "connecting"
    window._update_title()
    assert window.writes == ["WinZapp (1)", "WinZapp (1) | connecting"]


def test_archived_navigation_does_not_rewrite_the_same_accessible_name():
    writes = []
    nav = SimpleNamespace(
        _nav_keys=["archived"],
        main_window=SimpleNamespace(
            i18n=SimpleNamespace(t=lambda key: key), settings={},
            get_archived_unread_count=lambda: 0),
        nav_list=SimpleNamespace(GetItemText=lambda index: "archived_chats_nav",
                                 SetItemText=lambda *args: writes.append(args)))
    NavigationPanel.refresh_archived_label(nav)
    assert writes == []


class PresenceOwner:
    on_presence_update = ChatEventsMixin.on_presence_update

    def __init__(self):
        self._presence_cache = {}
        self._presence_timers = {}
        self._presence_pushname_map = {}
        self._lid_to_phone, self._phone_to_lid = {}, {}
        self.contacts, self.chats, self.settings = {}, {}, {}
        self.db = self.conversations_panel = None
        self.rows = []

    def _normalize_jid(self, jid): return jid.replace("@c.us", "@s.whatsapp.net")
    def _refresh_chat_row_in_list(self, jid): self.rows.append(jid)


@pytest.mark.parametrize("state", ["available", "unavailable"])
def test_online_only_burst_updates_cache_without_any_chat_row_refresh(state):
    owner = PresenceOwner()
    for index in range(100):
        jid = f"{1000 + index}@s.whatsapp.net"
        owner.on_presence_update(jid, {jid: {"lastKnownPresence": state}})
        assert cached(owner, jid)["isOnline"] is (state == "available")
    assert owner.rows == []


def test_nonpresence_row_refresh_keeps_its_unread_title_update():
    titles = []
    owner = SimpleNamespace(_update_title=lambda: titles.append(True))
    ChatEventsMixin._refresh_chat_row_in_list(owner, "123@s.whatsapp.net")
    assert titles == [True]
