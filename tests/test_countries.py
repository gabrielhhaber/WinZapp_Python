"""Tests for countries.get_countries() / get_default_country_index().

Regression coverage for the pairing dialog's country selector: it used to
hardcode Brazil as item 0 in every language and always pre-select it
(client/ui/dialogs/connect.py), regardless of the user's actual Windows
region or the alphabetical position of "Brazil" in the active UI language.
get_countries() now sorts alphabetically (diacritics ignored) by the
localized name with no country pinned first, and get_default_country_index()
picks the entry matching the user's Windows "Country or region" setting
(core.locale_format.get_country_or_region_iso2() — deliberately NOT the UI
language or the separate "Regional format" locale, which can disagree with
it entirely), falling back to the United States when that can't be detected
or isn't one of our entries.
"""

import core.locale_format as locale_format
from countries import (
    get_countries, get_default_country_index, COUNTRIES, _sort_key,
    _COUNTRIES, _KEY_TO_ISO2,
)

LOCALES = ["pt-BR", "pt-PT", "en-US", "es-ES", "pl", "tr-TR", "ro"]


class TestGetCountriesOrdering:
    def test_sorted_alphabetically_by_localized_name(self):
        countries = get_countries("en-US")
        names = [name for name, _ in countries]
        assert names == sorted(names, key=_sort_key)

    def test_no_country_is_pinned_first(self):
        # Brazil used to always be index 0 regardless of language/sorting.
        countries_en = get_countries("en-US")
        countries_pt = get_countries("pt-BR")
        assert countries_en[0][0] != "Brazil (+55)"
        assert countries_pt[0][0] != "Brasil (+55)"

    def test_accented_names_sort_next_to_their_base_letter(self):
        # "Áustria" (accented) must land near "Austrália", not after every
        # plain-ASCII entry (naive ordinal comparison puts accented
        # characters after 'z').
        countries = get_countries("pt-BR")
        names = [name for name, _ in countries]
        austria_idx = next(i for i, n in enumerate(names) if n.startswith("Áustria"))
        assert austria_idx < len(names) // 2

    def test_same_entries_across_languages_different_order(self):
        countries_en = {code for _, code in get_countries("en-US")}
        countries_pt = {code for _, code in get_countries("pt-BR")}
        assert countries_en == countries_pt

    def test_backward_compat_countries_constant_still_exported(self):
        assert ("Brasil (+55)", "55") in COUNTRIES


class TestCountryCoverage:
    def test_every_country_has_a_name_in_every_ui_language(self):
        for key, _, names in _COUNTRIES:
            for lang in ("pt", "en", "es", "pl", "tr", "ro"):
                assert names.get(lang), (key, lang)

    def test_every_country_has_an_iso_code(self):
        assert {key for key, _, _ in _COUNTRIES} == set(_KEY_TO_ISO2)

    def test_previously_missing_countries_are_listed(self):
        for lang in LOCALES:
            codes = {c for _, c in get_countries(lang)}
            assert {"31", "254", "375", "389", "992", "676"} <= codes

    def test_netherlands_name_is_localized(self):
        expected = {
            "pt-BR": "Países Baixos (+31)", "en-US": "Netherlands (+31)",
            "es-ES": "Países Bajos (+31)", "pl": "Holandia (+31)",
            "tr-TR": "Hollanda (+31)", "ro": "Țările de Jos (+31)",
        }
        for lang, label in expected.items():
            assert (label, "31") in get_countries(lang)

    def test_names_are_not_the_portuguese_fallback(self):
        pt = {n for n, _ in get_countries("pt-BR")}
        for lang in ("pl", "tr-TR", "ro"):
            assert "Alemanha (+49)" not in {n for n, _ in get_countries(lang)}
        assert "Alemanha (+49)" in pt


class TestGetDefaultCountryIndex:
    def _patch_region(self, monkeypatch, iso2):
        monkeypatch.setattr(locale_format, "get_country_or_region_iso2", lambda: iso2)

    def test_matches_detected_windows_region(self, monkeypatch):
        self._patch_region(monkeypatch, "HR")
        countries = get_countries("en-US")
        idx = get_default_country_index(countries, "en-US")
        assert countries[idx] == ("Croatia (+385)", "385")

    def test_falls_back_to_united_states_when_detection_fails(self, monkeypatch):
        self._patch_region(monkeypatch, None)
        countries = get_countries("en-US")
        idx = get_default_country_index(countries, "en-US")
        assert countries[idx] == ("United States (+1)", "1")

    def test_falls_back_to_united_states_for_unrecognized_region(self, monkeypatch):
        self._patch_region(monkeypatch, "ZZ")
        countries = get_countries("en-US")
        idx = get_default_country_index(countries, "en-US")
        assert countries[idx] == ("United States (+1)", "1")

    def test_is_case_insensitive_on_the_detected_code(self, monkeypatch):
        self._patch_region(monkeypatch, "hr")
        countries = get_countries("en-US")
        idx = get_default_country_index(countries, "en-US")
        assert countries[idx] == ("Croatia (+385)", "385")

    def test_index_is_valid_for_the_localized_list_passed_in(self, monkeypatch):
        self._patch_region(monkeypatch, "BR")
        countries = get_countries("pt-BR")
        idx = get_default_country_index(countries, "pt-BR")
        assert countries[idx] == ("Brasil (+55)", "55")
