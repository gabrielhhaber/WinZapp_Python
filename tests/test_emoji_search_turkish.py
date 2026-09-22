"""Turkish UI users search the emoji picker with Turkish words.

The picker used English Unicode names, curated terms for the older locales,
and Portuguese CLDR annotations.  Adding tr-TR to the UI therefore left
queries such as "mavi kalp" empty even though "blue heart" worked.
"""

from ui.dialogs.emoji_picker import EMOJI_CATEGORIES, filter_emojis


_CATEGORY_LABELS = [key for key, _values in EMOJI_CATEGORIES]


def test_turkish_blue_heart_name_finds_the_blue_heart_first():
    results = filter_emojis("mavi kalp", 0, _CATEGORY_LABELS)

    assert results[0] == "💙"


def test_turkish_color_word_finds_blue_emoji_across_categories():
    results = filter_emojis("mavi", 0, _CATEGORY_LABELS)

    assert "💙" in results
    assert "🔵" in results
    assert "🟦" in results


def test_turkish_search_is_not_limited_to_the_reported_color():
    assert filter_emojis("ambulans", 0, _CATEGORY_LABELS)[0] == "🚑"


def test_dotless_i_matches_a_plain_i_and_capitals():
    # CLDR stores "kırmızı", "yıldız". casefold() lowers "I" to "i", not "ı",
    # and NFKD leaves "ı" alone, so typing on a non-Turkish keyboard or with
    # Caps Lock found nothing.
    for query in ("kırmızı kalp", "kirmizi kalp", "KIRMIZI KALP"):
        assert "❤️" in filter_emojis(query, 0, _CATEGORY_LABELS), query
    for query in ("yıldız", "yildiz", "YILDIZ"):
        assert "⭐" in filter_emojis(query, 0, _CATEGORY_LABELS), query
