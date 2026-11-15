"""Which chats count toward the unread tally of the tray icon and window title."""
from types import SimpleNamespace

import pytest

from core.tray_manager import TrayIcon
from core.unread_badge import (
    DISCOUNT_ARCHIVED_KEY,
    DISCOUNT_MUTED_KEY,
    badge_discount_flags,
    counts_toward_badge,
)
from main_window.window_chrome import WindowChromeMixin


def _settings(archived=None, muted=None):
    general = {}
    if archived is not None:
        general[DISCOUNT_ARCHIVED_KEY] = archived
    if muted is not None:
        general[DISCOUNT_MUTED_KEY] = muted
    return {"general": general}


def test_both_discounts_default_to_on():
    assert badge_discount_flags({}) == (True, True)
    assert badge_discount_flags(None) == (True, True)
    assert badge_discount_flags(_settings()) == (True, True)


@pytest.mark.parametrize(
    "archived_opt, muted_opt, archived, muted, expected",
    [
        (True, True, False, False, True),
        (True, True, True, False, False),
        (True, True, False, True, False),
        (False, True, True, False, True),
        (False, True, True, True, False),
        (True, False, False, True, True),
        (True, False, True, True, False),
        (False, False, True, True, True),
    ],
)
def test_counts_toward_badge(archived_opt, muted_opt, archived, muted, expected):
    settings = _settings(archived_opt, muted_opt)
    assert counts_toward_badge(settings, archived=archived, muted=muted) is expected


class _Window:
    def __init__(self, settings, chats, archived=(), muted=()):
        self.settings = settings
        self.chats = chats
        self._sync_completed = True
        self._deleted_chats = set()
        self._archived = set(archived)
        self._muted = set(muted)

    def is_chat_archived(self, jid):
        return jid in self._archived

    def is_chat_muted(self, jid):
        return jid in self._muted

    def is_chat_locked(self, jid):
        return False

    def _resolve_contact_name(self, chat):
        return chat.get("name")

    def find_name_through_messages(self, chat):
        return ""

    def find_jid_through_messages(self, chat):
        return ""


def _msgs(n):
    return {"messages": {"records": [{}] * n}}


def _chats():
    return {
        "a@s.whatsapp.net": {"name": "A", "unreadCount": 1, "messages": _msgs(1)},
        "b@s.whatsapp.net": {"name": "B", "unreadCount": 2, "messages": _msgs(2)},
        "c@s.whatsapp.net": {"name": "C", "unreadCount": 4, "messages": _msgs(4)},
    }


def _tray(window):
    return SimpleNamespace(main_window=window)


def _tally(window):
    return TrayIcon._get_unread_info(_tray(window))


def test_tray_leaves_out_muted_and_archived_by_default():
    window = _Window({}, _chats(), archived={"b@s.whatsapp.net"}, muted={"c@s.whatsapp.net"})
    total, names = _tally(window)
    assert (total, names) == (1, ["A"])


def test_tray_counts_them_when_the_options_are_off():
    window = _Window(
        _settings(False, False), _chats(),
        archived={"b@s.whatsapp.net"}, muted={"c@s.whatsapp.net"},
    )
    total, names = _tally(window)
    assert total == 7
    assert sorted(names) == ["A", "B", "C"]


def test_window_title_follows_the_same_rule():
    titles = []

    class Frame(_Window):
        _initial_sync_running = False

        def _format_title(self, unread=0):
            titles.append(unread)
            return "t"

        offline_mode = False
        _tray_status = ""
        tray_icon = None

        def GetTitle(self):
            return "t"

    frame = Frame({}, _chats(), archived={"b@s.whatsapp.net"}, muted={"c@s.whatsapp.net"})
    WindowChromeMixin._update_title(frame)
    assert titles == [1]
    frame = Frame(_settings(False, False), _chats(), archived={"b@s.whatsapp.net"})
    WindowChromeMixin._update_title(frame)
    assert titles[-1] == 3
