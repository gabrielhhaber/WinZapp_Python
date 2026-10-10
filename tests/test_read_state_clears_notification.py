"""Reading a chat in the app removes its toast from the notification area."""

import pytest

from main_window.read_state import ReadStateMixin


class _Stop(Exception):
    pass


class _Manager:
    def __init__(self):
        self.cleared = []

    def clear_for_chat(self, jid):
        self.cleared.append(jid)


class _Window:
    def __init__(self, chats, manager):
        self.chats = chats
        if manager is not None:
            self.notification_manager = manager

    def _normalize_jid(self, jid):
        raise _Stop  # the toast was cleared before anything else ran


def test_reading_a_chat_clears_its_notification(monkeypatch):
    import main_window.read_state as rs
    monkeypatch.setattr(rs.wx, "IsMainThread", lambda: True)
    manager = _Manager()
    win = _Window({"a@s.whatsapp.net": {}}, manager)
    with pytest.raises(_Stop):
        ReadStateMixin.mark_conversation_as_read(win, "a@s.whatsapp.net")
    assert manager.cleared == ["a@s.whatsapp.net"]


def test_unknown_chat_clears_nothing(monkeypatch):
    import main_window.read_state as rs
    monkeypatch.setattr(rs.wx, "IsMainThread", lambda: True)
    manager = _Manager()
    ReadStateMixin.mark_conversation_as_read(_Window({}, manager), "a@s.whatsapp.net")
    assert manager.cleared == []
