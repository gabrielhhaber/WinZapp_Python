"""Builds the real CheckboxConfirmDialog and checks what a screen-reader user
depends on — the parts the fake in test_checkbox_confirm_dialog.py cannot see.

The dialog replaced wx.RichMessageDialog.ShowCheckBox(), whose TaskDialog
checkbox NVDA read as "not checked, read only". So: a real, enabled
wx.CheckBox; Tab order text -> checkbox -> Yes -> No; Esc answers No; Enter
answers whichever button the caller made the default.
"""

import pytest
import wx

from tests.conftest import hidden_frame
from ui.dialogs.checkbox_confirm import CheckboxConfirmDialog

# Creates a REAL top-level wx dialog - see the wxgui marker in pytest.ini.
pytestmark = pytest.mark.wxgui


def _build(**kw):
    frame = hidden_frame()
    dlg = CheckboxConfirmDialog(frame, "msg", "title", "box", "&Sim", "&Não", **kw)
    return frame, dlg


def test_controls_are_plain_and_in_tab_order(wx_app):
    frame, dlg = _build(checked=True, default_yes=True)
    try:
        children = list(dlg.GetChildren())
        assert isinstance(children[0], wx.StaticText)
        assert isinstance(children[1], wx.CheckBox)
        assert isinstance(children[2], wx.Button) and children[2].GetId() == wx.ID_YES
        assert isinstance(children[3], wx.Button) and children[3].GetId() == wx.ID_NO
        assert children[1].IsEnabled()
    finally:
        dlg.Destroy()
        frame.Destroy()


@pytest.mark.parametrize("checked", [True, False])
def test_the_checkbox_starts_as_asked(wx_app, checked):
    frame, dlg = _build(checked=checked, default_yes=True)
    try:
        assert dlg.is_checked() is checked
    finally:
        dlg.Destroy()
        frame.Destroy()


@pytest.mark.parametrize("default_yes, expected", [(True, wx.ID_YES), (False, wx.ID_NO)])
def test_escape_answers_no_and_enter_answers_the_chosen_default(wx_app, default_yes, expected):
    frame, dlg = _build(checked=False, default_yes=default_yes)
    try:
        assert dlg.GetEscapeId() == wx.ID_NO
        assert dlg.GetDefaultItem().GetId() == expected
    finally:
        dlg.Destroy()
        frame.Destroy()
