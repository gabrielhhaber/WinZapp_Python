"""Alt+Up / Alt+Down on a list whose order the user sets.

The AI provider list and the options of a new poll have Move up / Move down
buttons; a list with no arrow-key way of moving an item needs one too, or a
keyboard user must leave the list for the button each time. One preference
(scope ``reorder`` of the shortcut catalog) serves every such list, so a key
changed in Settings > Shortcuts changes all of them.
"""
import wx

from ui.shortcut_bindings import matches

MOVE_UP = 'reorder.move_up'
MOVE_DOWN = 'reorder.move_down'


def reorder_direction(owner, event):
    """-1 to move the selected item up, 1 to move it down, 0 for any other key."""
    if matches(owner, event, MOVE_UP):
        return -1
    if matches(owner, event, MOVE_DOWN):
        return 1
    return 0


def bind_reorder_keys(control, move):
    """Call ``move(direction)`` when the configured keys are pressed on *control*;
    every other key keeps its native job (arrows still walk the list)."""
    def on_key(event):
        direction = reorder_direction(control, event)
        if direction:
            move(direction)
            return
        event.Skip()
    control.Bind(wx.EVT_KEY_DOWN, on_key)
