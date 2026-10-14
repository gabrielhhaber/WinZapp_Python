"""A control with the label a screen reader reads for it.

Windows names an unlabelled control (a list, an edit field) after the static
text created **just before** it, in creation order, not after the text above
it on screen. A helper that takes an already built control,

    labelled("key", wx.ListBox(self))

creates the list first (Python evaluates the argument before the call) and
the label after it, so every label lands on the next control: in the catalog
window the products list had no name, the empty cart list was read as
"Catalog products (Alt+P)" and the note field as the cart. `make` is called
here, after the label, which is the only order that works.
"""

import wx

from ui.accessible import split_mnemonic


def add_labelled(parent, sizer, label, make, proportion=0):
    """Add `label`, then the control `make()` builds, to `sizer`; return the
    control. `make` is a callable (a lambda), never a control."""
    if isinstance(make, wx.Window):
        raise TypeError("pass a function that creates the control, so the label comes first")
    sizer.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
    control = make()
    control.SetName(split_mnemonic(label)[0])
    sizer.Add(control, proportion, wx.EXPAND | wx.ALL, 8)
    return control
