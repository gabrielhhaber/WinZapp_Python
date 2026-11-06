"""Saying what happened, without saying anything that did not.

Part 6a turns a run's codes into i18n keys; part 6b only reads them out. Which
makes this the last place a false sentence can be caught, and three of them are
specific enough to be pinned:

* **"You asked for the processor", said to somebody who did not.**
  `device.device_reason_i18n_key()` answers that for every reason it does not
  recognise, None included — correct where it lives, since an unknown reason
  still ran somewhere. Read at the start of a run, before the hardware probe,
  both the device and the reason are None, and taking that answer at face value
  has the app confidently name a decision nobody made on a machine with a
  working graphics card.

* **A transcription produced without the voice filter, looking exactly like
  one produced with it.** `vad_used=False` means Whisper answered the silence
  at the end of a voice note with an invented sentence in the same voice as the
  real ones. There is no symptom, so there has to be a warning — unconditional,
  including on a result that is otherwise perfect and including on one with
  nothing in it.

* **A sentence naming a choice the user was never offered.** The language note
  fires whenever the detected language differs from `preferences.
  preferred_language()` — and on factory settings that answer is the interface
  language, resolved from the `interface` sentinel without consulting
  `auto_detect_language`, which is on, which is what greys the language
  combobox out in the tab. So the user who has never opened that dialog is
  precisely the one who hears this note most often, and the sentence has to be
  true for them too.

* **An empty window, which a screen-reader user cannot tell from a crash.**

Nothing here imports wx and nothing formats a string: every assertion is about
which key, with which values, in which order.
"""

import re

from tests.locales import load_strings, registered_locale_codes
import pytest

from core.transcription import (
    backend as backend_module,
    device,
    errors,
    job,
    management,
    narration,
    preferences,
)
from tests.conftest import words_found_in


def _result(text="olá", language="pt", probability=0.98, vad_used=True):
    return backend_module.TranscriptionResult(
        text=text,
        language=language,
        language_probability=probability,
        duration_seconds=4.0,
        vad_used=vad_used,
    )


def _keys(notes):
    return [note.i18n_key for note in notes]


def _locales():
    return list(registered_locale_codes())


def _translations(locale):
    return load_strings(locale)


# ── While it runs ────────────────────────────────────────────────────────────


class TestPhaseWording:
    """Each wait is announced before it starts, because it is a wait.

    Converting, loading and decoding are three silences a blind user cannot
    tell from a crash — and loading large-v3 is the longest of them, with no
    fraction to report.
    """

    @pytest.mark.parametrize(
        "phase",
        [job.PHASE_PREPARING_AUDIO, job.PHASE_LOADING_MODEL, job.PHASE_TRANSCRIBING],
    )
    def test_every_running_phase_has_a_sentence(self, phase):
        assert narration.phase_i18n_key(phase)

    @pytest.mark.parametrize(
        "phase", [job.PHASE_DONE, job.PHASE_CANCELLED, job.PHASE_FAILED]
    )
    def test_a_terminal_phase_leaves_the_talking_to_the_outcome(self, phase):
        """Otherwise the user hears "transcribing" and "cancelled" together."""
        assert narration.phase_i18n_key(phase) is None

    @pytest.mark.parametrize("phase", [None, "", "rewinding_the_tape"])
    def test_a_phase_nobody_taught_it_says_nothing_rather_than_its_code(self, phase):
        """I18n.t() falls back to the raw key name, so an invented key is a
        screen reader spelling out `transcription_phase_rewinding_the_tape`."""
        assert narration.phase_i18n_key(phase) is None

    def test_every_phase_the_job_can_enter_is_accounted_for(self):
        """A phase added to job.py without a decision here would be silence."""
        decided = set(narration.PHASE_I18N_KEYS) | set(job.TERMINAL_PHASES)
        assert set(job.PHASES) == decided


