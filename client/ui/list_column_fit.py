"""Make the single column of a chat list follow the width of its control.

The chat lists (conversations, archived, locked) are one-column report lists.
The column was created with a fixed width (200/250/280 px) and never changed,
so a wide window left most of the control empty while each row's name and
preview were already cut with an ellipsis. The column now tracks the client
width, so the text is cut only when the control itself is too narrow.
"""

import wx


def column_width_for(client_width: int) -> int:
    """Width the column should have, or 0 when the control has no usable width yet."""
    return client_width if client_width > 0 else 0


def fit_single_column(list_ctrl) -> None:
    """Resize column 0 of ``list_ctrl`` to its current client width."""
    width = column_width_for(list_ctrl.GetClientSize().GetWidth())
    if width and list_ctrl.GetColumnWidth(0) != width:
        list_ctrl.SetColumnWidth(0, width)


def bind_single_column_fit(list_ctrl) -> None:
    """Keep column 0 as wide as the control, now and on every resize."""

    def on_size(event):
        event.Skip()
        # After the layout pass, so the client size already excludes a scroll bar.
        wx.CallAfter(_fit_if_alive, list_ctrl)

    list_ctrl.Bind(wx.EVT_SIZE, on_size)


def _fit_if_alive(list_ctrl) -> None:
    try:
        fit_single_column(list_ctrl)
    except RuntimeError:
        # The control was destroyed before the deferred call ran.
        pass
