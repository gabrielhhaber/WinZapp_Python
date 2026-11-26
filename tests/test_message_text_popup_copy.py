"""The Alt+C message text popup wraps for reading but copies the original.

Its breaks at ~100 characters are a view: copying a selection used to hand
out those extra line breaks, splitting the message wherever the wrap had.
"""


from core.wrapped_text import (
    message_viewer_wraps,
    native_position,
    normalize_newlines,
    original_range,
    selection_offsets,
    switch_wrap,
    viewer_text,
    word_wrap,
)
from tests.god_modules import conversations_method_source


LONG = ("palavra " * 40).strip()  # 319 chars, needs several wrapped lines


def test_wrap_keeps_every_line_within_width_without_breaking_words():
    wrapped = word_wrap(LONG, width=100)
    lines = wrapped.split("\n")
    assert len(lines) > 1
    assert all(len(line) <= 100 for line in lines)
    assert all(word == "palavra" for line in lines for word in line.split(" "))


def test_wrap_preserves_length_and_only_turns_spaces_into_breaks():
    text = "a  b" + " x" * 120 + "\nsegunda linha\n\n" + "y " * 80 + " "
    wrapped = word_wrap(text, width=100)
    assert len(wrapped) == len(text)
    for orig_ch, wrap_ch in zip(text, wrapped):
        assert orig_ch == wrap_ch or (orig_ch == " " and wrap_ch == "\n")


def test_wrap_keeps_a_word_longer_than_width_whole():
    word = "z" * 150
    assert word_wrap(f"{word} fim", width=100) == f"{word}\nfim"


def test_copying_everything_returns_the_original_message():
    text = LONG + "\n\nquebra original\n" + LONG
    wrapped = word_wrap(text)
    assert wrapped.count("\n") > text.count("\n")
    assert original_range(text, 0, len(wrapped)) == text


def test_copying_a_range_across_a_wrap_break_gives_a_space_back():
    wrapped = word_wrap(LONG)
    brk = wrapped.index("\n")
    copied = original_range(LONG, brk - 7, brk + 8)
    assert copied == "palavra palavra"
    assert "\n" not in copied


def test_the_messages_own_breaks_survive_a_partial_copy():
    text = "primeira\nsegunda"
    assert original_range(text, 3, 12) == "meira\nseg"


def test_crlf_in_the_original_is_normalized_so_offsets_line_up():
    text = "um\r\ndois\rtrês"
    assert normalize_newlines(text) == "um\ndois\ntrês"
    wrapped = word_wrap(text)
    assert wrapped == "um\ndois\ntrês"
    assert original_range(text, 0, len(wrapped)) == "um\ndois\ntrês"


def test_out_of_range_offsets_are_clamped():
    assert original_range("abc", -5, 99) == "abc"
    assert original_range("abc", 2, 1) == ""


class _FakeWinEdit:
    """A plain Windows multiline edit as wxMSW exposes it: stores CRLF,
    counts native positions in UTF-16 units, hands text back with LF."""

    def __init__(self, value):
        self._native = self._to_native(value)

    @staticmethod
    def _to_native(value):
        return value.replace("\n", "\r\n").encode("utf-16-le")

    def native_pos(self, py_offset, value):
        return len(self._to_native(value[:py_offset])) // 2

    def GetRange(self, frm, to):
        raw = self._native[frm * 2:to * 2].decode("utf-16-le")
        return raw.replace("\r\n", "\n")

    def select(self, frm, to):
        self._sel = (frm, to)

    def GetStringSelection(self):
        return self.GetRange(*self._sel)


def _copy_through_fake_control(text, py_start, py_end):
    wrapped = word_wrap(text)
    ctrl = _FakeWinEdit(wrapped)
    frm = ctrl.native_pos(py_start, wrapped)
    to = ctrl.native_pos(py_end, wrapped)
    ctrl.select(frm, to)
    offsets = selection_offsets(ctrl.GetRange, ctrl.GetStringSelection, frm, to)
    return None if offsets is None else original_range(text, *offsets)


