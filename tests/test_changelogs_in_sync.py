"""Every registered locale ships a changelog, and the newest section agrees.

Romanian was added to language_map.json without a changelog_ro.txt, and
nothing noticed: the updater silently falls back to English
(updater.load_changelog_text), so a Romanian user is shown the release notes
in a language they did not choose. The guide said "all five files" while
there were already seven locales — a hardcoded count is exactly what
tests/test_language_files_in_sync.py avoids for the UI strings, so this
derives the set from language_map.json the same way.

The newest section is the one users are about to receive: every locale must
carry the same version header and the same number of items under each
heading (the headings themselves are translated, so they are compared by
position). Older sections are history and are not compared — a locale that
arrived later (Romanian, in 2.0.0.0) legitimately has none of them.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client"
LOCALES = sorted(json.loads((CLIENT / "languages" / "language_map.json").read_text(encoding="utf-8")))
HEADER = re.compile(r"^V\d+\.\d+\.\d+\.\d+\s*$")


def _newest_section(loc):
    lines = (CLIENT / f"changelog_{loc}.txt").read_text(encoding="utf-8").splitlines()
    headers = [i for i, l in enumerate(lines) if HEADER.match(l)]
    assert headers, f"changelog_{loc}.txt has no 'V<version>' header"
    start = headers[0]
    end = headers[1] if len(headers) > 1 else len(lines)
    return lines[start].strip(), lines[start + 1:end]


def _shape(section_lines):
    """Item count under each heading, in order. A heading is a non-empty,
    non-item line after the intro paragraph."""
    blocks = [l for l in section_lines if l.strip()]
    shape = []
    for line in blocks[1:]:  # blocks[0] is the intro paragraph
        if line.startswith("- "):
            assert shape, "an item before any heading"
            shape[-1] += 1
        else:
            shape.append(0)
    return shape


def test_the_locales_are_being_found():
    assert len(LOCALES) >= 5


@pytest.mark.parametrize("loc", LOCALES)
def test_every_registered_locale_ships_a_changelog(loc):
    assert (CLIENT / f"changelog_{loc}.txt").is_file(), (
        f"{loc} is in language_map.json but client/changelog_{loc}.txt does not exist; "
        "the updater would show that user the English notes instead."
    )


def test_the_newest_section_is_the_same_release_everywhere():
    versions = {loc: _newest_section(loc)[0] for loc in LOCALES}
    assert len(set(versions.values())) == 1, versions


def test_the_newest_section_has_the_same_items_everywhere():
    shapes = {loc: _shape(_newest_section(loc)[1]) for loc in LOCALES}
    assert len({tuple(s) for s in shapes.values()}) == 1, (
        "the newest changelog section has a different number of items per heading "
        f"across locales (same items, same order, in every file): {shapes}"
    )


@pytest.mark.parametrize("loc", LOCALES)
def test_newest_section_starts_with_one_intro_and_then_the_news_list(loc):
    blocks = [line for line in _newest_section(loc)[1] if line.strip()]
    first_item = next(i for i, line in enumerate(blocks) if line.startswith("- "))
    assert first_item == 2, (
        f"{loc}: expected one introduction and the news heading before the first item; "
        "new features belong in the list, not in extra introduction paragraphs"
    )
