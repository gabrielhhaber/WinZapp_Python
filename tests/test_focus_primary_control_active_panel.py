"""Tests for MainWindow._focus_primary_control()'s panel-visibility fix.

The global hotkey's restore_window() always called _focus_primary_control(),
which used to check conversations_list.IsShown() before focusing it. IsShown()
only reflects a widget's own last explicit Show()/Hide() call, not whether an
ancestor is currently hidden — tab-switching (Alt+1/2/4) hides the PANEL
container (conversations_panel.Hide(), status_panel.Hide(), ...) but never
the list widgets inside it, so conversations_list kept reporting
IsShown()==True forever after the last time it was shown. Restoring the
window from tray while Status (or Archived) was the active tab therefore
always yanked NVDA focus back onto the invisible conversations list.
_focus_primary_control() now checks IsShownOnScreen(), which walks the whole
ancestor chain, and picks whichever panel's list is genuinely on screen.

MainWindow is a wx.Frame and cannot be instantiated without a running app, so
the method under test is bound onto a plain stub carrying only the attributes
it touches.
"""

from unittest.mock import Mock

from main import MainWindow


class _Stub:
    """Minimal stand-in for MainWindow for _focus_primary_control()."""

    def __init__(self):
        self.conversations_panel = Mock()
        self.archived_conversations_panel = Mock()
        self.status_panel = Mock()

    _focus_primary_control = MainWindow._focus_primary_control


def _set_visible(stub, *, conversations=False, archived=False, status=False):
    stub.conversations_panel.conversations_list.IsShownOnScreen.return_value = conversations
    stub.archived_conversations_panel.conversations_list.IsShownOnScreen.return_value = archived
    stub.status_panel._status_list.IsShownOnScreen.return_value = status


def test_focuses_conversations_list_when_conversations_panel_is_visible():
    stub = _Stub()
    _set_visible(stub, conversations=True)

    stub._focus_primary_control()

    stub.conversations_panel.conversations_list.SetFocus.assert_called_once()
    stub.archived_conversations_panel.conversations_list.SetFocus.assert_not_called()
    stub.status_panel._status_list.SetFocus.assert_not_called()


def test_focuses_status_list_when_status_panel_is_the_one_visible():
    """The bug itself: restoring the window while Status is the active tab
    must focus the status list, not the (invisible) conversations list."""
    stub = _Stub()
    _set_visible(stub, status=True)

    stub._focus_primary_control()

    stub.status_panel._status_list.SetFocus.assert_called_once()
    stub.conversations_panel.conversations_list.SetFocus.assert_not_called()


def test_focuses_archived_list_when_archived_panel_is_the_one_visible():
    stub = _Stub()
    _set_visible(stub, archived=True)

    stub._focus_primary_control()

    stub.archived_conversations_panel.conversations_list.SetFocus.assert_called_once()
    stub.conversations_panel.conversations_list.SetFocus.assert_not_called()
    stub.status_panel._status_list.SetFocus.assert_not_called()


def test_no_panel_visible_does_not_raise_or_focus_anything():
    stub = _Stub()
    _set_visible(stub)

    stub._focus_primary_control()  # must not raise

    stub.conversations_panel.conversations_list.SetFocus.assert_not_called()
    stub.archived_conversations_panel.conversations_list.SetFocus.assert_not_called()
    stub.status_panel._status_list.SetFocus.assert_not_called()
