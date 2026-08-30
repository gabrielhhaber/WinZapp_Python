"""Tests for core.combo_search.

Regression coverage: wx.ComboBox(style=wx.CB_READONLY) jumps to an item on
a single keystroke natively, but a second letter typed shortly after the
first used to restart the search from scratch instead of narrowing it —
pressing "B" then "A" quickly in a list containing "Blindography" and
"Bane" landed on the first entry starting with "A" (e.g. "Ana"), not on
"Bane". Reported live (twice): first against the pairing dialog's country
selector, then again after an initial fix attempt that tried to prevent the
native jump from happening at all — TestBindIncrementalSearch below covers
the actual bug in THAT attempt (confirmed live: wx invokes every handler
bound to the same wx.EVT_COMBOBOX on the same control regardless of
Skip(), and wx.ComboBox.SetSelection() does not itself fire
wx.EVT_COMBOBOX — see combo_search.py's module docstring for the
"let the native jump happen, then correct it" design this led to).

find_incremental_match() is the "does this buffer match anything" half,
next_search_buffer() is the "should this keystroke extend the buffer or
start over" half. TestBindIncrementalSearch drives the real wx wiring with
synthetic events (needs a real wx.ComboBox — see the wx_app fixture) to
pin the actual reported bug, not just the two pure halves in isolation.
"""

import wx

from tests.conftest import hidden_frame
from core.combo_search import (
    bind_incremental_search,
    find_incremental_match,
    next_search_buffer,
    SEARCH_TIMEOUT_SECONDS,
)


class TestFindIncrementalMatch:
    CHOICES = ["Bahamas", "Bolivia", "Botswana", "Croatia", "Oman"]

    def test_matches_by_prefix(self):
        assert find_incremental_match(self.CHOICES, "Bo") == 1  # "Bolivia"

    def test_case_insensitive(self):
        assert find_incremental_match(self.CHOICES, "bo") == 1
        assert find_incremental_match(self.CHOICES, "BO") == 1

    def test_single_letter_matches_first_entry_with_that_letter(self):
        assert find_incremental_match(self.CHOICES, "B") == 0  # "Bahamas"
        assert find_incremental_match(self.CHOICES, "O") == 4  # "Oman"

    def test_diacritics_are_ignored(self):
        choices = ["Áustria", "Austrália"]
        assert find_incremental_match(choices, "au") == 0  # "Áustria" folds to "austria"

    def test_no_match_returns_none(self):
        assert find_incremental_match(self.CHOICES, "Zz") is None

    def test_empty_prefix_returns_none(self):
        assert find_incremental_match(self.CHOICES, "") is None

    def test_empty_choices_returns_none(self):
        assert find_incremental_match([], "B") is None


class TestNextSearchBuffer:
    def test_within_timeout_extends_the_buffer(self):
        assert next_search_buffer("B", "o", elapsed=0.3) == "Bo"

    def test_at_exactly_the_timeout_still_extends(self):
        assert next_search_buffer("B", "o", elapsed=SEARCH_TIMEOUT_SECONDS) == "Bo"

    def test_past_the_timeout_starts_over(self):
        assert next_search_buffer("B", "o", elapsed=SEARCH_TIMEOUT_SECONDS + 0.01) == "o"

    def test_first_keystroke_ever_with_huge_elapsed_starts_fresh(self):
        assert next_search_buffer("", "b", elapsed=1e6) == "b"

    def test_custom_timeout_is_honored(self):
        assert next_search_buffer("B", "o", elapsed=2.0, timeout=3.0) == "Bo"
        assert next_search_buffer("B", "o", elapsed=2.0, timeout=1.0) == "o"


class TestBAndOScenarioEndToEnd:
    """The exact scenario reported: type "B" then "O" quickly."""

    def test_b_then_o_lands_on_first_bo_entry_not_first_o_entry(self):
        choices = ["Bahamas", "Bolivia", "Botswana", "Croatia", "Oman"]
        buffer = next_search_buffer("", "B", elapsed=1e6)
        idx = find_incremental_match(choices, buffer)
        assert choices[idx] == "Bahamas"  # first keystroke: native single-letter jump

        buffer = next_search_buffer(buffer, "o", elapsed=0.2)
        idx = find_incremental_match(choices, buffer)
        assert choices[idx] == "Bolivia"  # accumulated "Bo": narrows, doesn't restart


