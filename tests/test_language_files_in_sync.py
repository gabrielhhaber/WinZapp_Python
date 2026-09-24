"""Every UI language ships the same set of keys.

I18n.t() is `translations.get(key, key)` — there is no per-key fallback to any
other locale. A key missing from the language file in use therefore reaches the
user as the raw key name ("about_license", "status_reply_send") in the middle
of the UI, which is exactly what happened to pl.json: it drifted 68 keys behind
while features were added, and nothing failed until someone actually switched
the app to Polish.

The expected key set is the *union* of what all five locales define, not
pt-BR's. Anchoring on pt-BR would assume it is always the most complete file,
and that assumption breaks the moment a string arrives from outside the usual
flow: a contributor adding a key to en-US (or pl) and forgetting pt-BR would
leave pt-BR — the locale the app defaults to — showing a raw key name, while a
pt-BR-anchored check happily reported that en-US had an "unknown" key, if it
said anything at all. With the union, a key added anywhere is owed by everyone,
and whichever file is behind is the one that fails.

These tests are what "add the key to all five files" in CLAUDE.md is enforced
by.
"""

import json
import re
from pathlib import Path

import pytest

from app_paths import resource_path

# The locale I18n falls back to when settings carry none (see I18n.__init__ /
# get_language) — it has to exist, whatever the union says.
DEFAULT_LOCALE = "pt-BR"


def _load(name):
    with open(resource_path("languages", f"{name}.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _language_map():
    return _load("language_map")


LOCALES = sorted(_language_map())


@pytest.fixture(scope="module")
def translations():
    """{locale: {key: text}} for every registered locale."""
    return {locale: _load(locale) for locale in LOCALES}


@pytest.fixture(scope="module")
def every_key(translations):
    """Every key any locale defines — the set all of them are held to."""
    keys = set()
    for table in translations.values():
        keys |= set(table)
    return keys


def test_the_default_locale_is_registered():
    assert DEFAULT_LOCALE in _language_map()


@pytest.mark.parametrize("locale", LOCALES)
def test_every_registered_locale_has_a_language_file(locale):
    assert _load(locale), f"{locale}.json is missing or empty"


def test_every_language_file_is_registered():
    # The converse of the test above. Every check in this module iterates the
    # map, so a `<code>.json` dropped into languages/ without its map entry is
    # invisible to all of them: it can miss any number of keys and the suite
    # stays green — and the language picker never offers it either, so the
    # translation ships as dead weight. PR #276 (Romanian) arrived exactly
    # like that.
    languages_dir = Path(resource_path("languages"))
    on_disk = {p.stem for p in languages_dir.glob("*.json")} - {"language_map"}
    unregistered = sorted(on_disk - set(_language_map()))
    assert not unregistered, (
        f"language file(s) {unregistered} exist in {languages_dir} but are not "
        f"registered in language_map.json — add a {{code: display name}} entry "
        f"for each, or no test checks them and the app never offers them."
    )


@pytest.mark.parametrize("locale", LOCALES)
def test_locale_defines_every_key_the_others_do(locale, translations, every_key):
    missing = sorted(every_key - set(translations[locale]))
    # Name who does have each one, so the fix is a copy from a known file
    # rather than a hunt — and so a key that exists in only one locale is
    # visibly a forgotten translation rather than a mystery.
    owners = {
        key: sorted(loc for loc, table in translations.items() if key in table)
        for key in missing[:10]
    }
    assert missing == [], (
        f"{locale}.json is missing {len(missing)} key(s) other locales define — "
        f"they would render as the raw key name in the UI. "
        f"First few, with the locales that have them: {owners}"
    )


@pytest.mark.parametrize("locale", LOCALES)
def test_locale_has_no_blank_translations(locale):
    blank = sorted(k for k, v in _load(locale).items() if not v.strip())
    assert blank == [], f"{locale}.json has empty translations: {blank[:10]}"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_ampersand_is_a_well_formed_mnemonic(locale):
    # wx reads "&" in a label as "the next character is this control's Alt
    # shortcut", so a translator using it as the word "and" silently eats a
    # character and hands the shortcut to whatever followed. pt-PT shipped
    # "Fotos & vídeos" for exactly that reason; a literal ampersand has to be
    # written "&&". WHICH letter carries the mnemonic is a per-language
    # decision, so this checks only that every "&" is well formed — never that
    # locales agree on where mnemonics go.
    malformed = sorted(
        key for key, text in _load(locale).items()
        # "&&" is the escape for a literal "&" — drop those first, then no
        # surviving "&" may sit before anything but a letter or digit.
        if any(not m.group(1).isalnum()
               for m in re.finditer(r"&(.?)", text.replace("&&", "")))
    )
    assert malformed == [], (
        f"{locale}.json uses & as text rather than as a mnemonic marker "
        f"(write it as &&): {malformed}"
    )


def test_placeholders_agree_across_locales(translations, every_key):
    # Every string is fed through str.format(): a translation that drops {name}
    # silently loses information, and one that invents a placeholder the call
    # site does not pass raises KeyError instead. Compared between all locales
    # that define the key rather than against one reference file, for the same
    # reason the key set is a union — the reference is not guaranteed to be the
    # correct one, or even to have the key.
    divergent = {}
    for key in sorted(every_key):
        variants = {}
        for locale, table in translations.items():
            if key not in table:
                continue
            found = tuple(sorted(re.findall(r"\{(\w+)\}", table[key])))
            variants.setdefault(found, []).append(locale)
        if len(variants) > 1:
            divergent[key] = {ph: locs for ph, locs in variants.items()}
    assert divergent == {}, (
        f"placeholders differ between locales for these keys "
        f"(placeholders: locales that use them): {divergent}"
    )
