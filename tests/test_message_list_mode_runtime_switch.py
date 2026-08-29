"""Runtime switching between the two persistent messages-list controls.

Both native controls are created at panel startup. Settings only changes which
one is active, so a later conversation open cannot inherit a destroyed/replaced
wx control from the settings dialog.
"""

import pytest

wx = pytest.importorskip("wx")
from ui.conversations import ConversationsPanel


class _List:
    def __init__(self, focused=-1):
        self.focused = focused
        self.rows = []
        self.shown = True
        self.visible = -1
        self.selected = -1
        self.focus_calls = 0

    def GetFocusedItem(self):
        return self.focused

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def DeleteAllItems(self):
        self.rows.clear()

    def Append(self, entry):
        self.rows.append(entry[0])

    def Hide(self):
        self.shown = False

    def Show(self):
        self.shown = True

    def Focus(self, row):
        self.focused = row

    def Select(self, row):
        self.selected = row

    def EnsureVisible(self, row):
        self.visible = row

    def SetFocus(self):
        self.focus_calls += 1


class _Panel:
    def __init__(self):
        self.layouts = 0

    def Layout(self):
        self.layouts += 1


class _ReadMore:
    def __init__(self):
        self.hidden = False

    def Hide(self):
        self.hidden = True


class _Stub:
    apply_message_list_mode = ConversationsPanel.apply_message_list_mode

    def __init__(self):
        classic = _List(focused=1)
        listbox = _List()
        listbox.shown = False
        self._message_list_controls = {"classic": classic, "listbox": listbox}
        self._message_list_mode = "classic"
        self.messages_list = classic
        self._sorted_messages = ["first", "second", "third"]
        self.conversation = {"remoteJid": "chat@c.us"}
        self.conversation_panel = _Panel()
        self._read_more_btn = _ReadMore()
        self._read_more_remainder = "tail"
        self.rerendered = 0

    def _render_message_line(self, msg, index=None, total=None):
        return f"{msg}:{index + 1}/{total}"

    def _rerender_messages_list_rows(self):
        self.rerendered += 1

    def _update_read_more_button(self, index):
        pass


def test_switch_uses_persistent_control_and_keeps_current_row(monkeypatch):
    stub = _Stub()
    monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: None))
    classic = stub._message_list_controls["classic"]
    listbox = stub._message_list_controls["listbox"]

    stub.apply_message_list_mode("listbox")

    assert stub.messages_list is listbox
    assert stub._message_list_mode == "listbox"
    assert classic.shown is False
    assert listbox.shown is True
    assert listbox.rows == ["first:1/3", "second:2/3", "third:3/3"]
    assert listbox.focused == 1
    assert listbox.selected == 1
    assert listbox.visible == 1
    assert stub._read_more_btn.hidden is True
    assert stub._read_more_remainder == ""


def test_switch_while_no_conversation_does_not_copy_stale_rows(monkeypatch):
    stub = _Stub()
    monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: None))
    stub.conversation = None

    stub.apply_message_list_mode("listbox")

    assert stub.messages_list.rows == []


def test_switching_back_reuses_original_control(monkeypatch):
    stub = _Stub()
    monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: None))
    original = stub.messages_list

    stub.apply_message_list_mode("listbox")
    stub.apply_message_list_mode("classic")

    assert stub.messages_list is original
    assert original.shown is True
    assert stub._message_list_controls["listbox"].shown is False


def test_same_mode_only_rerenders_rows_in_place(monkeypatch):
    stub = _Stub()
    monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: None))

    stub.apply_message_list_mode("classic")

    assert stub.rerendered == 1
