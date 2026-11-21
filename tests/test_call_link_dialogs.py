"""The call-link windows' logic, called on plain stubs (no window is built):
who a link can be sent to, whether a send worked, and the waiting-room list
keeping its selection."""
import types

import pytest

from ui import call_link_dialog
from ui.call_waiting_room_dialog import CallWaitingRoomDialog
from ui.conversation_panel.forwarding import ForwardingMixin


@pytest.mark.parametrize("result, ok", [
    ("3EB0ABC", True), ("", False), (None, False),
    ({"ok": True}, True), ({"ok": False}, False), ({"id": "x"}, False),
])
def test_a_send_worked_when_it_answers_an_id_or_ok(result, ok):
    assert call_link_dialog.sent_ok(result) is ok


def test_a_link_is_offered_to_conversations_never_to_broadcasts_or_channels(monkeypatch):
    chats = [{"remoteJid": "1@s.whatsapp.net"}, {"remoteJid": "status@broadcast"},
             {"remoteJid": "9@newsletter"}, {"remoteJid": ""}, {"remoteJid": "2@g.us"}]
    names = ["Ana", "Status", "Canal", "Vazio", "Grupo"]
    monkeypatch.setattr(ForwardingMixin, "_forward_target_chats", lambda mw: (chats, names))
    rows, labels = call_link_dialog.send_targets(object())
    assert [c["remoteJid"] for c in rows] == ["1@s.whatsapp.net", "2@g.us"]
    assert labels == ["Ana", "Grupo"]


class ListBox:
    def __init__(self):
        self.labels, self.selection = [], -1

    def Freeze(self): pass
    def Thaw(self): pass
    def __bool__(self): return True
    def Set(self, labels): self.labels = list(labels)
    def SetSelection(self, index): self.selection = index
    def GetSelection(self): return self.selection


def room():
    stub = types.SimpleNamespace(
        main_window=types.SimpleNamespace(i18n=types.SimpleNamespace(t=lambda key: key)),
        _people=[], people_list=ListBox(), synced=0)
    stub._selected = lambda: CallWaitingRoomDialog._selected(stub)
    stub._sync_buttons = lambda: setattr(stub, "synced", stub.synced + 1)
    return stub


def test_the_waiting_list_keeps_the_selected_person_when_someone_arrives_before_them():
    stub = room()
    ana = {"jid": "1@lid", "name": "Ana", "guest": False}
    bia = {"jid": "2@lid", "name": "Bia", "guest": True}
    caio = {"jid": "3@lid", "name": "", "guest": True}
    CallWaitingRoomDialog.set_people(stub, [ana, bia])
    stub.people_list.selection = 1                        # Bia
    CallWaitingRoomDialog.set_people(stub, [caio, ana, bia])
    assert stub.people_list.labels == ["call_link_unnamed_guest", "Ana", "Bia"]
    assert stub.people_list.selection == 2                # still Bia
    synced = stub.synced
    CallWaitingRoomDialog.set_people(stub, [caio, ana, bia])
    assert stub.synced == synced                          # an identical room is left alone
    CallWaitingRoomDialog.set_people(stub, [])
    assert stub.people_list.labels == ["call_link_nobody_waiting"]