class TestDeviceAnnouncement:
    """Which model, and which processor — the issue asks for both out loud."""

    def test_the_card_is_named_with_the_model(self):
        notes = narration.device_announcement(
            device.DEVICE_CUDA, device.REASON_CUDA_SELECTED, "large-v3"
        )
        assert _keys(notes) == ["transcription_running_on_cuda"]
        assert notes[0].values == {"model": "large-v3"}

    def test_the_processor_is_named_with_the_model(self):
        notes = narration.device_announcement(
            device.DEVICE_CPU, device.REASON_CPU_REQUESTED, "small"
        )
        assert _keys(notes) == ["transcription_running_on_cpu"]
        assert notes[0].values == {"model": "small"}

    def test_an_unknown_device_says_nothing_at_all(self):
        """Read from inside the phase callback, `job.device` is None until the
        probe has run — and a sentence about a processor nobody has chosen yet
        would be invented."""
        assert narration.device_announcement(None, None, "small") == ()

    def test_a_reason_of_none_never_becomes_you_asked_for_the_processor(self):
        """The trap part 3 already paid for once, in device_reason_i18n_key().

        Its fallback is deliberate where it lives; taken literally here it
        tells a user with a working card that they requested the processor.
        """
        cpu_requested = device.device_reason_i18n_key(None)
        assert cpu_requested == "transcription_device_cpu_requested"
        for device_id in (None, device.DEVICE_CPU, device.DEVICE_CUDA):
            notes = narration.device_announcement(device_id, None, "small")
            assert cpu_requested not in _keys(notes)

    def test_a_card_that_could_not_be_used_is_explained(self):
        notes = narration.device_announcement(
            device.DEVICE_CPU, device.REASON_CUDA_LIBRARIES_MISSING, "small"
        )
        assert _keys(notes) == [
            "transcription_running_on_cpu",
            device.device_reason_i18n_key(device.REASON_CUDA_LIBRARIES_MISSING),
        ]

    @pytest.mark.parametrize(
        "reason",
        [device.REASON_CUDA_UNAVAILABLE, device.REASON_CUDA_DRIVER_ERROR,
         device.REASON_CUDA_LIBRARIES_MISSING],
    )
    def test_every_actionable_reason_reaches_the_user(self, reason):
        notes = narration.device_announcement(device.DEVICE_CPU, reason, "small")
        assert device.device_reason_i18n_key(reason) in _keys(notes)

    @pytest.mark.parametrize(
        "reason",
        [device.REASON_CUDA_SELECTED, device.REASON_CPU_REQUESTED,
         device.REASON_NO_CUDA_FOUND],
    )
    def test_a_reason_that_adds_nothing_is_not_repeated(self, reason):
        """The first two restate the sentence above them. The third would tell
        a user who chose "automatic" that a request they never made was denied
        — device.py's own note on REASON_NO_CUDA_FOUND."""
        notes = narration.device_announcement(device.DEVICE_CPU, reason, "small")
        assert len(notes) == 1

    def test_the_backend_is_named_when_it_is_given(self):
        notes = narration.device_announcement(
            device.DEVICE_CUDA, device.REASON_CUDA_SELECTED, "small, 5 bits",
            backend_name="whisper.cpp",
        )
        assert _keys(notes) == ["transcription_running_with_backend_on_cuda"]
        assert notes[0].values == {"model": "small, 5 bits", "backend": "whisper.cpp"}

    @pytest.mark.parametrize("reason", [device.REASON_CUDA_BUILD_UNSUPPORTED,
                                        device.REASON_CUDA_BUILD_MISSING])
    def test_a_card_whisper_cpp_does_not_use_is_explained(self, reason):
        # Under "automatic" too: the user has a card and it is idle.
        notes = narration.device_announcement(
            device.DEVICE_CPU, reason, "small", backend_name="whisper.cpp"
        )
        assert _keys(notes) == ["transcription_running_with_backend_on_cpu",
                                device.device_reason_i18n_key(reason)]

    def test_a_single_language_model_says_it_runs_in_its_language(self):
        notes = narration.device_announcement(
            device.DEVICE_CPU, None, "small", forced_language="sv"
        )
        assert _keys(notes)[-1] == narration.LANGUAGE_FORCED_I18N_KEY
        # The endonym, as every other sentence naming a language says it.
        assert notes[-1].values == {"language": preferences.language_name("sv")}

    def test_a_language_it_replaced_is_named_too(self):
        notes = narration.device_announcement(
            device.DEVICE_CPU, None, "small.en", forced_language="en",
            overridden_language="pt",
        )
        assert _keys(notes)[-1] == narration.LANGUAGE_OVERRIDDEN_I18N_KEY
        assert notes[-1].values == {"language": preferences.language_name("en"),
                                    "chosen": preferences.language_name("pt")}

    def test_nothing_forced_is_nothing_said(self):
        notes = narration.device_announcement(
            device.DEVICE_CPU, None, "small", overridden_language="pt"
        )
        assert _keys(notes) == ["transcription_running_on_cpu"]

    def test_a_missing_model_id_does_not_break_the_sentence(self):
        """`{model}` is formatted whatever happens; an empty one reads badly,
        a KeyError reads not at all."""
        notes = narration.device_announcement(device.DEVICE_CPU, None, None)
        assert notes[0].values == {"model": ""}


