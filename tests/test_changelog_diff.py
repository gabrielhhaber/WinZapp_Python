"""Two copies of a changelog compared item by item, said in words."""

import pathlib

import pytest

from core import changelog_diff as diff
from tests.locales import registered_locale_codes

ROOT = pathlib.Path(__file__).resolve().parents[1]

OLD = """V2.1.0.0

This release brings transcription.

WHAT'S NEW

- Calls can be recorded.

- Reactions show who reacted.

FIXES

- The chat list no longer jumps to the top when a message arrives in another chat.

- Typing an emoji no longer moves the cursor.

V2.0.0.0

The big news is calls.

WHAT'S NEW

- Voice calls.
"""

NEW = """V2.1.0.1

A small update.

FIXES

- Stickers open again.

V2.1.0.0

This release brings transcription.

WHAT'S NEW

- Calls can be recorded.

- Communities are listed.

FIXES

- The chat list no longer jumps to the top when a message arrives in a different chat.

- Reactions show who reacted.

V2.0.0.0

The big news is calls.

WHAT'S NEW

- Voice calls.
"""

KEYS = {
    "changelog_diff_version": "Version {version}",
    "changelog_diff_new_version": "New version {version}, in full:",
    "changelog_diff_added": "New in the section {section}:",
    "changelog_diff_changed": "Changed in the section {section}, as it now reads:",
    "changelog_diff_removed": "Removed from the section {section}:",
    "changelog_diff_summary": "Version summary",
}


def t(key):
    return KEYS[key]


class TestParsing:
    def test_versions_headings_and_items(self):
        [newest, older] = diff.parse(OLD)
        assert newest.number == "2.1.0.0" and older.number == "2.0.0.0"
        assert newest.sections == (
            (diff.SUMMARY, ("This release brings transcription.",)),
            ("WHAT'S NEW", ("Calls can be recorded.", "Reactions show who reacted.")),
            ("FIXES", ("The chat list no longer jumps to the top when a message arrives "
                       "in another chat.", "Typing an emoji no longer moves the cursor.")),
        )

    def test_an_alpha_number_is_a_version_too(self):
        assert diff.parse("V2.1.0.4391alpha\n\n- x\n")[0].number == "2.1.0.4391alpha"

    @pytest.mark.parametrize("locale", registered_locale_codes())
    def test_every_shipped_changelog_parses_with_items_in_every_version(self, locale):
        versions = diff.parse((ROOT / "client" / f"changelog_{locale}.txt").read_text(encoding="utf-8"))
        assert versions, locale
        for version in versions:
            assert sum(len(items) for _heading, items in version.sections) > 1, version.number

    def test_every_locale_has_the_same_versions_and_headings_count(self):
        shapes = {}
        for locale in registered_locale_codes():
            text = (ROOT / "client" / f"changelog_{locale}.txt").read_text(encoding="utf-8")
            shapes[locale] = [(v.number, len(v.sections)) for v in diff.parse(text)]
        assert len({tuple(shape) for shape in shapes.values()}) == 1, shapes


class TestComparing:
    def test_new_changed_removed_and_a_whole_new_version(self):
        fresh, changes = diff.compare(OLD, NEW)
        assert [version.number for version in fresh] == ["2.1.0.1"]
        found = {(c.heading, c.kind, c.text) for c in changes}
        assert found == {
            ("WHAT'S NEW", diff.ADDED, "Communities are listed."),
            ("FIXES", diff.CHANGED, "The chat list no longer jumps to the top when a message "
                                    "arrives in a different chat."),
            ("FIXES", diff.REMOVED, "Typing an emoji no longer moves the cursor."),
        }

    def test_an_item_moved_to_another_heading_unchanged_is_not_news(self):
        _fresh, changes = diff.compare(OLD, NEW)
        assert not any("Reactions show who reacted." == c.text for c in changes)

    def test_the_same_text_has_no_changes(self):
        assert diff.compare(NEW, NEW) == ([], [])
        assert diff.describe([], [], t) == ""


class TestSayingIt:
    def test_in_sentences_without_a_developers_diff(self):
        text = diff.describe(*diff.compare(OLD, NEW), t)
        assert text.startswith("New version 2.1.0.1, in full:\n\nA small update.")
        assert "Version 2.1.0.0" in text
        assert "New in the section WHAT'S NEW:\n- Communities are listed." in text
        assert ("Changed in the section FIXES, as it now reads:\n- The chat list no longer "
                "jumps to the top when a message arrives in a different chat.") in text
        assert "Removed from the section FIXES:\n- Typing an emoji no longer moves the cursor." in text
        for line in text.splitlines():
            assert not line.startswith(("+", "@@", "--- ", "+++ ")), line

    def test_the_summary_paragraph_is_named(self):
        old = "V1.0.0.0\n\nOld summary of things.\n\nFIXES\n\n- a\n"
        new = "V1.0.0.0\n\nA completely different opening.\n\nFIXES\n\n- a\n"
        text = diff.describe(*diff.compare(old, new), t)
        assert "Version summary" in text
