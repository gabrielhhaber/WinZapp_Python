"""Editing a text field's value at its native caret position.

A wx.TextCtrl's caret counts what the native control counts: on Windows (and
macOS) every emoji outside the BMP takes two positions (UTF-16 units) and a
Windows multiline control counts a line break as two. GetValue() is a Python
str, where both take one. Slicing the value by the caret position put text
in the wrong place after an emoji, and moving the caret by one after
inserting an emoji left it between its two halves: the next key typed went
in the middle, and the send dropped both halves (core/text_surrogates.py).

The mention list types into the message field this way (composer.py). Pure
functions over core/emoticons.py's unit counting, tested in
tests/test_caret_edit.py.
"""

from core.emoticons import caret_value_index, native_units


def insert_at_caret(text, position, chars, newline_width=1, utf16=False):
    """(new value, new native caret) after inserting *chars* at *position*."""
    index = caret_value_index(text, position, newline_width, utf16)
    advance = sum(native_units(c, newline_width, utf16) for c in chars)
    return text[:index] + chars + text[index:], position + advance


def delete_before_caret(text, position, newline_width=1, utf16=False):
    """(new value, new native caret) after Backspace at *position*, or None
    when there is nothing before the caret. A whole character goes, never
    half of an emoji."""
    index = caret_value_index(text, position, newline_width, utf16)
    if index <= 0:
        return None
    removed = text[index - 1]
    return (text[:index - 1] + text[index:],
            max(0, position - native_units(removed, newline_width, utf16)))