# ── Once it has finished ─────────────────────────────────────────────────────


class TestResultNotes:
    def test_a_normal_result_has_nothing_to_warn_about(self):
        assert narration.result_notes(_result()) == ()

    def test_no_speech_is_announced_rather_than_shown_as_an_empty_window(self):
        assert _keys(narration.result_notes(_result(text="   "))) == [
            narration.NO_SPEECH_I18N_KEY
        ]

    def test_a_missing_filter_is_always_announced(self):
        """Not negotiable. A result produced without the voice filter may end
        in a sentence nobody said, and it looks exactly like one that does not."""
        notes = narration.result_notes(_result(vad_used=False))
        assert narration.VAD_UNAVAILABLE_I18N_KEY in _keys(notes)

    def test_a_missing_filter_is_announced_even_for_an_empty_result(self):
        """The one note that survives every other suppression rule here."""
        notes = narration.result_notes(_result(text="", vad_used=False))
        assert _keys(notes) == [
            narration.NO_SPEECH_I18N_KEY, narration.VAD_UNAVAILABLE_I18N_KEY
        ]

    def test_a_missing_filter_is_announced_alongside_every_other_note(self):
        notes = narration.result_notes(
            _result(language="es", probability=0.2, vad_used=False),
            preferred_language="pl",
        )
        assert narration.VAD_UNAVAILABLE_I18N_KEY in _keys(notes)

    def test_a_filter_that_ran_is_not_mentioned(self):
        assert narration.VAD_UNAVAILABLE_I18N_KEY not in _keys(
            narration.result_notes(_result(vad_used=True))
        )

    def test_a_language_other_than_the_preferred_one_is_named_by_its_endonym(self):
        """"pl" is what a screen reader spells out letter by letter; "polski"
        is what a Polish user recognises."""
        notes = narration.result_notes(_result(language="es"), preferred_language="pl")
        assert _keys(notes) == [narration.LANGUAGE_DIFFERS_I18N_KEY]
        assert notes[0].values == {"language": preferences.language_name("es")}
        assert notes[0].values == {"language": "español"}

    def test_the_language_the_user_wanted_is_not_worth_saying(self):
        assert narration.result_notes(
            _result(language="pl"), preferred_language="pl"
        ) == ()

    def test_no_preference_means_no_comparison(self):
        """`preferred_language()` answers None whenever the user has not chosen
        one that Whisper knows — there is nothing for a mismatch to be against."""
        assert narration.result_notes(_result(language="es")) == ()

    def test_a_language_the_table_cannot_name_is_left_unmentioned(self):
        """Naming it by its code would have the code read out; the low
        confidence note below is what still fires for an unsure detection."""
        notes = narration.result_notes(
            _result(language="xx"), preferred_language="pl"
        )
        assert narration.LANGUAGE_DIFFERS_I18N_KEY not in _keys(notes)

    def test_an_unsure_detection_is_flagged(self):
        notes = narration.result_notes(_result(probability=0.31))
        assert _keys(notes) == [narration.LOW_CONFIDENCE_I18N_KEY]

    def test_a_confident_detection_is_not(self):
        assert narration.result_notes(_result(probability=0.93)) == ()

    def test_the_threshold_itself_is_the_boundary(self):
        """Pinned so the value cannot drift into never firing — a warning that
        does not fire protects nobody."""
        just_below = narration.LOW_CONFIDENCE_THRESHOLD - 0.01
        assert _keys(narration.result_notes(_result(probability=just_below))) == [
            narration.LOW_CONFIDENCE_I18N_KEY
        ]
        assert narration.result_notes(
            _result(probability=narration.LOW_CONFIDENCE_THRESHOLD)
        ) == ()

    def test_an_unmeasured_confidence_is_not_a_low_one(self):
        """A backend that reports no probability has not reported a bad one."""
        assert narration.result_notes(_result(probability=None)) == ()

    def test_an_unsure_detection_of_a_nameless_language_still_warns(self):
        notes = narration.result_notes(_result(language="xx", probability=0.1))
        assert _keys(notes) == [narration.LOW_CONFIDENCE_I18N_KEY]

    def test_an_empty_result_is_not_described_as_being_in_a_language(self):
        """Whisper reports a language for a note of pure noise too. Passing it
        on would describe content that does not exist."""
        notes = narration.result_notes(
            _result(text="", language="fi", probability=0.1),
            preferred_language="pl",
        )
        assert _keys(notes) == [narration.NO_SPEECH_I18N_KEY]

    def test_the_notes_come_in_the_order_they_should_be_spoken(self):
        notes = narration.result_notes(
            _result(language="es", probability=0.2, vad_used=False),
            preferred_language="pl",
        )
        assert _keys(notes) == [
            narration.VAD_UNAVAILABLE_I18N_KEY,
            narration.LANGUAGE_DIFFERS_I18N_KEY,
            narration.LOW_CONFIDENCE_I18N_KEY,
        ]

    def test_no_result_is_not_a_crash(self):
        assert narration.result_notes(None) == ()


