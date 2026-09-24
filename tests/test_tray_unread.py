"""Regression test: the system-tray tooltip counted archived conversations
toward its unread total/name list.

Archived chats get their own unread indicator on the "Conversas arquivadas"
nav item (see MainWindow._update_title()'s docstring for the original
rationale) — the tray tooltip is supposed to follow the same rule, but
TrayIcon._get_unread_info() iterated every chat with no archived/deleted
exclusion at all.

TrayIcon is a wx.adv.TaskBarIcon and can't be instantiated without a running
wx.App, so _get_unread_info is exercised as a plain function bound to a
lightweight stub — same approach as tests/test_reported_bugfixes.py.
"""

from core.tray_manager import TrayIcon


class _MainWindowStub:
    def __init__(self, chats, archived_jids=(), locked_jids=()):
        self.chats = chats
        self._sync_completed = True
        self._deleted_chats = set()
        self._archived_jids = set(archived_jids)
        self._locked_jids = set(locked_jids)

    def is_chat_archived(self, jid):
        return jid in self._archived_jids

    def is_chat_locked(self, jid):
        return jid in self._locked_jids

    def _resolve_contact_name(self, chat):
        return ""

    def find_name_through_messages(self, chat):
        return ""

    def find_jid_through_messages(self, chat):
        return ""


def _chat(unread):
    return {
        "unreadCount": unread,
        "messages": {"messages": {"records": [{"key": {"id": "1"}}]}},
    }


class _TrayStub:
    _get_unread_info = TrayIcon._get_unread_info

    def __init__(self, main_window):
        self.main_window = main_window


def test_archived_chat_excluded_from_tray_unread_total():
    mw = _MainWindowStub(
        chats={
            "a@s.whatsapp.net": _chat(3),
            "b@g.us": _chat(5),
        },
        archived_jids={"b@g.us"},
    )
    tray = _TrayStub(mw)
    total, names = tray._get_unread_info()
    assert total == 3
    assert len(names) == 1


def test_all_chats_archived_gives_zero_total():
    mw = _MainWindowStub(
        chats={"a@g.us": _chat(7)},
        archived_jids={"a@g.us"},
    )
    tray = _TrayStub(mw)
    total, names = tray._get_unread_info()
    assert total == 0
    assert names == []


def test_no_archived_chats_counts_everything_as_before():
    mw = _MainWindowStub(
        chats={
            "a@s.whatsapp.net": _chat(2),
            "b@s.whatsapp.net": _chat(1),
        },
    )
    tray = _TrayStub(mw)
    total, names = tray._get_unread_info()
    assert total == 3
    assert len(names) == 2


def test_locked_chat_is_excluded_from_tray_total_and_name_list():
    mw = _MainWindowStub(
        chats={
            "visible@s.whatsapp.net": _chat(2),
            "private@s.whatsapp.net": _chat(7),
        },
        locked_jids={"private@s.whatsapp.net"},
    )
    tray = _TrayStub(mw)

    total, names = tray._get_unread_info()

    assert total == 2
    assert names == ["visible"]
