"""A settings page taller than its window keeps its groups readable.

Reported live, Settings > User interface: tabbing into "Posição para anunciar
itens selecionados", NVDA read the radio button with no group name, and read
the group name on the NEXT control instead, the "show yesterday" checkbox.

NVDA decides by geometry which group box a control belongs to: the nearest
earlier group box whose rectangle contains the control's. The page holds more
options than a screen is tall, and on a plain panel the sizer then squeezes
whatever is past the bottom edge down to a height of zero: the box collapses,
its radio buttons end up outside it, and the control after the group lands on
the collapsed box's corner, "inside" it.

The page is a ScrolledPanel now, so nothing is squeezed. These tests build the
same arrangement (options, a group box with sibling radio buttons, the control
after it) on an off-screen hidden_frame() and compare rectangles the way NVDA
does. The dialog itself is checked in test_settings_checkboxes_roundtrip.py,
which builds the real one and therefore only runs on CI.
"""

import pytest
import wx
from wx.lib.scrolledpanel import ScrolledPanel

from tests.conftest import destroy_now, hidden_frame


def group_box_of(control, boxes):
    """The group box NVDA would report for *control*: the one whose screen
    rectangle contains the control's, or None."""
    rect = control.GetScreenRect()
    for box in boxes:
        outer = box.GetScreenRect()
        if (rect.x >= outer.x and rect.y >= outer.y
                and rect.x + rect.width <= outer.x + outer.width
                and rect.y + rect.height <= outer.y + outer.height):
            return box
    return None


def _page(parent, make_panel, options_before=30):
    """More options than fit, then the group, then the control after it."""
    panel = make_panel(parent)
    sizer = wx.BoxSizer(wx.VERTICAL)
    for index in range(options_before):
        sizer.Add(wx.CheckBox(panel, label=f"option {index}"), 0, wx.ALL, 8)
    box = wx.StaticBox(panel, label="Position to announce selected items:")
    box_sizer = wx.StaticBoxSizer(box, wx.VERTICAL)
    radios = [wx.RadioButton(panel, label="At the start", style=wx.RB_GROUP),
              wx.RadioButton(panel, label="At the end of the message or chat")]
    for radio in radios:
        box_sizer.Add(radio, 0, wx.LEFT | wx.TOP, 5)
    sizer.Add(box_sizer, 0, wx.EXPAND | wx.ALL, 8)
    after = wx.CheckBox(panel, label="Show yesterday")
    sizer.Add(after, 0, wx.ALL, 8)
    panel.SetSizer(sizer)
    return panel, box, radios, after


def _scrolled(parent):
    return ScrolledPanel(parent)


@pytest.fixture
def frame(wx_app):
    window = hidden_frame()
    yield window
    destroy_now(window)


def test_a_scrollable_page_keeps_the_group_around_its_radio_buttons(frame):
    panel, box, radios, after = _page(frame, _scrolled)
    panel.SetupScrolling(scroll_x=False, rate_y=15, scrollIntoView=True)
    assert panel.GetSizer().GetMinSize().height > 500      # it really does not fit
    panel.SetSize((600, 500))
    panel.Layout()
    panel.FitInside()

    assert all(group_box_of(radio, [box]) is box for radio in radios)
    assert group_box_of(after, [box]) is None
    assert all(control.GetSize().height > 0 for control in (box, after, *radios))


def test_which_is_what_a_plain_panel_got_wrong(frame):
    """The failure itself, so the reason for the scrolled page stays provable:
    if wx stops squeezing, this fails and the comment in settings_dialog.py is
    out of date."""
    panel, box, radios, after = _page(frame, wx.Panel)
    panel.SetSize((600, 500))
    panel.Layout()

    assert not any(group_box_of(radio, [box]) is box for radio in radios)
    assert group_box_of(after, [box]) is box


def test_a_page_that_fits_was_never_the_problem(frame):
    panel, box, radios, after = _page(frame, wx.Panel, options_before=2)
    panel.SetSize((600, 500))
    panel.Layout()
    assert all(group_box_of(radio, [box]) is box for radio in radios)
    assert group_box_of(after, [box]) is None