#: Words that would put a choice back into the language note. Not a style
#: check and not a spell check — these are the verbs the first draft of this
#: sentence used ("…and not in the language you chose"), which is the exact
#: regression below. It has to be a word list because the sentence itself is
#: what is being asserted about: the key, the placeholder and the endonym are
#: all identical between a wording that claims a choice and one that does not.
_CLAIMS_A_CHOICE = {
    "pt-BR": ("escolh",),
    "pt-PT": ("escolh",),
    "en-US": ("chose", "chosen", "choose", "picked", "selected"),
    "es-ES": ("elig", "eleg", "escog"),
    "pl": ("wybra", "wybór", "wybor"),
    "ro": ("ales", "alege", "aleg", "selectat", "prefer"),
    "tr-TR": ("seç", "tercih"),
    "ar": ("اخترت", "اختيار", "اختر", "حددت"),
    "it": ("scelt", "scegli", "selezionat"),
}


#: The word each locale uses for "text", for the voice-filter note below. Keyed
#: by locale, like _CLAIMS_A_CHOICE, so a locale added to language_map fails
#: loudly instead of passing unchecked. Turkish drops a vowel when the noun
#: takes a suffix ("metin" → "metni", "metne"), hence the second stem.
_TEXT_WORDS = {
    "pt-BR": ("texto",),
    "pt-PT": ("texto",),
    "en-US": ("text",),
    "es-ES": ("texto",),
    "pl": ("tekst",),
    "ro": ("text",),
    "tr-TR": ("metin", "metn"),
    # "النصي" (textual) names the feature, so only the noun forms are listed.
    "ar": ("نصًا", "نصوص", "نصه"),
    "it": ("testo",),
}


