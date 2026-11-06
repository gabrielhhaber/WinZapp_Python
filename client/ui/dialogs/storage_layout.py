"""Reflow explanatory native text when a resizable storage view narrows."""
import wx


def note(parent, label):
    control = wx.StaticText(parent, label=label)
    control.Wrap(480)
    last_width = None
    def resized(event):
        nonlocal last_width
        width = max(200, parent.GetClientSize().width - 28)
        if width != last_width:
            last_width = width
            control.SetLabel(label)
            control.Wrap(width)
            parent.Layout()
        event.Skip()
    parent.Bind(wx.EVT_SIZE, resized)
    return control
