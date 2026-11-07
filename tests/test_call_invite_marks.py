"""The marks of the "Add participants" dialog: sound, announcements and keys.

The mixin runs on a plain stub (no window); the dialog's wiring is pinned by
source inspection, as for the forward dialog.
"""
import inspect
import types

import wx

from ui.dialogs import call_invite
from ui.dialogs.call_invite_marks import CallInviteMarksMixin, row_labels

T = {"selected": "Selected", "unselected": "Unselected", "all_selected": "All selected",
     "all_unselected": "All unselected", "selection_mode_on": "Mode on",
     "selection_mode_off": "Mode off", "selected_suffix": "selected", "unknown_contact": "Unknown"}


class FakeList:
    def __init__(self, count):
        self.strings, self.sel = ["" for _ in range(count)], 0

    def Freeze(self): pass
    def Thaw(self): pass
    def GetSelection(self): return self.sel
    def SetSelection(self, i): self.sel = i
    def SetString(self, i, text): self.strings[i] = text


class Stub(CallInviteMarksMixin):
    def __init__(self, rows, settings=None):
        self._rows = rows
        self._list = FakeList(len(rows))
        self.spoken, self.sounds = [], 0
        self._mw = types.SimpleNamespace(
            i18n=types.SimpleNamespace(t=T.__getitem__),
            settings={"user_interface": settings or {}},
            output=lambda text, interrupt=False: self.spoken.append((text, interrupt)))
        self._marks, self._mark_sound = set(), types.SimpleNamespace(play=self._play)
        self._labels = self._invite_labels()

    def _play(self):
        self.sounds += 1


ROWS = [("1@lid", "Ana"), ("2@lid", "Bia"), ("3@lid", "")]


def test_row_labels_never_show_a_jid_and_mark_the_selected():
    assert row_labels(ROWS, {"2@lid"}, "Unknown", "selected", "end") == ["Ana", "Bia selected", "Unknown"]
    assert row_labels(ROWS, {"2@lid"}, "Unknown", "selected", "start")[1] == "selected Bia"


def test_ctrl_space_marks_with_sound_and_announces_the_mode():
    s = Stub(ROWS)
    assert s._handle_invite_mark_key(wx.WXK_SPACE, True, False)
    assert s._marks == {"1@lid"} and s.sounds == 1
    assert s.spoken == [("Selected. Mode on", True)]
    assert s._list.strings[0] == "Ana selected"
    s._handle_invite_mark_key(wx.WXK_SPACE, True, False)
    assert s._marks == set() and s.sounds == 1
    assert s.spoken[-1] == ("Unselected. Mode off", True)


def test_plain_space_only_marks_once_something_is_marked():
    s = Stub(ROWS)
    assert not s._handle_invite_mark_key(wx.WXK_SPACE, False, False)
    s._handle_invite_mark_key(wx.WXK_SPACE, True, False)
    s._list.sel = 1
    assert s._handle_invite_mark_key(wx.WXK_SPACE, False, False)
    assert s._marks == {"1@lid", "2@lid"}
    assert s.spoken[-1] == ("Selected", True)


def test_plain_space_stays_native_when_the_setting_is_off():
    s = Stub(ROWS, {"space_selects_in_selection_mode": False})
    s._marks.add("1@lid")
    assert not s._handle_invite_mark_key(wx.WXK_SPACE, False, False)


def test_ctrl_shift_space_marks_everyone_then_unmarks_them():
    s = Stub(ROWS)
    s._handle_invite_mark_key(wx.WXK_SPACE, True, True)
    assert s._marks == {"1@lid", "2@lid", "3@lid"} and s.spoken[-1][0] == "All selected. Mode on"
    s._handle_invite_mark_key(wx.WXK_SPACE, True, True)
    assert s._marks == set() and s.spoken[-1][0] == "All unselected. Mode off"


def test_shift_arrows_and_home_end_mark_and_move():
    s = Stub(ROWS)
    s._handle_invite_mark_key(wx.WXK_DOWN, False, True)
    assert s._marks == {"2@lid"} and s._list.sel == 1
    s._handle_invite_mark_key(wx.WXK_END, False, True)
    assert s._marks == {"2@lid", "3@lid"} and s._list.sel == 2
    s._handle_invite_mark_key(wx.WXK_HOME, False, True)
    assert s._marks == {"1@lid", "2@lid", "3@lid"} and s._list.sel == 0


def test_clear_marks_is_a_noop_when_nothing_is_marked():
    s = Stub(ROWS)
    assert not s._clear_invite_marks()
    s._marks.add("1@lid")
    assert s._clear_invite_marks() and s.spoken[-1][0] == "All unselected. Mode off"


def test_the_dialog_uses_the_marks_and_not_a_checklistbox():
    source = inspect.getsource(call_invite)
    assert "CheckListBox" not in source and "GetCheckedItems" not in source
    assert issubclass(call_invite.CallInviteDialog, CallInviteMarksMixin)
    assert "_init_invite_marks()" in inspect.getsource(call_invite.CallInviteDialog.__init__)


class Mw:
    def __init__(self):
        self.i18n = types.SimpleNamespace(t=lambda key: "Could not add: {reason}")
        self.spoken = []

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def _call_error_text(self, error):
        return "HTTP 500"


def finish(spoken, error, invited, alive=True):
    d = types.SimpleNamespace(_mw=Mw(), _alive=alive, _busy=True, closed=0,
                              _status=types.SimpleNamespace(SetLabel=lambda t: None))
    d._close = lambda: setattr(d, "closed", d.closed + 1)
    call_invite.CallInviteDialog._finish_add(d, spoken, error, invited)
    return d


def test_a_failed_request_is_spoken_with_its_reason_and_keeps_the_dialog():
    d = finish(None, RuntimeError("HTTP 500"), False)
    assert d._mw.spoken == ["Could not add: HTTP 500"] and d.closed == 0 and not d._busy


def test_nobody_invited_keeps_the_dialog_open_and_speaks_who_failed():
    d = finish("Could not add: Ana.", None, False)
    assert d._mw.spoken == ["Could not add: Ana."] and d.closed == 0