class TestTheLanguageNoteOnUntouchedSettings:
    """The reporter's own case: Polish interface, nothing configured.

    He is told the transcription came out in a language other than the one he
    chose, on an install where the control that would have let him choose was
    greyed out the whole time — and the issue says he receives messages in
    several languages, so it fires on most of what he transcribes.

    The comparison is not the bug and is not removed: a detected language
    differing from the interface language is genuinely the thing worth
    mentioning. The claim about where the preference came from is the bug.
    """

    @pytest.mark.parametrize("settings", [
        {},
        {"transcription": dict(preferences.DEFAULTS)},
    ])
    def test_factory_settings_resolve_a_preference_nobody_expressed(self, settings):
        """`SETTING_LANGUAGE` defaults to the `interface` sentinel and
        `preferred_language()` resolves it without looking at the detection
        flag, which defaults to on — so there is always a preference to differ
        from, chosen or not."""
        section = preferences.read_section(settings)
        assert section[preferences.SETTING_LANGUAGE] == preferences.LANGUAGE_INTERFACE
        assert section[preferences.SETTING_AUTO_DETECT_LANGUAGE] is True
        assert preferences.preferred_language(settings, "pl") == "pl"

    def test_the_note_fires_on_settings_the_user_has_never_opened(self):
        """Which is what obliges the sentence to be true in that state."""
        preferred = preferences.preferred_language({}, "pl")
        notes = narration.result_notes(_result(language="pt"), preferred_language=preferred)
        assert _keys(notes) == [narration.LANGUAGE_DIFFERS_I18N_KEY]
        assert notes[0].values == {"language": "português"}

    @pytest.mark.parametrize("locale", _locales())
    def test_the_sentence_claims_no_choice_in_any_language(self, locale):
        assert locale in _CLAIMS_A_CHOICE, (
            f"{locale} is new here: decide which of its words would claim a "
            "choice and add them, rather than letting the note go unchecked"
        )
        text = _translations(locale)[narration.LANGUAGE_DIFFERS_I18N_KEY]
        offenders = words_found_in(text, _CLAIMS_A_CHOICE[locale])
        assert offenders == [], (
            f"{locale} catalog tells the user this differs from a language they "
            f"chose, which on default settings they did not: {offenders}"
        )


