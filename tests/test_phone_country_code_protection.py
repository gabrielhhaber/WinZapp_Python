"""Tests for ui.dialogs.connect._resolve_protected_edit().

The pairing dialog's phone field displays "+<country code> <local number>"
and used to let the user freely Backspace/Delete/type into the "+<country
code>" portion. Because _format_phone_display() derives the displayed local
number by stripping the country code off the FRONT of the digit string
(`digits[len(cc):] if digits.startswith(cc) else digits`), corrupting even
one of those leading digits made it stop matching the stored dial code and
silently folded extra/missing digits into the local number instead — e.g.
typing a stray "9" at the very start of "+55 11987654321" produced
"+55 95511987654321" (the phone number actually being paired shifts, then
gets sent to WhatsApp corrupted) rather than editing the local number as the
user intended. The fix makes the "+<country code>" prefix immutable except
through the country ComboBox (Connect.on_country_changed).

A caret-only edit (no selection) that would touch the prefix is a pure
no-op (returns None). A selection that starts inside the prefix but extends
past it — the common case is Ctrl+A then Delete/Backspace/Paste/typing — is
NOT blocked outright: it is clamped to start right after the prefix, so
"select everything, delete" clears the local number and leaves the country
code exactly as it was, which is the behaviour a user pressing Ctrl+A then
Delete actually expects.
"""

from ui.dialogs.connect import _resolve_protected_edit

# "+55" - Brazil's prefix is 3 characters: '+', '5', '5'.
PREFIX_LEN = 3


class TestTypedCharacterAndForwardDelete:
    """Both act at the caret (insertion_point) with no deleting_backward."""

    def test_typing_before_prefix_end_is_a_no_op(self):
        assert _resolve_protected_edit(PREFIX_LEN, 0, 0, 0) is None
        assert _resolve_protected_edit(PREFIX_LEN, 2, 2, 2) is None

    def test_typing_exactly_at_prefix_end_is_unaffected(self):
        assert _resolve_protected_edit(PREFIX_LEN, 3, 3, 3) == (3, 3)

    def test_typing_after_prefix_is_unaffected(self):
        assert _resolve_protected_edit(PREFIX_LEN, 7, 7, 7) == (7, 7)


class TestBackspace:
    """Backspace removes the character immediately before the caret."""

    def test_backspace_at_prefix_end_removes_last_protected_char_is_a_no_op(self):
        # Caret at position 3 ("+55|"): Backspace would delete index 2 ('5').
        assert _resolve_protected_edit(
            PREFIX_LEN, 3, 3, 3, deleting_backward=True
        ) is None

    def test_backspace_just_after_prefix_is_unaffected(self):
        # Caret at position 4 ("+55 |"): Backspace deletes index 3 (space).
        assert _resolve_protected_edit(
            PREFIX_LEN, 4, 4, 4, deleting_backward=True
        ) == (4, 4)

    def test_backspace_at_start_of_field_is_a_no_op(self):
        assert _resolve_protected_edit(
            PREFIX_LEN, 0, 0, 0, deleting_backward=True
        ) is None


class TestSelectionSpanningTheBoundary:
    """The Ctrl+A case: a selection starting inside the prefix but ending
    past it must be CLAMPED (local number cleared, country code kept), not
    blocked outright."""

    def test_select_all_then_delete_clamps_to_just_after_prefix(self):
        assert _resolve_protected_edit(PREFIX_LEN, 99, 0, 99) == (3, 99)

    def test_select_all_then_backspace_clamps_the_same_way(self):
        # deleting_backward is irrelevant once there's a selection — a
        # selection-based edit always acts on the selection itself.
        assert _resolve_protected_edit(
            PREFIX_LEN, 99, 0, 99, deleting_backward=True
        ) == (3, 99)

    def test_selection_spanning_into_local_number_from_mid_prefix_clamps(self):
        assert _resolve_protected_edit(PREFIX_LEN, 5, 1, 5) == (3, 5)


class TestSelectionEntirelyInsideOrAfterThePrefix:
    def test_selection_entirely_inside_prefix_is_a_no_op(self):
        assert _resolve_protected_edit(PREFIX_LEN, 2, 0, 2) is None
        # Ending exactly at the boundary still counts as "entirely inside".
        assert _resolve_protected_edit(PREFIX_LEN, 3, 1, 3) is None

    def test_selection_entirely_after_prefix_is_unaffected(self):
        assert _resolve_protected_edit(PREFIX_LEN, 10, 4, 10) == (4, 10)

    def test_selection_starting_exactly_at_prefix_end_is_unaffected(self):
        assert _resolve_protected_edit(PREFIX_LEN, 8, 3, 8) == (3, 8)


class TestDifferentCountryCodeLengths:
    """prefix_len varies with the selected country ("+1" = 2 chars,
    "+1268" (Antigua and Barbuda) = 5 chars) — not hardcoded to Brazil."""

    def test_short_dial_code(self):
        # "+1" — protects positions 0-1.
        assert _resolve_protected_edit(2, 1, 1, 1) is None
        assert _resolve_protected_edit(2, 2, 2, 2) == (2, 2)

    def test_long_dial_code(self):
        # "+1268" — protects positions 0-4.
        assert _resolve_protected_edit(5, 4, 4, 4) is None
        assert _resolve_protected_edit(5, 5, 5, 5) == (5, 5)
        # Ctrl+A then Delete with the longer prefix clamps to 5, not 3.
        assert _resolve_protected_edit(5, 20, 0, 20) == (5, 20)
