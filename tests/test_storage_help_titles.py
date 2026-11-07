"""Published storage navigation/headings preserve the full locale title."""
from html.parser import HTMLParser
from pathlib import Path

import polib
import pytest

from tests.locales import registered_locale_codes

ROOT = Path(__file__).resolve().parents[1]


class Titles(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active, self.parts, self.navigation, self.headings = None, [], [], []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href") == "#armazenamento":
            self.active, self.parts = "a", []
        if tag == "h2" and attrs.get("id") == "h-armazenamento":
            self.active, self.parts = "h2", []
    def handle_data(self, data):
        if self.active: self.parts.append(data)
    def handle_endtag(self, tag):
        if self.active == tag:
            (self.navigation if tag == "a" else self.headings).append("".join(self.parts).strip())
            self.active = None


@pytest.mark.parametrize("locale", registered_locale_codes())
def test_storage_nav_and_heading_keep_the_full_feature_name(locale):
    catalog = polib.pofile(str(ROOT / "translations" / locale / "LC_MESSAGES/winzapp.po"))
    title = catalog.find("storage_manager_title", by="msgctxt").msgstr
    parsed = Titles()
    parsed.feed((ROOT / "client/data/help_guide" / (locale + ".html")).read_text(encoding="utf-8"))
    assert len(parsed.navigation) == 2 and all(text.endswith(title) for text in parsed.navigation)
    assert len(parsed.headings) == 1 and title in parsed.headings[0]
