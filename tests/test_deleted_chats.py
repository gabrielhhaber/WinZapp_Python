"""core/deleted_chats.py and MainWindow.restore_deleted_chat(): a conversation
the user deleted comes back when it is used again, under any form of the
person (reported 2026-11-17: deleted, reopened from "Nova conversa", a message
sent, and it never came back to the list)."""
from types import SimpleNamespace

from core.deleted_chats import marks_to_lift
from main_window.chat_actions import ChatActionsMixin
from ui.dialogs.new_conversation import NewConversationDialog

PHONE9 = "5511999990000@s.whatsapp.net"
PHONE8 = "551199990000@s.whatsapp.net"
LID = "123456789012345@lid"
OTHER = "5522988880000@s.whatsapp.net"
GROUP = "1203-4@g.us"


def test_every_form_of_the_person_is_lifted_and_nobody_else():
    deleted = {PHONE8, LID, OTHER, GROUP}
    assert marks_to_lift(deleted, PHONE9, {LID: PHONE8}, {PHONE8: LID}) == {PHONE8, LID}
    # a message under the @lid the deletion did not know yet
    assert marks_to_lift({PHONE8}, LID, {LID: PHONE8}, {}) == {PHONE8}
    assert marks_to_lift(deleted, GROUP, {}, {}) == {GROUP}
    assert marks_to_lift(deleted, "5533977770000@s.whatsapp.net", {}, {}) == set()
    assert marks_to_lift(set(), PHONE9, {}, {}) == set()


class Window:
    restore_deleted_chat = ChatActionsMixin.restore_deleted_chat

    def __init__(self, deleted):
        self._deleted_chats = set(deleted)
        self._lid_to_phone, self._phone_to_lid = {LID: PHONE8}, {PHONE8: LID}
        self.saved = []
        self.db = SimpleNamespace(set_metadata_json=lambda key, value: self.saved.append((key, sorted(value))))


def test_restoring_lifts_the_marks_and_persists_them():
    window = Window({PHONE8, LID, OTHER})
    assert window.restore_deleted_chat(PHONE9)
    assert window._deleted_chats == {OTHER}
    assert window.saved == [("deleted_chats", [OTHER])]
    assert not window.restore_deleted_chat(PHONE9)
    assert len(window.saved) == 1


def test_reopening_a_deleted_conversation_from_new_conversation_brings_it_back(monkeypatch):
    monkeypatch.setattr("ui.dialogs.new_conversation.wx.CallAfter", lambda *a, **k: None)
    window = Window({PHONE8, LID})
    window.chats, window.listed = {}, []
    window._normalize_jid = lambda jid: jid
    window.get_chat = lambda jid: None
    window._schedule_set_chats = lambda: window.listed.append(True)
    window.conversations_panel = SimpleNamespace(navigate_to_conversation=None, message_field=SimpleNamespace(SetFocus=None))
    dialog = SimpleNamespace(_mw=window, _find_existing_chat=NewConversationDialog._find_existing_chat)
    NewConversationDialog._finish_open(dialog, "Ana", PHONE8, {"remoteJid": PHONE8})
    assert window._deleted_chats == set()
    assert PHONE8 in window.chats and window.listed


def test_the_message_pipeline_restores_on_a_plain_window_and_logs_no_number(caplog):
    """on_new_message calls core.deleted_chats.restore() itself, so a window
    stub without the ChatActionsMixin method still works, and the log line
    names the person by a tag, never by the number."""
    import logging

    from core import deleted_chats

    window = SimpleNamespace(_deleted_chats={PHONE8, OTHER}, _lid_to_phone={}, _phone_to_lid={})
    with caplog.at_level(logging.INFO):
        assert deleted_chats.restore(window, PHONE9)
    assert window._deleted_chats == {OTHER}
    assert "5511999990000" not in caplog.text and "restored" in caplog.text
    assert not deleted_chats.restore(SimpleNamespace(_deleted_chats=set()), PHONE9)