class TestOutcomeAnnouncement:
    def test_a_finished_transcription_says_so(self):
        announcement = narration.outcome_announcement(result=_result())
        assert announcement.i18n_key == narration.FINISHED_I18N_KEY
        assert announcement.outcome == management.OUTCOME_DONE

    def test_an_empty_result_is_a_warning_rather_than_a_success(self):
        announcement = narration.outcome_announcement(result=_result(text=""))
        assert announcement.i18n_key == narration.NO_SPEECH_I18N_KEY
        assert announcement.outcome == management.OUTCOME_WARNING

    def test_the_empty_headline_and_the_empty_note_are_the_same_sentence(self):
        """One key, one translation — and the deduplication rule part 6b needs
        is then `note.i18n_key == announcement.i18n_key`, with nothing to
        remember."""
        result = _result(text="")
        assert narration.result_notes(result)[0].i18n_key == (
            narration.outcome_announcement(result=result).i18n_key
        )

    def test_a_cancellation_is_its_own_outcome(self):
        """Not a failure: the user did it on purpose, and the tab should not
        play the failure sound at somebody who pressed Escape."""
        error = errors.TranscriptionError(errors.CANCELLED, "by the user")
        announcement = narration.outcome_announcement(error=error)
        assert announcement.outcome == management.OUTCOME_CANCELLED
        assert announcement.i18n_key == errors.error_i18n_key(errors.CANCELLED)

    @pytest.mark.parametrize("code", errors.ERROR_CODES)
    def test_every_error_code_reaches_the_user_as_its_own_sentence(self, code):
        """Through errors.error_i18n_key(), never through a second copy of the
        map — two copies is how the two start disagreeing."""
        announcement = narration.outcome_announcement(
            error=errors.TranscriptionError(code, "technical")
        )
        assert announcement.i18n_key == errors.error_i18n_key(code)

    def test_an_unrecognised_code_degrades_to_the_internal_error(self):
        announcement = narration.outcome_announcement(
            error=errors.TranscriptionError("something_new", "technical")
        )
        assert announcement.i18n_key == errors.error_i18n_key(errors.BACKEND_ERROR)
        assert announcement.outcome == management.OUTCOME_FAILED

    def test_neither_a_result_nor_an_error_is_not_reported_as_success(self):
        announcement = narration.outcome_announcement()
        assert announcement.outcome == management.OUTCOME_FAILED

    def test_the_technical_detail_never_becomes_the_sentence(self):
        """`detail` is a CTranslate2 message or an ffmpeg tail, usually with a
        path in it, and `wx.MessageBox(str(exc))` is an idiom in this repo."""
        error = errors.TranscriptionError(
            errors.BACKEND_ERROR, "C:/Users/someone/media/ABCDEF.wzmedia is bad"
        )
        announcement = narration.outcome_announcement(error=error)
        assert "ABCDEF" not in announcement.i18n_key
        assert announcement.values == {}

    def test_a_full_temp_disk_names_the_drive_and_only_the_drive(self, monkeypatch):
        """The only field any error sentence has — and the settings tab's
        management.announcement() passes the same one, through the same
        helper."""
        monkeypatch.setattr(errors.tempfile, "gettempdir",
                            lambda: r"C:\Users\Ana Souza\AppData\Local\Temp")
        error = errors.TranscriptionError(errors.TEMP_NO_DISK_SPACE, "rc=1: ...")
        announcement = narration.outcome_announcement(error=error)
        assert announcement.values == {"drive": "C:"}
        assert management.announcement(
            management.ACTION_DOWNLOAD_MODEL, error=error).values == {"drive": "C:"}
        text = _translations("pl")[announcement.i18n_key].format(**announcement.values)
        assert "C:" in text and "Ana Souza" not in text and "Users" not in text


class TestCpuRetryOffer:
    def test_a_vram_failure_is_worth_offering_again(self):
        note = narration.cpu_retry_note(
            errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "oom"),
            device.DEVICE_CUDA,
        )
        assert note.i18n_key == "transcription_retry_on_cpu_vram"

    def test_a_failure_the_processor_would_hit_too_is_not_offered(self):
        """Re-running a forty-minute recording that fails identically costs the
        whole wait twice and teaches the user to dismiss the offer."""
        assert narration.cpu_retry_note(
            errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "absent"),
            device.DEVICE_CUDA,
        ) is None

    def test_a_run_that_was_already_on_the_processor_has_nothing_to_fall_back_to(self):
        assert narration.cpu_retry_note(
            errors.TranscriptionError(errors.INSUFFICIENT_VRAM, "oom"),
            device.DEVICE_CPU,
        ) is None


# ── The language files ──────────────────────────────────────────────────────


