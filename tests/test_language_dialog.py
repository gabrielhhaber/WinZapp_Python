"""Tests for ui.dialogs.language_dialog's pure helper functions.

Regression coverage for the first-run language picker
(client/ui/dialogs/language_dialog.py): its title, prompt and OK/Cancel
button labels used to be hardcoded in Portuguese regardless of the actual
machine, and the language list itself was shown in language_map.json's raw
dict order (pt-BR, pt-PT, en-US, es-ES, pl) with pt-BR always pre-selected —
none of that had anything to do with the user's actual Windows settings.

_load_bootstrap_strings() now sources this dialog's own UI text from the
matching languages/<code>.json file (the same files core.i18n.I18n reads),
_detect_system_language() picks that code from the real Windows UI/display
language (falling back to English when it isn't one of ours or can't be
detected), and _load_language_choices() sorts the picker's own list
alphabetically with no language pinned first.
"""

import ui.dialogs.language_dialog as language_dialog
import core.locale_format as locale_format
from ui.dialogs.language_dialog import (
    _detect_system_language,
    _load_bootstrap_strings,
    _load_language_choices,
    _BOOTSTRAP_KEYS,
)


class TestLoadLanguageChoices:
    def test_sorted_alphabetically_with_no_language_pinned_first(self):
        choices = _load_language_choices()
        names = [name for name, _ in choices]
        assert names == sorted(
            names,
            key=lambda n: __import__(
                "core.utils", fromlist=["normalize_for_search"]
            ).normalize_for_search(n, mode="nfkd"),
        )
        # pt-BR used to always be index 0 regardless of anything else.
        assert choices[0][1] != "pt-BR" or len(choices) == 1

    def test_every_registered_locale_is_present(self):
        codes = {code for _, code in _load_language_choices()}
        assert {"pt-BR", "pt-PT", "en-US", "es-ES", "pl"} <= codes


class TestDetectSystemLanguage:
    CODES = ["en-US", "es-ES", "pl", "pt-BR", "pt-PT"]

    def test_exact_match(self, monkeypatch):
        monkeypatch.setattr(locale_format, "get_system_ui_language", lambda: "es-ES")
        assert _detect_system_language(self.CODES) == "es-ES"

    def test_same_language_different_region_falls_through_to_a_supported_variant(self, monkeypatch):
        # System UI language is Angolan Portuguese; we don't ship "pt-AO",
        # so this must match one of our Portuguese variants, not English.
        monkeypatch.setattr(locale_format, "get_system_ui_language", lambda: "pt-AO")
        assert _detect_system_language(self.CODES) in ("pt-BR", "pt-PT")

    def test_unsupported_language_falls_back_to_english(self, monkeypatch):
        monkeypatch.setattr(locale_format, "get_system_ui_language", lambda: "hr-HR")
        assert _detect_system_language(self.CODES) == "en-US"

    def test_detection_failure_falls_back_to_english(self, monkeypatch):
        monkeypatch.setattr(locale_format, "get_system_ui_language", lambda: None)
        assert _detect_system_language(self.CODES) == "en-US"

    def test_region_less_code_matches_by_prefix(self, monkeypatch):
        monkeypatch.setattr(locale_format, "get_system_ui_language", lambda: "pl-PL")
        assert _detect_system_language(self.CODES) == "pl"

    def test_custom_fallback_is_honored(self, monkeypatch):
        monkeypatch.setattr(locale_format, "get_system_ui_language", lambda: "hr-HR")
        assert _detect_system_language(self.CODES, fallback="pt-BR") == "pt-BR"


class TestLoadBootstrapStrings:
    def test_returns_all_required_keys_for_every_registered_locale(self):
        for code in ("pt-BR", "pt-PT", "en-US", "es-ES", "pl"):
            strings = _load_bootstrap_strings(code)
            for key in _BOOTSTRAP_KEYS:
                assert strings.get(key), f"{code}: missing/blank {key!r}"

    def test_es_es_strings_are_actually_in_spanish(self):
        strings = _load_bootstrap_strings("es-ES")
        assert strings["language_select_prompt"] == "Seleccionar un idioma"

    def test_unknown_locale_falls_back_to_english(self):
        strings = _load_bootstrap_strings("xx-XX")
        assert strings["language_select_prompt"] == "Select a language"
