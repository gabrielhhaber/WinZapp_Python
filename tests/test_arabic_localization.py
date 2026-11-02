"""Arabic packaging and branding checks; no GUI objects are constructed."""

from pathlib import Path
import re
import polib


ROOT = Path(__file__).resolve().parents[1]


def test_arabic_keeps_the_product_name_in_english():
    catalog = polib.pofile(str(ROOT / 'translations/ar/LC_MESSAGES/winzapp.po'))
    for entry in catalog:
        if not entry.obsolete:
            assert 'وينزاب' not in entry.msgstr, entry.msgctxt
            if 'WinZapp' in entry.msgid:
                assert 'WinZapp' in entry.msgstr, entry.msgctxt


def test_the_arabic_guide_is_a_complete_right_to_left_page():
    text = (ROOT / 'client/data/help_guide/ar.html').read_text(encoding='utf-8')
    assert '<html lang="ar" dir="rtl">' in text
    assert 'href="en-US.html"' not in text
    assert 'وينزاب' not in text
    assert 'WinZapp' in text


def test_only_the_arabic_guide_runs_right_to_left():
    text = (ROOT / 'client/data/help_guide/en-US.html').read_text(encoding='utf-8')
    assert '<html lang="en" dir="ltr">' in text


def test_no_other_script_leaked_into_the_arabic_guide():
    text = (ROOT / 'client/data/help_guide/ar.html').read_text(encoding='utf-8')
    assert re.findall(r'[가-힯Ѐ-ӿ一-鿿]+', text) == []
