"""The Channels window comes back as it was left to open a channel.

Esc in a channel opened from the Channels window shows the window again
(channels.channel_closed). It used to come back empty, on My channels, the
search, category and list cleared and the directory forgotten: the window is
modal and was destroyed when the channel opened. `snapshot()` keeps what the
person had (the mode, the search text, the category and list, the channels
already read, the selected one) and `restore()` puts it back before the
window shows, so it opens where it was left, at once; the list of followed
channels is then read again quietly, as the window does on WhatsApp's events.

Tested in test_channels_return.py.
"""
from dataclasses import dataclass, field

#: channels.FIND (the directory): not imported, channels imports this module
FIND = 1


@dataclass(frozen=True)
class WindowState:
    mode: int
    query: str = ""
    category: int = 0
    view: int = 0
    mine: tuple = field(default_factory=tuple)
    found: tuple = field(default_factory=tuple)
    loaded: bool = False
    searched: bool = False
    selected: str = ""


def snapshot(dialog) -> WindowState:
    """What the Channels window shows right now."""
    channel = dialog._selected()
    return WindowState(
        mode=dialog._mode, query=dialog._search.GetValue(),
        category=dialog._category.GetSelection(), view=dialog._view.GetSelection(),
        mine=tuple(dialog._mine), found=tuple(dialog._found),
        loaded=dialog._loaded, searched=dialog._searched,
        selected=channel.jid if channel is not None else "")


def restore(dialog, state: WindowState) -> None:
    """Put *state* back on a Channels window not shown yet. ChangeValue and
    SetSelection fire no event: nothing is searched again."""
    dialog._mode = state.mode
    dialog._search.ChangeValue(state.query)
    if 0 <= state.category < dialog._category.GetCount():
        dialog._category.SetSelection(state.category)
    if 0 <= state.view < dialog._view.GetCount():
        dialog._view.SetSelection(state.view)
    dialog._mine, dialog._found = list(state.mine), list(state.found)
    dialog._loaded, dialog._searched = state.loaded, state.searched
    dialog._keep_first = state.selected
    dialog._restored = True
    dialog._sync_mode()
    dialog._show(dialog._found if state.mode == FIND else dialog._mine, state.selected)
