"""Multi-character type-ahead search for read-only wx.ComboBox controls.

wx.ComboBox(style=wx.CB_READONLY) already jumps to an item on a single
keystroke (native Win32 CBS_DROPDOWNLIST behavior — same as the country and
language ComboBoxes already relied on before this module existed), but a
SECOND letter typed shortly after the first restarts the search from
scratch instead of narrowing it: pressing "B" then "A" quickly lands on the
first entry starting with "A" (treating the "A" as a brand new search), not
on the first entry starting with "Ba". Reported live against the pairing
dialog's country selector.

WinZapp's own conversation/message/contact lists don't have this problem
and need no code of their own for it: the main conversation list
(ui/conversations.py's self.conversations_list, a wx.ListCtrl/SysListView32)
and the "forward message" contact picker (a wx.ListBox — see
ui/accessible.py's CompatListBoxMessagesCtrl docstring) both fall through to
plain event.Skip() for an unmodified letter keystroke, because a native
Win32 SysListView32 or LISTBOX genuinely does accumulate consecutive
keystrokes into one search string on Windows. wx.ComboBox's dropdown list,
despite being backed by a similar native control, does not do this: it
re-searches from scratch on every keystroke — which is the ONE place in
this codebase that needs the accumulation reimplemented by hand, here, and
nowhere else.

bind_incremental_search() reimplements the accumulation by hand: an
EVT_CHAR handler buffers recent keystrokes (resetting after a short pause,
exactly like a real incremental search), and an EVT_COMBOBOX handler lets
the control's own (single-keystroke-only) native jump happen and then
immediately corrects it — via SetSelection() — to whichever entry actually
matches the accumulated buffer, before notifying *on_select*. Correcting
after the fact, rather than trying to keep the native jump from happening
in the first place, sidesteps a real trap: wx invokes every handler bound
to the same event on the same control regardless of Skip() (Skip() only
controls propagation to *parent* windows) — so a second, separately-bound
EVT_COMBOBOX handler would see the "wrong" intermediate native jump before
this one gets a chance to correct it. Route your own reaction to a
selection change through *on_select* instead of binding wx.EVT_COMBOBOX
directly on a control this is attached to, so it only ever fires once, with
the final, already-corrected selection.
"""

import time
import wx

from core.utils import normalize_for_search

# How long a pause between keystrokes is allowed before the search buffer
# resets to just the new key, instead of extending the previous search.
# Windows' own native list/combo incremental search doesn't expose its
# timeout to applications; this is the same rough order of magnitude.
SEARCH_TIMEOUT_SECONDS = 1.0


def find_incremental_match(choices, prefix: str):
    """Index of the first entry in *choices* (an iterable of strings) whose
    text starts with *prefix* (case-insensitive, diacritics ignored), or
    None if *prefix* is empty or nothing matches.

    Pure — no wx dependency — so it's exercised directly in tests."""
    if not prefix:
        return None
    needle = normalize_for_search(prefix, mode="nfkd")
    for idx, text in enumerate(choices):
        if normalize_for_search(text, mode="nfkd").startswith(needle):
            return idx
    return None


def next_search_buffer(
    buffer: str, char: str, elapsed: float, timeout: float = SEARCH_TIMEOUT_SECONDS
) -> str:
    """The new search buffer after *char* is typed *elapsed* seconds after
    the previous keystroke on the same control: extends *buffer* when
    *elapsed* is within *timeout*, otherwise starts over with just *char*.

    Pure — exercised directly in tests."""
    return (buffer + char) if elapsed <= timeout else char


def bind_incremental_search(combo: wx.ComboBox, on_select=None) -> None:
    """Attach multi-character type-ahead search to *combo*.

    *combo* must be style=wx.CB_READONLY with its choices already set.
    Consecutive letters typed within SEARCH_TIMEOUT_SECONDS of each other
    accumulate into one search string instead of each restarting the
    search from scratch, e.g. "B" then "A" lands on the first entry
    starting with "Ba" rather than jumping to the first "B" and then the
    first "A" independently.

    *on_select*, if given, is called with the new selection index every
    time a selection change settles — whether from typing (single- or
    multi-character), the arrow keys, or a mouse click. This is the ONLY
    reliable way to react to a selection change made through this function:
    see the module docstring for why binding your own wx.EVT_COMBOBOX
    handler directly on *combo* alongside this one would see an
    uncorrected, momentarily-wrong selection first.
    """
    state = {"buffer": "", "last_time": 0.0, "keystroke_pending": False}

    def _notify(idx):
        if on_select is not None:
            on_select(idx)

    def _on_char(event):
        # Always let native handling proceed — correction happens in
        # _on_combobox() once the (possibly wrong) native jump has
        # actually landed, not by trying to prevent it here.
        event.Skip()

        if event.ControlDown() or event.AltDown() or event.CmdDown():
            return
        key = event.GetUnicodeKey()
        char = chr(key) if key != wx.WXK_NONE else ""
        if not char.isalpha():
            return

        now = time.monotonic()
        elapsed = now - state["last_time"]
        state["last_time"] = now
        state["buffer"] = next_search_buffer(state["buffer"], char, elapsed)
        state["keystroke_pending"] = True

    def _on_combobox(event):
        event.Skip()

        if not state.pop("keystroke_pending", False):
            # A mouse click or arrow-key navigation, not a typed search —
            # nothing to correct, just relay the (already correct) result.
            _notify(combo.GetSelection())
            return

        buffer = state["buffer"]
        if len(buffer) > 1:
            choices = [combo.GetString(i) for i in range(combo.GetCount())]
            idx = find_incremental_match(choices, buffer)
            if idx is None:
                # The accumulated buffer no longer matches anything — fall
                # back to just the latest keystroke, same as native
                # list/combo incremental search does when a typed sequence
                # stops matching.
                buffer = buffer[-1]
                state["buffer"] = buffer
                idx = find_incremental_match(choices, buffer)
            if idx is not None and idx != combo.GetSelection():
                combo.SetSelection(idx)

        _notify(combo.GetSelection())

    combo.Bind(wx.EVT_CHAR, _on_char)
    combo.Bind(wx.EVT_COMBOBOX, _on_combobox)