def test_native_positions_after_crlf_and_emoji_map_back_to_the_original():
    text = "oi \U0001F600\n\n" + "\U0001F600 palavra " * 30 + "\nfim \U0001F44D aqui"
    wrapped = word_wrap(text)
    assert wrapped != text
    tail = wrapped.index("fim")
    assert _copy_through_fake_control(text, tail, len(wrapped)) == "fim \U0001F44D aqui"
    assert _copy_through_fake_control(text, 0, len(wrapped)) == text
    brk = wrapped.index("\n", wrapped.index("palavra"))
    expected = text[brk - 7:brk + 3]
    assert "\n" not in expected
    assert _copy_through_fake_control(text, brk - 7, brk + 3) == expected


def test_an_empty_selection_copies_nothing():
    assert _copy_through_fake_control("abc def", 2, 2) is None


def test_popup_copies_through_the_original_range_not_the_control_text():
    body = conversations_method_source("_show_message_text_popup")
    assert "value=viewer_text(text, message_viewer_wraps(self.main_window.settings))" in body
    assert "selection_offsets(" in body
    assert "original_range(text, *offsets)" in body
    assert "pyperclip.copy(copied)" in body
    assert "wx.EVT_TEXT_COPY" in body
    assert "wx.WXK_INSERT" in body


# ── The window's "Wrap long lines" box (#923) ──

def test_the_viewer_wraps_by_default():
    assert message_viewer_wraps({}) is True
    assert message_viewer_wraps(None) is True
    assert message_viewer_wraps({"user_interface": {}}) is True
    assert message_viewer_wraps({"user_interface": {"message_viewer_word_wrap": True}}) is True
    assert viewer_text(LONG) == word_wrap(LONG)


def test_turned_off_only_the_messages_own_breaks_remain():
    settings = {"user_interface": {"message_viewer_word_wrap": False}}
    assert message_viewer_wraps(settings) is False
    text = LONG + "\r\nsegunda linha\n" + LONG
    shown = viewer_text(text, wrap=False)
    assert shown == LONG + "\nsegunda linha\n" + LONG
    assert shown.count("\n") == 2


def test_unwrapped_view_still_copies_the_original_range():
    text = LONG + "\nfim"
    shown = viewer_text(text, wrap=False)
    assert original_range(text, 0, len(shown)) == text
    start = shown.index("fim")
    assert original_range(text, start, len(shown)) == "fim"


def test_switching_the_wrap_keeps_the_reading_position():
    """The box turns the string offset back into a native position of the
    other view: CRLF and emoji count as two, as in a Windows edit."""
    text = "oi \U0001F600\n" + "\U0001F600 palavra " * 30 + "\nfim"
    for wrap in (True, False):
        shown = viewer_text(text, wrap)
        ctrl = _FakeWinEdit(shown)
        for offset in (0, 5, shown.index("fim"), len(shown)):
            native = native_position(shown, offset)
            assert native == ctrl.native_pos(offset, shown)
            assert len(ctrl.GetRange(0, native)) == offset
    assert native_position("abc", 99) == 3
    assert native_position("abc", -1) == 0


def test_the_window_has_the_wrap_box_and_remembers_it():
    body = conversations_method_source("_show_message_text_popup")
    assert 'i18n.t("msg_text_word_wrap")' in body
    assert "wrap_check.SetValue(message_viewer_wraps(settings))" in body
    assert "switch_wrap(settings, text, wrap_check.GetValue(), point)" in body
    assert "_schedule_save_settings()" in body
    assert "text_ctrl.ChangeValue(shown)" in body
    assert "text_ctrl.SetInsertionPoint(native)" in body


def test_toggling_the_wrap_keeps_the_choice_and_the_reading_position():
    text = "oi \U0001F600\n" + "palavra " * 30 + "fim"
    settings = {}
    shown, native = switch_wrap(settings, text, False, text.index("fim"))
    assert settings == {"user_interface": {"message_viewer_word_wrap": False}}
    assert shown == viewer_text(text, False)
    assert native == native_position(shown, shown.index("fim"))
    shown, native = switch_wrap(settings, text, True, shown.index("fim"))
    assert settings["user_interface"]["message_viewer_word_wrap"] is True
    assert shown == word_wrap(text) and shown.count("\n") > 1
    assert native == native_position(shown, shown.index("fim"))
