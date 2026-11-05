"""core/caret_edit.py: editing a field's value at its native caret position."""
from core.caret_edit import delete_before_caret, insert_at_caret

LAPTOP = chr(0x1F4BB)


def test_an_emoji_moves_the_caret_by_two_on_windows():
    assert insert_at_caret("@", 1, LAPTOP, utf16=True) == ("@" + LAPTOP, 3)
    assert insert_at_caret("@", 1, LAPTOP, utf16=False) == ("@" + LAPTOP, 2)


def test_text_after_an_emoji_goes_after_it():
    text = "a" + LAPTOP
    assert insert_at_caret(text, 3, "b", utf16=True) == ("a" + LAPTOP + "b", 4)


def test_backspace_after_an_emoji_removes_the_whole_emoji():
    assert delete_before_caret("a" + LAPTOP, 3, utf16=True) == ("a", 1)
    assert delete_before_caret("ab", 2) == ("a", 1)
    assert delete_before_caret("", 0) is None


def test_a_windows_line_break_counts_two():
    assert insert_at_caret("a\nb", 3, "x", newline_width=2) == ("a\nxb", 4)