class TestBindIncrementalSearch:
    """Drives the real wx.EVT_CHAR / wx.EVT_COMBOBOX wiring with synthetic
    events against a real wx.ComboBox, simulating what the native control's
    own (buggy, single-character-only) type-ahead does on each keystroke —
    a fresh single-letter jump, ignoring anything typed before it — the
    same way the real Win32 control does. bind_incremental_search() must
    correct that native result using the accumulated buffer."""

    CHOICES = ["Croatia", "Blindography", "Ana", "Bane", "Bojan"]

    def _make_combo(self, wx_app):
        frame = hidden_frame()
        combo = wx.ComboBox(frame, style=wx.CB_READONLY, choices=self.CHOICES)
        return frame, combo

    def _type_char(self, combo, char):
        """A keystroke landing on *combo*: fires EVT_CHAR (as the real
        control would), then simulates the native control's own
        single-character-only jump by selecting the first entry starting
        with just *char* and firing EVT_COMBOBOX — exactly what the real
        native jump does, right or wrong, on real Windows."""
        char_event = wx.KeyEvent(wx.EVT_CHAR.typeId)
        char_event.SetEventObject(combo)
        char_event.SetUnicodeKey(ord(char))
        combo.GetEventHandler().ProcessEvent(char_event)

        naive_idx = find_incremental_match(self.CHOICES, char)
        if naive_idx is not None:
            combo.SetSelection(naive_idx)
            combo_event = wx.CommandEvent(wx.EVT_COMBOBOX.typeId, combo.GetId())
            combo_event.SetEventObject(combo)
            combo_event.SetInt(naive_idx)
            combo.GetEventHandler().ProcessEvent(combo_event)

    def test_b_then_a_lands_on_bane_not_on_the_native_single_char_jump_to_ana(self, wx_app):
        frame, combo = self._make_combo(wx_app)
        try:
            notifications = []
            bind_incremental_search(combo, on_select=lambda idx: notifications.append(idx))

            self._type_char(combo, "B")
            assert combo.GetString(combo.GetSelection()) == "Blindography"

            self._type_char(combo, "A")
            # Native's own naive jump alone would have landed on "Ana" (the
            # first entry starting with just "A") — this is the exact bug
            # reported. The correction must override it to "Bane" ("Ba…").
            assert combo.GetString(combo.GetSelection()) == "Bane"
            assert notifications == [1, 3]  # Blindography, then corrected to Bane
        finally:
            frame.Destroy()

    def test_on_select_is_not_called_twice_for_one_corrected_keystroke(self, wx_app):
        """Regression for the first fix attempt: binding wx.EVT_COMBOBOX
        directly (in addition to this function's own internal binding)
        made a caller's handler see the wrong, uncorrected native jump
        first — wx calls every handler bound to the same event on the same
        control, regardless of Skip(). on_select must be the only channel,
        and must fire exactly once per settled (corrected) keystroke."""
        frame, combo = self._make_combo(wx_app)
        try:
            call_count = {"n": 0}
            bind_incremental_search(combo, on_select=lambda idx: call_count.update(n=call_count["n"] + 1))

            self._type_char(combo, "B")
            self._type_char(combo, "A")

            assert call_count["n"] == 2  # one per keystroke, not one per internal correction
        finally:
            frame.Destroy()

    def test_mouse_or_arrow_driven_selection_is_relayed_unmodified(self, wx_app):
        """A selection change with no preceding keystroke (mouse click,
        arrow key) must be passed through as-is, never "corrected" using a
        stale search buffer from an earlier, unrelated keystroke."""
        frame, combo = self._make_combo(wx_app)
        try:
            notifications = []
            bind_incremental_search(combo, on_select=lambda idx: notifications.append(idx))

            self._type_char(combo, "B")
            self._type_char(combo, "A")  # leaves a "BA" buffer behind
            notifications.clear()

            # Simulate a plain mouse click on "Croatia" (index 0) — no
            # EVT_CHAR precedes this wx.EVT_COMBOBOX.
            combo.SetSelection(0)
            combo_event = wx.CommandEvent(wx.EVT_COMBOBOX.typeId, combo.GetId())
            combo_event.SetEventObject(combo)
            combo_event.SetInt(0)
            combo.GetEventHandler().ProcessEvent(combo_event)

            assert combo.GetString(combo.GetSelection()) == "Croatia"
            assert notifications == [0]
        finally:
            frame.Destroy()
