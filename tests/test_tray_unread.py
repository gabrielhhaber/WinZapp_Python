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
        self.settings = {}
        self._archived_jids = set(archived_jids)
        self._locked_jids = set(locked_jids)

    def is_chat_archived(self, jid):
        return jid in self._archived_jids

    def is_chat_locked(self, jid):
        return jid in self._locked_jids

    def is_chat_muted(self, jid):
        return False

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


def test_a_chat_rekeyed_while_the_tooltip_is_built_does_not_raise():
    """Reported on a Neonize account downloading media: the chat list was
    rewritten (a chat moved under another address) while the tray tooltip
    walked it, and "dictionary keys changed during iteration" escaped from
    _update_title. The walk now reads a snapshot, like the title's own count."""

    class Rekeying(_MainWindowStub):
        def _resolve_contact_name(self, chat):
            # What a sync on another thread does between two steps of the
            # walk: the chat already counted moves to its phone address.
            if "old@lid" in self.chats:
                self.chats["new@s.whatsapp.net"] = self.chats.pop("old@lid")
            return "Ana"

    mw = Rekeying(chats={"old@lid": _chat(2), "a@s.whatsapp.net": _chat(1)})
    total, names = _TrayStub(mw)._get_unread_info()
    assert total == 3 and names == ["Ana", "Ana"]


def test_no_code_walks_the_live_chat_dictionary():
    """The window's chats dict is rewritten by sync threads; every walk of it
    goes over a snapshot (list(...)), or a re-keyed chat raises mid-walk."""
    import re
    from pathlib import Path

    client = Path(__file__).resolve().parents[1] / "client"
    pattern = re.compile(r"\b(?:self|mw|self\._mw|self\.main_window|main_window)\.chats\.items\(\)")
    live = []
    for path in client.rglob("*.py"):
        if "api" in path.relative_to(client).parts[:1] or "venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            if not text[:match.start()].endswith(("list(", "dict(", "tuple(")):
                line = text.count("\n", 0, match.start()) + 1
                live.append(f"{path.relative_to(client)}:{line}")
    assert live == []
