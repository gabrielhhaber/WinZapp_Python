"""Accessible emoji picker data and native insertion behavior."""

from ui.dialogs.emoji_picker import (
    EMOJI_CATEGORIES,
    EmojiPickerDialog,
    filter_emojis,
    insert_emoji,
)
import pytest

# Constructs a REAL top-level wx dialog - see the wxgui marker in pytest.ini.
pytestmark = pytest.mark.wxgui


class _TextCtrl:
    def __init__(self):
        self.written = []
        self.focus_calls = 0

    def WriteText(self, value):
        self.written.append(value)

    def SetFocus(self):
        self.focus_calls += 1


def test_every_emoji_category_has_unique_values():
    keys = [key for key, _ in EMOJI_CATEGORIES]
    assert len(keys) == len(set(keys))
    assert all(values.split() for _, values in EMOJI_CATEGORIES)


def test_insert_emoji_uses_native_text_control_selection_and_restores_focus():
    field = _TextCtrl()

    assert insert_emoji(field, "😀") is True

    assert field.written == ["😀"]
    assert field.focus_calls == 1


def test_empty_emoji_is_not_inserted():
    field = _TextCtrl()

    assert insert_emoji(field, "") is False
    assert field.written == []
    assert field.focus_calls == 0


def test_search_matches_portuguese_alias_without_accents():
    labels = [key for key, _ in EMOJI_CATEGORIES]

    assert "❤️" in filter_emojis("coracao", 0, labels)


def test_search_uses_unicode_names_across_all_categories():
    labels = [key for key, _ in EMOJI_CATEGORIES]

    assert "🐶" in filter_emojis("dog", 0, labels)


def test_search_accepts_and_validates_a_complete_multicharacter_word():
    labels = [key for key, _ in EMOJI_CATEGORIES]

    results = filter_emojis("cachorro", 0, labels)
    assert results[0] == "🐶"
    assert "🌭" in results  # "cachorro-quente" is also a legitimate match


def test_search_covers_portuguese_terms_multiword_and_typo():
    labels = [key for key, _ in EMOJI_CATEGORIES]

    assert filter_emojis("tartaruga", 0, labels)[0] == "🐢"
    assert filter_emojis("ambulância", 0, labels)[0] == "🚑"
    assert filter_emojis("cadeado fechado", 0, labels)[0] == "🔒"
    assert filter_emojis("cachoro", 0, labels)[0] == "🐶"
    assert filter_emojis("fone de ouvido", 0, labels)[0] == "🎧"
    assert filter_emojis("rosto dormindo", 0, labels)[0] == "😴"
    assert filter_emojis("bola de basquete", 0, labels)[0] == "🏀"
    assert filter_emojis("emoji de fogo", 0, labels)[0] == "🔥"
    assert filter_emojis("🇧🇷", 0, labels) == ["🇧🇷"]


class _I18n:
    _values = {
        "emoji_picker_search": "&Buscar emoji",
        "emoji_picker_search_hint": "Digite um nome",
        "emoji_picker_search_results_label": "Resultados da busca de emoji",
        "emoji_picker_search_results": "{count} emojis encontrados",
        "emoji_picker_search_no_results": "Nenhum emoji encontrado",
        "emoji_picker_queued": "{emoji} adicionado. {count} emojis preparados.",
    }

    def t(self, key):
        return self._values.get(key, key)


def test_native_search_keeps_the_full_value_and_clears_an_invalid_result(wx_app):
    dialog = EmojiPickerDialog(None, _I18n())
    try:
        assert dialog._search.GetName() == "Buscar emoji"
        assert dialog._search_button.GetName() == "Buscar emoji"
        assert dialog._search.IsEnabled() is False

        dialog._on_search_button(None)
        assert dialog._search.IsEnabled() is True

        dialog._search.ChangeValue("cachorro")
        dialog._on_search_changed(None)
        assert dialog._search.GetValue() == "cachorro"
        assert dialog._list.GetItemCount() >= 1
        assert dialog.get_selected_emoji() == "🐶"
        assert dialog._result_status.GetLabel().endswith("emojis encontrados")

        dialog._search.ChangeValue("termo que não existe")
        dialog._on_search_changed(None)
        assert dialog._list.GetItemCount() == 0
        assert dialog.get_selected_emoji() == ""
        assert dialog._result_status.GetLabel() == "Nenhum emoji encontrado"
    finally:
        dialog.Destroy()


def test_ctrl_enter_queues_multiple_emojis_without_changing_single_insert(wx_app):
    dialog = EmojiPickerDialog(None, _I18n())
    try:
        dialog._list.Select(0, False)
        dialog._selected_emoji = "😀"
        assert dialog._queue_current_emoji() is True
        dialog._selected_emoji = "❤️"
        assert dialog._queue_current_emoji() is True
        dialog._selected_emoji = "🙏"

        assert dialog._queued_emojis == ["😀", "❤️"]
        assert dialog._final_selection() == "😀❤️🙏"
        assert dialog._result_status.GetLabel().startswith("❤️ adicionado. 2")

        dialog._queued_emojis.clear()
        assert dialog._final_selection() == "🙏", \
            "normal Enter must retain the original one-emoji behavior"
    finally:
        dialog.Destroy()


def test_empty_search_keeps_the_selected_category_only():
    labels = [key for key, _ in EMOJI_CATEGORIES]
    selected = 3

    assert filter_emojis("", selected, labels) == EMOJI_CATEGORIES[selected][1].split()


def test_pasting_an_emoji_finds_that_emoji():
    """Looking a row up by pasting it used to answer with nothing at all:
    filter_emojis() computed `direct_emoji` and then wrapped the entire body in
    `if not direct_emoji`, so the one row that matched perfectly was the only
    one skipped."""
    labels = [key for key, _ in EMOJI_CATEGORIES]

    results = filter_emojis("\U0001f436", 0, labels)

    assert results[0] == "\U0001f436"


def test_a_row_named_by_the_query_outranks_one_merely_associated_with_it():
    """"cachorro" is a name of 🐶 and a CLDR association of 🦴 (bone). Both
    match verbatim, so with a single flat word bucket the winner was whichever
    category came first — the bone."""
    labels = [key for key, _ in EMOJI_CATEGORIES]

    results = filter_emojis("cachorro", 0, labels)

    assert results.index("\U0001f436") < results.index("\U0001f9b4")


def test_typo_tolerance_ranks_by_how_close_the_match_is():
    """"cachoro" is one missing letter from "cachorro" and two edits from
    "choro". Unranked, tolerance returned its hits in category order and
    answered 😢 first."""
    labels = [key for key, _ in EMOJI_CATEGORIES]

    results = filter_emojis("cachoro", 0, labels)

    assert results[0] == "\U0001f436"
    assert results.index("\U0001f436") < results.index("\U0001f622")


def test_an_unrelated_query_still_finds_nothing():
    """The tiers must not turn ranking into matching."""
    labels = [key for key, _ in EMOJI_CATEGORIES]

    assert filter_emojis("zzzzqqqqxxxx", 0, labels) == []