class TestEveryKeyIsTranslated:
    """A key missing from a locale reaches the user as its own name.

    I18n.t() is `translations.get(key, key)` with no per-key fallback, so
    `transcription_note_vad_unavailable` is what the screen reader would say —
    in place of the one warning this feature cannot do without.
    """

    @pytest.mark.parametrize("locale", _locales())
    def test_the_locale_defines_every_narration_key(self, locale):
        table = _translations(locale)
        missing = sorted(k for k in narration.NARRATION_I18N_KEYS if k not in table)
        assert missing == [], f"{locale} catalog is missing: {missing}"

    @pytest.mark.parametrize("locale", _locales())
    def test_the_device_sentence_takes_the_model_the_code_passes(self, locale):
        table = _translations(locale)
        for key in narration.DEVICE_I18N_KEYS.values():
            assert re.findall(r"\{(\w+)\}", table[key]) == ["model"], f"{locale}: {key}"

    @pytest.mark.parametrize("locale", _locales())
    def test_the_sentences_naming_the_backend_take_what_the_code_passes(self, locale):
        table = _translations(locale)
        for key in narration.DEVICE_WITH_BACKEND_I18N_KEYS.values():
            assert sorted(re.findall(r"\{(\w+)\}", table[key])) == ["backend", "model"], (
                f"{locale}: {key}")
        # The language may be said twice ("only understands X, so ... in X").
        assert set(re.findall(r"\{(\w+)\}", table[narration.LANGUAGE_FORCED_I18N_KEY])) == {
            "language"}, locale
        assert set(re.findall(
            r"\{(\w+)\}", table[narration.LANGUAGE_OVERRIDDEN_I18N_KEY])) == {
            "chosen", "language"}, locale

    @pytest.mark.parametrize("locale", _locales())
    def test_the_language_note_takes_the_language_the_code_passes(self, locale):
        table = _translations(locale)
        text = table[narration.LANGUAGE_DIFFERS_I18N_KEY]
        assert re.findall(r"\{(\w+)\}", text) == ["language"], locale

    def test_polish_does_not_ask_the_endonym_to_decline(self):
        """The endonym arrives in the nominative ("polski", "angielski"), and a
        Polish preposition governs a case: "w języku {language}" wants the
        locative ("w języku polskim") and reads every language wrongly. After
        a colon it is a label, and a label takes no case at all."""
        text = _translations("pl")[narration.LANGUAGE_DIFFERS_I18N_KEY]
        assert re.search(r"\w\s+\{language\}", text) is None, text

    @pytest.mark.parametrize("locale", _locales())
    def test_the_missing_filter_sentence_describes_no_text(self, locale):
        """It fires on an empty result too — deliberately, that is the one note
        that survives every suppression rule — and there is no text there for a
        warning to be about. A sentence ending "the text may end with a phrase
        nobody said" read out over nothing is the app describing content it did
        not produce."""
        assert locale in _TEXT_WORDS, (
            f"{locale} is new here: add the word it uses for a text to _TEXT_WORDS"
        )
        text = _translations(locale)[narration.VAD_UNAVAILABLE_I18N_KEY]
        offenders = words_found_in(text, _TEXT_WORDS[locale])
        assert offenders == [], (
            f"{locale} catalog describes a transcribed text this note also fires "
            f"without: {offenders}"
        )

    @pytest.mark.parametrize("locale", _locales())
    def test_the_sentences_with_nothing_to_fill_in_ask_for_nothing(self, locale):
        """A placeholder no call site passes raises KeyError at format() time,
        in the middle of announcing a result."""
        table = _translations(locale)
        for key in (narration.NO_SPEECH_I18N_KEY,
                    narration.VAD_UNAVAILABLE_I18N_KEY,
                    narration.LOW_CONFIDENCE_I18N_KEY,
                    narration.FINISHED_I18N_KEY) + tuple(
                        narration.PHASE_I18N_KEYS.values()):
            assert re.findall(r"\{(\w+)\}", table[key]) == [], f"{locale}: {key}"


def test_nothing_here_imports_wx():
    """Part 6a is the half that is testable without a wx.App, and stays so."""
    with open(narration.__file__, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert re.search(r"^\s*(import wx|from wx)", source, re.M) is None
