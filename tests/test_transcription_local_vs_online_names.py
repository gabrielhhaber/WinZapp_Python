"""The local (Whisper) and online (AI provider) transcription features must be
told apart by name alone, in every language.

A blind user moving through Settings tabs, the context menu or the F1 shortcut
list hears one name at a time. Both features transcribe, and both used to say
just "Transcrever"/"Transcrição": the online one sends the voice message to a
third party, the local one never leaves the computer, so picking the wrong one
is a privacy mistake and not only a cosmetic one. The online names now say
"online AI" and the local ones say "local" / "on this computer", and every
sentence that points at a tab by name must use the tab's current name.
"""


from tests.locales import load_strings, registered_locale_codes
import pytest


LOCALES = list(registered_locale_codes())

#: Per locale, lower-case words that mark the local and the online feature.
#: Taken from the words each file already uses for "local" and "online".
MARKERS = {
    "pt-BR": (("local",), ("ia online",)),
    "pt-PT": (("local",), ("ia online",)),
    "en-US": (("local",), ("online ai",)),
    "es-ES": (("local",), ("ia en línea",)),
    "pl": (("lokaln",), ("ai online",)),
    "tr-TR": (("yerel", "bu bilgisayarda"), ("çevrimiçi",)),
    "ro": (("local",), ("ai online",)),
    "ar": (("محلي",), ("عبر الإنترنت",)),
    "it": (("local",), ("ia online",)),
}

LOCAL_KEYS = (
    "tab_transcription", "transcribe_message", "shortcut_alt_shift_t_label",
    "transcribe_inline_menu", "shortcut_alt_shift_n_label",
)
ONLINE_KEYS = (
    "tab_ai_accessibility", "ai_transcribe_audio_menu", "shortcut_ctrl_shift_i_label",
    "ai_result_transcription_title",
)
#: Sentences that name a tab, and the tab each one names.
TAB_REFERENCES = (
    ("tab_transcription", "transcription_open_settings_question"),
    ("tab_transcription", "transcription_error_external_model_changed"),
    ("tab_ai_accessibility", "ai_disabled"),
    ("tab_ai_accessibility", "ai_error_providers"),
)


def test_every_locale_has_markers():
    assert sorted(MARKERS) == sorted(LOCALES)


@pytest.mark.parametrize("locale", LOCALES)
class TestLocalAndOnlineNamesDiffer:
    def test_local_names_say_local(self, locale):
        words = MARKERS[locale][0]
        strings = load_strings(locale)
        for key in LOCAL_KEYS:
            assert any(w in strings[key].lower() for w in words), key

    def test_online_names_say_online(self, locale):
        words = MARKERS[locale][1]
        strings = load_strings(locale)
        for key in ONLINE_KEYS:
            assert any(w in strings[key].lower() for w in words), key

    def test_the_two_menu_items_and_the_two_tabs_are_not_the_same_text(self, locale):
        strings = load_strings(locale)
        assert strings["transcribe_message"] != strings["ai_transcribe_audio_menu"]
        assert strings["tab_transcription"] != strings["tab_ai_accessibility"]

    def test_sentences_pointing_at_a_tab_use_its_current_name(self, locale):
        strings = load_strings(locale)
        for tab_key, sentence_key in TAB_REFERENCES:
            assert strings[tab_key] in strings[sentence_key], sentence_key
