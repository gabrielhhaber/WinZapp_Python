"""Two consumers disagree about what a line break is, and both have to win.

WhatsApp, the database and every comparison in this codebase want the canonical
``\\n``. A screen reader navigating a `wx.TextCtrl` with the arrow keys wants
``\\r\\n``: with a bare ``\\n`` NVDA reads a pasted block as ONE line, and Up/Down
move through it as though the breaks were not there.

Reported by pasting a plain-LF file out of Notepad — verified: 26 425 bytes,
644 LF, zero CR. `normalize_line_separators()` collapsed everything to ``\\n``,
which is right for sending and exactly wrong for the field the user then has to
read back.

So the field holds the editor form and the send path collapses it again. The
round trip is the property that keeps a stray CR off WhatsApp, and it is
pinned here rather than trusted.
"""

import inspect
import re

import pytest

from core.utils import normalize_line_separators, to_editor_line_endings
from ui.conversations import ConversationsPanel


class TestTheEditorForm:
    def test_bare_newlines_become_crlf(self):
        assert to_editor_line_endings("a\nb\nc") == "a\r\nb\r\nc"

    def test_text_that_is_already_crlf_is_unchanged(self):
        """The clipboard on Windows usually supplies CRLF already. Doubling it
        into \\r\\r\\n would show a blank line between every line."""
        assert to_editor_line_endings("a\r\nb") == "a\r\nb"

    def test_applying_it_twice_changes_nothing(self):
        once = to_editor_line_endings("a\nb")
        assert to_editor_line_endings(once) == once

    @pytest.mark.parametrize("separator", [" ", " ", "", "\x0b", "\x0c"])
    def test_the_exotic_separators_are_converted_too(self, separator):
        """Google Docs, Word and Apple apps copy these where a plain editor
        stores \\n — the case normalize_line_separators() already existed for."""
        assert to_editor_line_endings(f"a{separator}b") == "a\r\nb"

    def test_a_lone_cr_does_not_survive(self):
        assert to_editor_line_endings("a\rb") == "a\r\nb"

    @pytest.mark.parametrize("value", ["", None, "sem quebras"])
    def test_text_without_breaks_is_left_alone(self, value):
        assert "\r" not in to_editor_line_endings(value)


class TestTheRoundTripKeepsWhatsAppClean:
    """The send paths already call normalize_line_separators() on the field's
    value. That is what makes putting CRLF in the field safe — and if it ever
    stops being true, WhatsApp starts receiving control characters."""

    @pytest.mark.parametrize("original", [
        "a\nb\nc",
        "a\r\nb",
        "linha\n\nparagrafo",
        "a b",
        "sem quebras",
    ])
    def test_the_editor_form_collapses_back_to_the_canonical_one(self, original):
        canonical = normalize_line_separators(original)
        assert normalize_line_separators(to_editor_line_endings(original)) == canonical

    def test_the_send_path_still_normalizes_the_field(self):
        source = inspect.getsource(ConversationsPanel.on_send_message)
        assert "normalize_line_separators(self.message_field.GetValue())" in source, (
            "the composer now holds CRLF; without this call every multi-line "
            "message reaches WhatsApp with a carriage return on every line"
        )


class TestOnlyMultilineFieldsGetIt:
    """_on_text_field_paste is bound to two controls: the multiline message
    field and the SINGLE-LINE attachment caption. A single-line wx.TextCtrl
    cannot navigate lines and does not translate line endings — it would just
    hold the control characters."""

    def test_the_paste_handler_checks_the_control(self):
        source = inspect.getsource(ConversationsPanel._on_text_field_paste)
        assert "IsMultiLine()" in source
        assert "to_editor_line_endings(" in source

    def test_the_caption_field_is_single_line(self):
        """If this ever gains TE_MULTILINE the guard above starts converting it
        too, which is correct — but the reasoning in the handler would be stale,
        so fail loudly and make someone re-read it."""
        source = inspect.getsource(ConversationsPanel)
        caption = source[source.index("self._caption_field = wx.TextCtrl("):]
        caption = caption[:caption.index(")")]
        assert "TE_MULTILINE" not in caption

    def test_the_message_field_is_multiline(self):
        source = inspect.getsource(ConversationsPanel)
        field = source[source.index("self.message_field = wx.TextCtrl("):]
        field = field[:field.index(")")]
        assert "TE_MULTILINE" in field


class TestEveryPathThatFillsTheComposer:
    """Paste is what was reported, but text reaches the composer three ways and
    a screen-reader user cannot navigate any of them without CRLF."""

    @pytest.mark.parametrize("method_name", [
        "_on_text_field_paste",        # Ctrl+V in the composer
        "_paste_from_messages_list",   # Ctrl+V with the history focused
    ])
    def test_the_paste_paths_produce_the_editor_form(self, method_name):
        source = inspect.getsource(getattr(ConversationsPanel, method_name))
        assert "to_editor_line_endings(" in source

    def test_editing_a_sent_message_produces_it_too(self):
        """Stored message text is canonical \\n; dropped into the field as-is it
        is one unnavigable line."""
        source = inspect.getsource(ConversationsPanel)
        call = re.search(
            r"self\.message_field\.SetValue\(to_editor_line_endings\(content\)\)",
            source)
        assert call, "the edit-message pre-fill must convert to the editor form"
