"""What the transcription settings mean, and what a stale one must not cost.

A settings.json outlives the version that wrote it, and every value in this
section names something that can stop existing: a model the catalogue retires,
a backend an install does not have, a language code that was never valid, a
device string from a WinZapp that spelled them differently. None of those is a
fault, so none of them may raise — but a silent swap is its own bug, and a
worse one for a blind user, who has no visual cue that the model actually
running is not the one the dialog is showing. So every fallback here is pinned
twice: that it happens, and that it is *reported*.

The two decisions that are easy to get backwards:

* **The interface language is a preference, never an override.** The issue asks
  for the configured language to be preferred, and the tempting reading — "the
  app is in Polish, so transcribe in Polish" — is the wrong one. Whisper handed
  a language the audio is not in does not fail; it confidently invents a
  transcription in that language, and a listener cannot tell that from a real
  one. The reporter runs the app in Polish and receives voice notes in other
  languages, so the default (detect each message on its own) must survive a
  Polish interface untouched. The interface language counts in exactly one
  place: the user turned detection off and never said which language instead.

* **The models folder is install-wide, and app_settings is what enforces it.**
  The files are shared by every account — one model is up to 3 GB, which is why
  they live under global_dir() — so an account pointing somewhere else while its
  siblings read the old folder is a state with no meaning. app_settings raises
  KeyError for anything that is not global, which is the mechanism that keeps
  this key out of a per-account settings.json by accident.

Nothing here probes anything: the hardware, the installed models and the usable
backends all arrive as arguments, which is what lets "what does a 4 GB card
get" be answered on a machine with no card at all.
"""

import json
import os

from tests.locales import load_strings, registered_locale_codes
import pytest

import app_settings
from core.transcription import backend as backend_module
from core.transcription import device, model_catalog, preferences, whisper_cpp_catalog
from core.utils import DEFAULT_SETTINGS


LOCALES = registered_locale_codes()

# A machine with plenty of RAM and no graphics card — the common case, and the
# one where every "automatic" has an unambiguous right answer.
_CPU_ONLY = device.HardwareProbe(total_ram_mb=16_384, available_ram_mb=12_288)

# The same machine with nothing measurable at all: auto_select_model() then
# refuses to guess upwards and only an installed model can be chosen.
_UNKNOWN = device.HardwareProbe()


def _settings(**values):
    """A settings dict carrying only the transcription keys under test."""
    return {"transcription": dict(values)}


def _substituted(resolution):
    return sorted(sub.setting for sub in resolution.substitutions)


class TestTheDefaultsAreOneThing:
    """Three copies of this section exist and all three ship to the user.

    `preferences.DEFAULTS` is what the resolver reads through, DEFAULT_SETTINGS
    is what bootstraps a missing settings.json, and settings_default.json is
    what seeds a new install. Two of them agreeing is not enough: whichever one
    is behind is the one a real user gets.
    """

    def test_default_settings_carries_the_section(self):
        assert DEFAULT_SETTINGS[preferences.SECTION] == preferences.DEFAULTS

    def test_the_seed_file_carries_the_same_section(self):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(base, "client", "data", "settings_default.json"),
                  "r", encoding="utf-8") as handle:
            seeded = json.load(handle)
        assert seeded[preferences.SECTION] == preferences.DEFAULTS

    def test_every_default_is_a_stored_automatic_rather_than_a_blank(self):
        """A blank could not be told from "the user picked what was default".

        That ambiguity is the whole reason core/utils.py needs one-shot
        migrations for the settings that were written that way; this section
        buys its way out of ever needing one by storing the sentinel.
        """
        assert preferences.DEFAULTS[preferences.SETTING_BACKEND] == preferences.AUTO
        assert preferences.DEFAULTS[preferences.SETTING_MODEL] == preferences.AUTO
        assert preferences.DEFAULTS[preferences.SETTING_DEVICE] == preferences.AUTO
        assert (preferences.DEFAULTS[preferences.SETTING_LANGUAGE]
                == preferences.LANGUAGE_INTERFACE)

    def test_the_detection_checkbox_is_the_one_exception_and_it_is_named(self):
        """`auto_detect_language` is a bool, and the rule above does not cover it.

        Granting it in silence is what this test used to do, with a bare `is
        True` at the end of the list above. The exception is deliberate: a
        checkbox has two positions and no third behaviour, so a stored "auto"
        would have to be mapped back onto one of them the first time the dialog
        was opened — the same ambiguity, one OK later. The price is real and
        accepted: this is the default most likely to be reconsidered, and the
        day it flips, True on disk cannot be told from never-touched. The
        answer then is a one-shot migration carrying its own flag, exactly like
        `migrate_voice_message_mode_default()`.
        """
        assert preferences.DEFAULTS[preferences.SETTING_AUTO_DETECT_LANGUAGE] is True
        stored_automatics = [
            key for key, value in preferences.DEFAULTS.items()
            if isinstance(value, str)
        ]
        assert sorted(stored_automatics) == sorted(
            set(preferences.DEFAULTS) - {preferences.SETTING_AUTO_DETECT_LANGUAGE,
                                         preferences.SETTING_KEEP_MODEL_LOADED,
                                         preferences.SETTING_AUTO_TRANSCRIBE_RECEIVED,
                                         preferences.SETTING_CACHE_ENABLED,
                                         preferences.SETTING_CACHE_BY_MODEL,
                                         # The status tab's own two switches
                                         # (core/transcription/statuses.py):
                                         # plain bools for the same reason the
                                         # conversation's two are.
                                         preferences.SETTING_AUTO_TRANSCRIBE_STATUSES,
                                         preferences.SETTING_STATUS_CACHE_ENABLED}
        ), "a third setting became an exception without saying why"

    def test_keeping_the_model_loaded_is_a_bool_and_starts_off(self):
        """The other checkbox of the section: off by default, so a model nobody
        asked to pin is let go after idle time (core/transcription/residency)."""
        assert preferences.DEFAULTS[preferences.SETTING_KEEP_MODEL_LOADED] is False
        assert preferences.DEFAULTS[preferences.SETTING_AUTO_TRANSCRIBE_RECEIVED] is False


class TestEveryAutomaticResolves:
    def test_a_fresh_install_resolves_to_the_automatic_of_everything(self):
        resolved = preferences.resolve({}, _CPU_ONLY, ui_language="pt-BR")

        assert resolved.backend_id == backend_module.BACKEND_IDS[0]
        assert resolved.device_preference == device.PREFERENCE_AUTO
        assert resolved.model_id == device.auto_select_model(
            _CPU_ONLY, device.DEVICE_CPU, ()
        )
        # Detection is on by default, so no language is forced — not even the
        # one the interface is in. See the module docstring.
        assert resolved.language is None
        assert resolved.substitutions == ()

    def test_a_settings_file_written_before_the_feature_reads_as_defaults(self):
        """No section at all is the state of every existing install."""
        assert preferences.read_section({}) == preferences.DEFAULTS
        assert preferences.read_section({"transcription": None}) == preferences.DEFAULTS
        assert preferences.read_section("not a dict") == preferences.DEFAULTS

    def test_an_installed_model_is_preferred_over_a_download(self):
        """auto_select_model()'s rule, reached through the settings path.

        A silent multi-gigabyte download nobody asked for is worse than
        transcribing with what is already on disk.
        """
        resolved = preferences.resolve(
            {}, _CPU_ONLY, installed_ids=("small",), ui_language="pt-BR"
        )
        assert resolved.model_id == "small"

    def test_no_model_installed_and_nothing_measurable_is_not_an_error(self):
        """This layer answers None; the settings tab turns that into an offer.

        Raising here would put a failure in front of a user who has not asked
        for a transcription yet.
        """
        resolved = preferences.resolve({}, _UNKNOWN, installed_ids=())
        assert resolved.model_id is None
        assert resolved.substitutions == ()

    def test_the_automatic_backend_is_the_first_one_that_can_run(self):
        resolved = preferences.resolve(
            {}, _CPU_ONLY, available_backends=(backend_module.BACKEND_FASTER_WHISPER,)
        )
        assert resolved.backend_id == backend_module.BACKEND_FASTER_WHISPER

    def test_no_usable_backend_resolves_to_none_rather_than_raising(self):
        resolved = preferences.resolve({}, _CPU_ONLY, available_backends=())
        assert resolved.backend_id is None
        assert resolved.substitutions == ()


class TestAStoredValueThatIsHonoured:
    """The other half: a valid choice must survive the fallback machinery."""

    def test_a_chosen_device_is_passed_through(self):
        resolved = preferences.resolve(
            _settings(device=device.PREFERENCE_CPU), _CPU_ONLY
        )
        assert resolved.device_preference == device.PREFERENCE_CPU
        assert resolved.substitutions == ()

    def test_a_chosen_model_wins_over_the_automatic_one(self):
        resolved = preferences.resolve(_settings(model="tiny"), _CPU_ONLY)
        assert resolved.model_id == "tiny"
        assert resolved.substitutions == ()

    def test_a_model_that_is_not_downloaded_yet_is_still_the_chosen_one(self):
        """Not installed is not the same as not existing.

        The first is MODEL_NOT_INSTALLED at run time and a download button in
        the settings; swapping it for another model here would quietly cancel
        the choice the user made and never mention it.
        """
        resolved = preferences.resolve(
            _settings(model="large-v3"), _CPU_ONLY, installed_ids=("tiny",)
        )
        assert resolved.model_id == "large-v3"
        assert resolved.substitutions == ()

    def test_a_chosen_backend_is_kept_when_it_can_run(self):
        resolved = preferences.resolve(
            _settings(backend=backend_module.BACKEND_FASTER_WHISPER),
            _CPU_ONLY,
            available_backends=(backend_module.BACKEND_FASTER_WHISPER,),
        )
        assert resolved.backend_id == backend_module.BACKEND_FASTER_WHISPER
        assert resolved.substitutions == ()


class TestEveryStaleValueFallsBackAndSaysSo:
    """One test per way a stored value can stop existing.

    Each has to do both things — resolve to the automatic value *and* report
    the substitution — because doing only the first is the bug where the model
    running is not the model the dialog is showing.
    """

    def test_a_retired_model_id(self):
        resolved = preferences.resolve(
            _settings(model="whisper-from-a-later-version"), _CPU_ONLY
        )
        assert resolved.model_id == device.auto_select_model(
            _CPU_ONLY, device.DEVICE_CPU, ()
        )
        assert _substituted(resolved) == [preferences.SETTING_MODEL]

    def test_a_language_code_that_does_not_exist(self):
        resolved = preferences.resolve(
            _settings(auto_detect_language=False, language="zz"), _CPU_ONLY,
            ui_language="pl",
        )
        # Detection, not "some other language": guessing a second one would be
        # the same confident-nonsense failure one step removed.
        assert resolved.language is None
        assert _substituted(resolved) == [preferences.SETTING_LANGUAGE]

    def test_a_backend_this_install_does_not_have(self):
        resolved = preferences.resolve(
            _settings(backend="whisper_cpp"), _CPU_ONLY,
            available_backends=(backend_module.BACKEND_FASTER_WHISPER,),
        )
        assert resolved.backend_id == backend_module.BACKEND_FASTER_WHISPER
        assert _substituted(resolved) == [preferences.SETTING_BACKEND]

    def test_a_backend_nothing_here_can_run(self):
        resolved = preferences.resolve(
            _settings(backend="whisper_cpp"), _CPU_ONLY, available_backends=()
        )
        assert resolved.backend_id is None
        assert _substituted(resolved) == [preferences.SETTING_BACKEND]

    def test_an_unmeasured_machine_only_rejects_a_backend_nobody_knows(self):
        """`available_backends=None` means "nobody asked", not "none work".

        Refusing a backend on a measurement that was never taken would invent
        an obstacle, which for the user is a swap with no cause.
        """
        resolved = preferences.resolve(
            _settings(backend=backend_module.BACKEND_FASTER_WHISPER), _CPU_ONLY
        )
        assert resolved.backend_id == backend_module.BACKEND_FASTER_WHISPER
        assert resolved.substitutions == ()

    def test_an_unknown_device_string(self):
        """resolve_device() already treats this as auto — silently.

        The substitution is the whole point of checking it here again.
        """
        resolved = preferences.resolve(_settings(device="npu"), _CPU_ONLY)
        assert resolved.device_preference == device.PREFERENCE_AUTO
        assert _substituted(resolved) == [preferences.SETTING_DEVICE]

    def test_every_one_at_once(self):
        resolved = preferences.resolve(
            _settings(
                backend="whisper_cpp",
                model="gone",
                device="npu",
                compute_type="int4",
                language="zz",
                auto_detect_language=False,
            ),
            _CPU_ONLY,
            ui_language="pl",
            available_backends=(),
        )
        assert _substituted(resolved) == sorted(preferences.SUBSTITUTION_I18N_KEYS)
        assert resolved.substituted is True

    def test_a_substitution_reports_what_was_stored_for_the_log_only(self):
        """The id travels for log.log; the sentence for the user is the key.

        Same split as TranscriptionError's code/detail — "large-v3" read out to
        a blind user says nothing they can act on.
        """
        resolved = preferences.resolve(_settings(model="gone"), _CPU_ONLY)
        substitution = resolved.substitutions[0]
        assert substitution.stored == "gone"
        assert substitution.i18n_key == "transcription_substituted_model"

    def test_a_corrupt_detection_flag_falls_back_to_the_default(self):
        """Not reported: a checkbox holding nonsense carries no user intent,
        and the default is also the safe answer."""
        resolved = preferences.resolve(
            _settings(auto_detect_language="yes please", language="es"), _CPU_ONLY
        )
        assert resolved.language is None
        assert resolved.substitutions == ()

    @pytest.mark.parametrize("stored", [["pl"], {"a": 1}, 3, None])
    @pytest.mark.parametrize(
        "setting",
        [preferences.SETTING_BACKEND, preferences.SETTING_MODEL,
         preferences.SETTING_DEVICE, preferences.SETTING_LANGUAGE],
    )
    def test_a_stored_value_that_is_not_even_a_string(self, setting, stored):
        """The promise is that *nothing* on disk raises, not that no string does.

        A settings.json is a plain JSON file: a hand edit, a bad merge or the
        version-that-outlived-its-writer this module is built around can leave
        a list or a dict under any of these keys. Two of the four resolvers
        used to answer that with `TypeError: unhashable type` — a membership
        test against a dict — which in the settings tab is a wx handler that
        never opens the dialog and, once part 6 exists, a generic
        BACKEND_ERROR out of the job's own `except Exception`. Both of them
        blame the wrong thing, and neither hints at the file that caused it.
        """
        resolved = preferences.resolve(
            _settings(auto_detect_language=False, **{setting: stored}),
            _CPU_ONLY,
            ui_language="pl",
        )
        assert _substituted(resolved) == [setting]


class TestTheInterfaceLanguageIsAPreferenceNotAnOverride:
    def test_detection_stays_on_through_a_polish_interface(self):
        """The case where the priority must NOT apply, and the reported one.

        The reporter runs WinZapp in Polish and receives voice notes in other
        languages. Forcing "pl" on those would not fail — Whisper would invent
        a Polish transcription of Spanish audio, and nothing about the result
        would look wrong.
        """
        resolved = preferences.resolve({}, _CPU_ONLY, ui_language="pl")
        assert resolved.language is None
        assert resolved.substitutions == ()

    def test_turning_detection_off_without_picking_uses_the_app_language(self):
        resolved = preferences.resolve(
            _settings(auto_detect_language=False), _CPU_ONLY, ui_language="pl"
        )
        assert resolved.language == "pl"
        assert resolved.substitutions == ()

    def test_the_region_tag_is_dropped(self):
        """WinZapp's locales are region-tagged and Whisper's are not; both
        Portuguese locales are the same model."""
        for ui_language in ("pt-BR", "pt-PT"):
            resolved = preferences.resolve(
                _settings(auto_detect_language=False), _CPU_ONLY,
                ui_language=ui_language,
            )
            assert resolved.language == "pt", ui_language

    def test_an_explicit_language_beats_the_interface_one(self):
        resolved = preferences.resolve(
            _settings(auto_detect_language=False, language="en"), _CPU_ONLY,
            ui_language="pl",
        )
        assert resolved.language == "en"
        assert resolved.substitutions == ()

    def test_an_interface_language_whisper_has_no_model_for(self):
        """A locale WinZapp could gain tomorrow. Detection, and say so — never
        a refusal, and never some other language."""
        resolved = preferences.resolve(
            _settings(auto_detect_language=False), _CPU_ONLY, ui_language="zz-ZZ"
        )
        assert resolved.language is None
        assert _substituted(resolved) == [preferences.SETTING_LANGUAGE]

    def test_no_interface_language_at_all_is_not_reported_as_a_substitution(self):
        """The caller passed nothing; the user's own value is still intact.

        Detection either way, but "the language you chose is not available"
        spoken to somebody who never chose a language is a warning about a
        fault one layer up, read out to the one person who cannot act on it.
        """
        resolved = preferences.resolve(
            _settings(auto_detect_language=False), _CPU_ONLY, ui_language=""
        )
        assert resolved.language is None
        assert resolved.substitutions == ()

    def test_the_sentinel_is_stored_rather_than_the_resolved_code(self):
        """Which is what makes the choice follow a later change of language.

        A "pl" written into settings.json the day the user happened to be on
        Polish would stay Polish forever after they switched.
        """
        stored = preferences.DEFAULTS[preferences.SETTING_LANGUAGE]
        assert stored == preferences.LANGUAGE_INTERFACE
        assert stored not in preferences.LANGUAGE_NAMES


class TestThePreferenceSurvivesDetectionBeingOn:
    """`resolve()` answers None while detection is on, so it cannot carry one.

    Both answers fail quietly, which is why both halves have to stay reachable.
    Forcing the interface language is the worse failure and the class above
    pins it. But detection is not the safe opposite: Whisper's identifier reads
    only the first 30 s window and gets a short, noisy voice note wrong often
    enough that the reporter's own case is the common one, and a listener
    cannot tell an invented language from a real one either way. Part 6 needs
    the preference in hand to *say* that a message came out in another
    language; a later part needs it to weigh against
    `TranscriptionResult.language_probability`. Neither can ask
    `Resolution.language`, which is None exactly then.
    """

    def test_the_interface_language_is_the_preference_by_default(self):
        assert preferences.preferred_language({}, "pl") == "pl"
        assert preferences.preferred_language({}, "pt-PT") == "pt"

    def test_it_is_answered_even_while_detection_is_on(self):
        settings = _settings(auto_detect_language=True)
        assert preferences.resolve(settings, _CPU_ONLY, ui_language="pl").language is None
        assert preferences.preferred_language(settings, "pl") == "pl"

    def test_an_explicit_choice_is_the_preference(self):
        assert preferences.preferred_language(_settings(language="es"), "pl") == "es"

    def test_an_unusable_value_has_no_preference_rather_than_a_guess(self):
        for stored in ("zz", ["pl"], {"a": 1}, 3, None):
            assert preferences.preferred_language(_settings(language=stored), "pl") \
                is None, stored

    def test_no_interface_language_and_the_sentinel_means_no_preference(self):
        assert preferences.preferred_language({}, "") is None
        assert preferences.preferred_language({}, "zz-ZZ") is None

    def test_it_never_reaches_the_resolution(self):
        """Deliberately not a field: a field there is a decision the job acts
        on, and a preference is not one."""
        resolved = preferences.resolve({}, _CPU_ONLY, ui_language="pl")
        assert not hasattr(resolved, "preferred_language")


class TestWhyThereIsNoModel:
    """`model_id is None` has two causes and the settings tab owes two sentences.

    "Download a model" and "WinZapp could not measure this computer" are
    different instructions, and a tab that could only see None would have to
    call `device.available_memory_mb()` for itself to tell them apart — a
    second copy of this decision, free to disagree with the one it is showing.
    """

    def test_a_machine_nothing_could_be_measured_on(self):
        resolved = preferences.resolve({}, _UNKNOWN, installed_ids=())
        assert resolved.model_id is None
        assert resolved.model_none_reason == preferences.MODEL_NONE_UNMEASURED
        # Still not a substitution: nothing of the user's was replaced.
        assert resolved.substitutions == ()

    def test_a_machine_measured_and_too_small_for_every_model(self):
        tiny = device.HardwareProbe(total_ram_mb=256, available_ram_mb=128)
        resolved = preferences.resolve({}, tiny, installed_ids=())
        assert resolved.model_id is None
        assert resolved.model_none_reason == preferences.MODEL_NONE_NOTHING_FITS

    def test_an_unmeasured_machine_with_a_model_on_disk_needs_no_reason(self):
        """Rule 1 of auto_select_model(): what is already there wins."""
        resolved = preferences.resolve({}, _UNKNOWN, installed_ids=("tiny",))
        assert resolved.model_id == "tiny"
        assert resolved.model_none_reason is None

    def test_a_model_that_was_chosen_needs_no_reason_either(self):
        resolved = preferences.resolve({}, _CPU_ONLY)
        assert resolved.model_id is not None
        assert resolved.model_none_reason is None


class TestSanitizingWhatIsOnDisk:
    """`resolve()` reports a substitution and never records it.

    Which means a model retired two versions ago earns the same warning every
    time the tab is opened, and the combobox has no entry to select for the
    value on disk. This is the write half, in the shape
    `backfill_missing_defaults()` already established: edit in place, answer
    whether a save is warranted, let the caller decide when to write.
    """

    def test_a_value_that_can_never_be_valid_again_is_rewritten(self):
        settings = _settings(model="whisper-from-a-later-version", device="npu",
                             language="zz")
        assert preferences.sanitize_section(settings) is True
        section = settings[preferences.SECTION]
        assert section[preferences.SETTING_MODEL] == preferences.AUTO
        assert section[preferences.SETTING_DEVICE] == preferences.AUTO
        assert section[preferences.SETTING_LANGUAGE] == preferences.LANGUAGE_INTERFACE

    def test_a_value_that_is_not_even_a_string_is_rewritten_too(self):
        settings = _settings(model=["tiny"], device={"a": 1}, language=3,
                             backend=None, auto_detect_language="yes please",
                             compute_type=["int8"], keep_model_loaded="yes",
                             auto_transcribe_received="yes",
                             cache_enabled="no", cache_by_model="junk",
                             auto_transcribe_statuses="yes",
                             status_cache_enabled="no")
        assert preferences.sanitize_section(settings) is True
        assert settings[preferences.SECTION] == preferences.DEFAULTS

    def test_a_backend_this_machine_merely_lacks_today_is_left_alone(self):
        """The one thing sanitize must *not* do, and the reason it is narrower
        than resolve().

        Resolving around an unavailable backend costs one run; writing over it
        costs the user their choice for good — and the component they have not
        installed yet is the one they are about to.
        """
        settings = _settings(backend=backend_module.BACKEND_FASTER_WHISPER)
        assert preferences.sanitize_section(settings) is False
        assert (settings[preferences.SECTION][preferences.SETTING_BACKEND]
                == backend_module.BACKEND_FASTER_WHISPER)

    def test_a_model_that_is_only_not_downloaded_yet_is_left_alone(self):
        """Not installed is not the same as not existing — the same rule
        `_resolve_model()` keeps, and here it decides what reaches the file."""
        known = model_catalog.MODELS[-1].id
        settings = _settings(model=known)
        assert preferences.sanitize_section(settings) is False
        assert settings[preferences.SECTION][preferences.SETTING_MODEL] == known

    def test_a_healthy_section_is_not_reported_as_changed(self):
        """A save on every open is a rewritten settings.json for nothing, and
        `backfill_missing_defaults()`'s contract is the same one."""
        settings = {preferences.SECTION: dict(preferences.DEFAULTS)}
        assert preferences.sanitize_section(settings) is False
        assert settings[preferences.SECTION] == preferences.DEFAULTS

    def test_sanitizing_twice_changes_nothing_the_second_time(self):
        settings = _settings(model="gone", language="zz")
        assert preferences.sanitize_section(settings) is True
        assert preferences.sanitize_section(settings) is False

    def test_it_is_exactly_what_stops_the_warning_repeating(self):
        """The whole point, measured end to end rather than asserted."""
        settings = _settings(model="gone")
        assert preferences.resolve(settings, _CPU_ONLY).substituted is True
        preferences.sanitize_section(settings)
        assert preferences.resolve(settings, _CPU_ONLY).substitutions == ()

    def test_a_settings_file_from_before_the_feature_gets_the_section(self):
        settings = {}
        assert preferences.sanitize_section(settings) is True
        assert settings[preferences.SECTION] == preferences.DEFAULTS

    def test_something_that_is_not_a_dict_where_the_section_belongs(self):
        settings = {preferences.SECTION: "corrupted"}
        assert preferences.sanitize_section(settings) is True
        assert settings[preferences.SECTION] == preferences.DEFAULTS

    def test_a_settings_object_that_is_not_a_dict_at_all_is_not_a_crash(self):
        assert preferences.sanitize_section("not a dict") is False

    def test_an_absent_key_is_left_absent(self):
        """`read_section()` defaults it and load already backfills it, so
        writing here would report a change the user did not make."""
        settings = _settings(model="tiny")
        assert preferences.sanitize_section(settings) is False
        assert list(settings[preferences.SECTION]) == [preferences.SETTING_MODEL]


class TestACustomModelIsAChoiceUntilItsReferenceIsForgotten:
    """`external:<id>` names a model the user pointed WinZapp at in a folder of
    their own. It is a retired model the moment its reference is forgotten —
    replaced by the automatic choice, reported, and rewritten once the report
    has been delivered — and never a candidate of the automatic choice."""

    CHOICE = "external:abc123"

    def test_a_choice_whose_reference_exists_is_kept(self):
        resolved = preferences.resolve(
            _settings(model=self.CHOICE), _CPU_ONLY, ("small",),
            custom_model_ids={"abc123"})
        assert resolved.model_id == self.CHOICE
        assert resolved.substitutions == ()

    def test_a_forgotten_reference_is_replaced_and_reported(self):
        resolved = preferences.resolve(
            _settings(model=self.CHOICE), _CPU_ONLY, ("small",),
            custom_model_ids={"other"})
        assert resolved.model_id != self.CHOICE
        assert [s.setting for s in resolved.substitutions] == [preferences.SETTING_MODEL]
        assert resolved.substitutions[0].stored == self.CHOICE
        assert resolved.substitutions[0].i18n_key == "transcription_substituted_model"

    def test_nobody_measured_keeps_the_choice_like_an_unmeasured_backend(self):
        resolved = preferences.resolve(
            _settings(model=self.CHOICE), device.HardwareProbe(), ())
        assert resolved.model_id == self.CHOICE
        assert resolved.substitutions == ()

    def test_an_empty_id_names_nothing(self):
        resolved = preferences.resolve(
            _settings(model="external:"), _CPU_ONLY, ("small",), custom_model_ids={""})
        assert resolved.substituted

    def test_the_automatic_choice_is_made_from_the_catalogue_ids_alone(self):
        resolved = preferences.resolve(
            _settings(model=self.CHOICE), _CPU_ONLY, ("small",),
            custom_model_ids={"other"})
        assert resolved.model_id in (None, "small")

    def test_a_forgotten_choice_is_rewritten_so_the_report_is_not_repeated(self):
        settings = _settings(model=self.CHOICE)
        assert preferences.sanitize_section(settings, custom_model_ids={"other"}) is True
        assert settings[preferences.SECTION][preferences.SETTING_MODEL] == preferences.AUTO

    def test_a_choice_whose_reference_exists_is_not_rewritten(self):
        settings = _settings(model=self.CHOICE)
        assert preferences.sanitize_section(settings, custom_model_ids={"abc123"}) is False
        assert settings[preferences.SECTION][preferences.SETTING_MODEL] == self.CHOICE

    def test_without_the_references_it_is_left_alone(self):
        """A caller that could not read them must not cost the user a choice."""
        settings = _settings(model=self.CHOICE)
        assert preferences.sanitize_section(settings) is False
        assert settings[preferences.SECTION][preferences.SETTING_MODEL] == self.CHOICE

    def test_the_spelling_is_one_thing_for_the_picker_and_the_resolver(self):
        from core.transcription import external_models

        assert external_models.CUSTOM_CHOICE_PREFIX == preferences.CUSTOM_MODEL_PREFIX
        assert preferences.custom_model_reference_id("external:xyz") == "xyz"
        assert preferences.custom_model_reference_id("large-v3") is None
        assert preferences.custom_model_reference_id(["external:x"]) is None


_FASTER = backend_module.BACKEND_FASTER_WHISPER
_CPP = backend_module.BACKEND_WHISPER_CPP


class TestTheModelBelongsToTheBackend:
    """A GGML file is not a model faster-whisper can load, nor the other way
    round: a model of the other backend is as unloadable as a retired one, and
    is treated the same — replaced and reported."""

    def test_a_ggml_model_under_faster_whisper_is_replaced_and_reported(self):
        resolved = preferences.resolve(
            _settings(backend=_FASTER, model="ggml-small"), _CPU_ONLY
        )
        assert resolved.model_id in {model.id for model in model_catalog.list_models()}
        assert _substituted(resolved) == [preferences.SETTING_MODEL]

    def test_a_faster_whisper_model_under_whisper_cpp_is_replaced_and_reported(self):
        resolved = preferences.resolve(
            _settings(backend=_CPP, model="small"), _CPU_ONLY
        )
        assert resolved.model_id in {model.id for model in whisper_cpp_catalog.MODELS}
        assert _substituted(resolved) == [preferences.SETTING_MODEL]

    def test_a_ggml_model_under_whisper_cpp_is_kept(self):
        resolved = preferences.resolve(
            _settings(backend=_CPP, model="ggml-small-q5_1"), _CPU_ONLY
        )
        assert resolved.model_id == "ggml-small-q5_1"
        assert resolved.substitutions == ()

    def test_the_automatic_choice_comes_from_the_backends_own_catalogue(self):
        resolved = preferences.resolve(
            _settings(backend=_CPP), _CPU_ONLY, installed_ids=("small", "ggml-base")
        )
        assert resolved.model_id == "ggml-base"

    def test_the_memory_budget_is_read_off_whisper_cpps_device(self):
        # A Blackwell card whisper.cpp's CUDA build cannot run on: the choice
        # is made against the RAM the run will really use, not the 24 GB card.
        probe = device.HardwareProbe(
            cuda_available=True, cuda_device_count=1, compute_capability=(12, 0),
            total_vram_mb=24_000, free_vram_mb=24_000, cuda_libraries_ok=True,
            total_ram_mb=4_096, available_ram_mb=2_048,
        )
        resolved = preferences.resolve(
            _settings(backend=_CPP), probe, whisper_cpp_cuda_installed=True
        )
        assert resolved.model_id == device.auto_select_model(
            probe, device.DEVICE_CPU, (), catalog=whisper_cpp_catalog.list_models()
        )

    def test_a_custom_model_of_the_other_backend_is_replaced(self):
        choice = "external:abc123"
        kept = preferences.resolve(
            _settings(backend=_CPP, model=choice), _CPU_ONLY,
            custom_model_ids={"abc123": _CPP},
        )
        assert kept.model_id == choice
        replaced = preferences.resolve(
            _settings(backend=_FASTER, model=choice), _CPU_ONLY,
            custom_model_ids={"abc123": _CPP},
        )
        assert replaced.model_id != choice
        assert _substituted(replaced) == [preferences.SETTING_MODEL]

    def test_a_ggml_model_is_not_rewritten_on_disk(self):
        # Valid for one backend is valid: switching back must find it there.
        settings = _settings(model="ggml-small")
        assert preferences.sanitize_section(settings) is False
        assert settings[preferences.SECTION][preferences.SETTING_MODEL] == "ggml-small"

    def test_each_backend_lists_its_own_catalogue_and_never_the_filter(self):
        assert preferences.catalogue_models(_FASTER) == model_catalog.list_models()
        assert preferences.catalogue_models(None) == model_catalog.list_models()
        assert preferences.catalogue_models(_CPP) == whisper_cpp_catalog.list_models()
        vad = whisper_cpp_catalog.VAD_MODEL
        assert vad not in preferences.catalogue_models(_CPP)
        assert preferences.catalogue_entry(vad.id) is None


class TestASingleLanguageModelRunsInItsOwnLanguage:
    """An English-only model, or a fine-tune for one language, can only write
    that language: it is run in it whatever the setting says, and the
    replacement is reported for the flow and the tab to say — never silently."""

    @pytest.mark.parametrize("model_id, language", [
        ("small.en", "en"), ("distil-large-v3.5", "en"), ("kb-whisper-small", "sv"),
        ("ivrit-large-v3", "he"), ("ivrit-yi-large-v3", "yi"),
        ("kotoba-whisper-v2.0", "ja"),
    ])
    def test_detection_on_runs_in_the_models_language(self, model_id, language):
        resolved = preferences.resolve(_settings(model=model_id), _CPU_ONLY)
        assert resolved.model_language == language
        assert resolved.language == language
        assert resolved.language_forced is True
        # Nothing the user chose was replaced: detection was on.
        assert resolved.language_overridden is None

    def test_another_language_chosen_falls_back_and_says_which(self):
        resolved = preferences.resolve(
            _settings(backend=_CPP, model="ggml-ivrit-large-v3",
                      auto_detect_language=False, language="pt"),
            _CPU_ONLY,
        )
        assert resolved.language == "he"
        assert resolved.language_forced is True
        assert resolved.language_overridden == "pt"
        # Not a substitution: the stored value stays the user's choice and is
        # used again the moment a multilingual model is picked.
        assert resolved.substitutions == ()

    def test_the_models_own_language_chosen_is_not_forced(self):
        resolved = preferences.resolve(
            _settings(model="small.en", auto_detect_language=False, language="en"),
            _CPU_ONLY,
        )
        assert resolved.language == "en"
        assert resolved.language_forced is False
        assert resolved.language_overridden is None

    @pytest.mark.parametrize("model_id", ["small", "ggml-small-q5_1", "external:abc"])
    def test_a_multilingual_or_custom_model_leaves_the_language_alone(self, model_id):
        resolved = preferences.resolve(_settings(model=model_id), _CPU_ONLY)
        assert resolved.model_language is None
        assert resolved.language is None
        assert resolved.language_forced is False

    def test_the_automatic_choice_skips_them_while_detection_is_on(self):
        # Installed is normally the winning argument; a model that would turn
        # every Portuguese voice note into English is not one to win with.
        resolved = preferences.resolve(
            {}, _CPU_ONLY, installed_ids=("small.en", "kb-whisper-small")
        )
        assert resolved.model_id not in ("small.en", "kb-whisper-small")
        assert resolved.language_forced is False

    def test_the_automatic_choice_takes_one_for_its_own_language(self):
        resolved = preferences.resolve(
            _settings(auto_detect_language=False, language="sv"), _CPU_ONLY,
            installed_ids=("kb-whisper-small",),
        )
        assert resolved.model_id == "kb-whisper-small"
        assert resolved.language_forced is False

    def test_the_automatic_choice_never_takes_one_for_another_language(self):
        resolved = preferences.resolve(
            _settings(auto_detect_language=False, language="pt"), _CPU_ONLY,
            installed_ids=("small.en",),
        )
        assert resolved.model_id != "small.en"
        assert resolved.language == "pt"


class TestA32BitFileIsNeverChosenAutomatically:
    """A 32-bit GGML file plans more memory than its f16 one for the same
    model, so "the largest that fits" would land on it over a better model.
    Installed or not, only the user's own choice runs one."""

    def test_an_installed_32_bit_file_loses_to_a_smaller_better_model(self):
        resolved = preferences.resolve(
            _settings(backend=_CPP, auto_detect_language=False, language="en"),
            _CPU_ONLY, installed_ids=("ggml-distil-small.en-f32", "ggml-small.en"),
        )
        assert resolved.model_id == "ggml-small.en"

    def test_the_users_own_choice_of_one_is_kept(self):
        resolved = preferences.resolve(
            _settings(backend=_CPP, model="ggml-distil-small.en-f32",
                      auto_detect_language=False, language="en"),
            _CPU_ONLY, installed_ids=("ggml-distil-small.en-f32",),
        )
        assert resolved.model_id == "ggml-distil-small.en-f32"


class TestAThirdPartyModelIsNeverDownloadedByChance:
    """Fine-tunes weigh what official models weigh — ivrit's large-v3 is
    large-v3's size to the byte — so "the largest that fits" must not land on
    one by a tie. The automatic choice never downloads one; one on disk is the
    user's own and may still be picked; and of two equals the official wins."""

    @pytest.mark.parametrize("backend", [_FASTER, _CPP])
    @pytest.mark.parametrize("language", ["he", "sv", "yi"])
    def test_nothing_installed_downloads_an_official_model(self, backend, language):
        resolved = preferences.resolve(
            _settings(backend=backend, auto_detect_language=False, language=language),
            _CPU_ONLY,
        )
        entry = preferences.catalogue_entry(resolved.model_id)
        assert entry is not None and not entry.third_party
        assert entry.language is None

    @pytest.mark.parametrize("backend, model_id, language", [
        (_FASTER, "ivrit-large-v3", "he"), (_FASTER, "kb-whisper-small", "sv"),
        (_FASTER, "ivrit-yi-large-v3-turbo", "yi"),
        (_CPP, "ggml-ivrit-large-v3", "he"), (_CPP, "ggml-kb-whisper-small", "sv"),
        (_CPP, "ggml-ivrit-yi-large-v3", "yi"),
    ])
    def test_one_already_installed_for_that_language_is_still_used(
        self, backend, model_id, language
    ):
        resolved = preferences.resolve(
            _settings(backend=backend, auto_detect_language=False, language=language),
            _CPU_ONLY, installed_ids=(model_id,),
        )
        assert resolved.model_id == model_id

    @pytest.mark.parametrize("third_party, language", [
        ("ggml-ivrit-large-v3", "he"), ("ggml-kb-whisper-large", "sv"),
        ("ggml-ivrit-yi-large-v3", "yi"),
    ])
    def test_of_two_equals_installed_the_official_one_wins(self, third_party, language):
        official = whisper_cpp_catalog.get_model("ggml-large-v3")
        twin = whisper_cpp_catalog.get_model(third_party)
        # The tie this guards against is real, not hypothetical.
        assert (official.size_bytes, official.min_ram_mb) == (twin.size_bytes, twin.min_ram_mb)
        for installed in ((official.id, twin.id), (twin.id, official.id)):
            resolved = preferences.resolve(
                _settings(backend=_CPP, auto_detect_language=False, language=language),
                _CPU_ONLY, installed_ids=installed,
            )
            assert resolved.model_id == official.id

    def test_an_unmeasured_machine_prefers_the_official_one_too(self):
        installed = ("ggml-ivrit-large-v3", "ggml-large-v3")
        assert device.auto_select_model(
            _UNKNOWN, device.DEVICE_CPU, installed,
            catalog=whisper_cpp_catalog.list_models(), language="he",
        ) == "ggml-large-v3"


class TestTheLanguageList:
    """Data, not translation: 100 endonyms rather than 500 translated names."""

    def test_the_five_app_locales_are_all_supported(self):
        for locale in LOCALES:
            assert preferences.interface_language_code(locale) is not None, locale

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_app_locale_opens_the_list_and_is_the_preference(self, locale):
        """The test above only asks that some code comes back; this is what the
        user gets from it — their own language first in the picker, and the
        same code as the preference on untouched settings."""
        own = preferences.interface_language_code(locale)
        assert preferences.language_choices(locale)[0] == (own, preferences.LANGUAGE_NAMES[own])
        assert preferences.preferred_language({}, locale) == own

    def test_the_two_locales_after_the_first_five_map_to_their_own_languages(self):
        """ro and tr-TR arrived after the feature: one untagged, one tagged,
        and the tagged one's region must be dropped rather than make the code
        unknown to Whisper."""
        assert preferences.interface_language_code("ro") == "ro"
        assert preferences.interface_language_code("tr-TR") == "tr"
        assert preferences.language_name("ro") == "română"
        assert preferences.language_name("tr") == "Türkçe"

    def test_the_languages_the_issue_names_are_right(self):
        assert preferences.language_name("pl") == "polski"
        assert preferences.language_name("pt") == "português"
        assert preferences.language_name("en") == "English"
        assert preferences.language_name("es") == "español"

    def test_no_name_is_blank_and_no_code_is_region_tagged(self):
        for code, name in preferences.LANGUAGE_NAMES.items():
            assert name.strip(), code
            assert code == code.lower() and "-" not in code and "_" not in code

    def test_an_unknown_code_has_no_name(self):
        assert preferences.language_name("zz") is None

    def test_the_choices_put_the_interface_language_first(self):
        """The presentation half of "prefer the configured language".

        A screen-reader user arrowing through a hundred entries to reach their
        own is the difference between a usable list and an unusable one — and
        it costs nothing when the guess is wrong, because the entry is still in
        the list.
        """
        choices = preferences.language_choices("pl")
        assert choices[0] == ("pl", "polski")
        assert len(choices) == len(preferences.LANGUAGE_NAMES)
        assert len({code for code, _name in choices}) == len(choices)

    def test_an_unsupported_interface_language_reorders_nothing(self):
        plain = preferences.language_choices("")
        assert preferences.language_choices("zz-ZZ") == plain
        assert len(plain) == len(preferences.LANGUAGE_NAMES)

    def test_the_choices_are_ordered_by_the_name_the_user_reads(self):
        names = [name for _code, name in preferences.language_choices("")]
        assert names == sorted(names, key=str.casefold)


class TestTheModelsFolderIsInstallWide:
    def test_it_is_a_global_setting(self, tmp_path):
        settings = app_settings.AppSettings(str(tmp_path / "global"))
        assert settings.get(preferences.MODELS_DIR_SETTING) == ""

        settings.set(preferences.MODELS_DIR_SETTING, str(tmp_path / "models"))
        reopened = app_settings.AppSettings(str(tmp_path / "global"))
        assert reopened.get(preferences.MODELS_DIR_SETTING) == str(tmp_path / "models")

    def test_the_per_account_section_does_not_carry_it(self):
        """Two accounts pointing at different folders is a state with no
        meaning: the model files themselves are shared."""
        assert preferences.MODELS_DIR_SETTING not in DEFAULT_SETTINGS[preferences.SECTION]
        assert "models_dir" not in DEFAULT_SETTINGS[preferences.SECTION]

    def test_a_per_account_transcription_key_is_refused_by_app_settings(self, tmp_path):
        """The guard that keeps the two files from being confused, in the
        direction that matters: routing an account's own setting through the
        shared file would apply it to every account.

        `language` is left out of the loop deliberately — app_settings has a
        global key by that name and it is the *interface* language, an entirely
        different setting that merely shares a word. The next test is what
        pins them apart.
        """
        settings = app_settings.AppSettings(str(tmp_path / "global"))
        keys = [key for key in DEFAULT_SETTINGS[preferences.SECTION]
                if key != preferences.SETTING_LANGUAGE]
        assert keys, "nothing left to check — the exemption swallowed the section"
        for key in keys + [preferences.SECTION]:
            with pytest.raises(KeyError):
                settings.get(key)
            with pytest.raises(KeyError):
                settings.set(key, "whatever")

    def test_the_transcription_language_is_not_the_interface_language(self, tmp_path):
        """Two settings, one word, and only one of them is install-wide.

        `general.language` is which language WinZapp is *in* and is shared by
        every account; `transcription.language` is which language a voice note
        is transcribed in and is that account's own. The legacy split reaches
        only into the general and connection blocks, so a transcription section
        survives it untouched — worth pinning, because the two are one careless
        flattening away from being the same key.
        """
        settings = app_settings.AppSettings(str(tmp_path / "global"))
        settings.set("language", "pl")

        legacy = {
            "general": {"language": "pl", "notifications_enabled": True},
            preferences.SECTION: dict(preferences.DEFAULTS),
        }
        globals_, per_account = app_settings.split_legacy_settings(legacy)

        assert globals_["language"] == "pl"
        assert preferences.SECTION not in globals_
        assert per_account[preferences.SECTION] == preferences.DEFAULTS

    def test_reading_it_needs_nothing_but_the_app_settings_object(self, tmp_path):
        """A module-level function, callable without a wx.Dialog around it.

        The version of this that lived on the settings dialog was reachable
        only through a stub — and the stub is what let it read a `main_window`
        attribute production does not have, under six passing tests. Part 5c's
        download and part 6's run need the same answer; a copy on each of them
        is how they start disagreeing about which folder is in use.
        """
        settings = app_settings.AppSettings(str(tmp_path / "global"))
        assert preferences.stored_models_dir(settings) == ""

        settings.set(preferences.MODELS_DIR_SETTING, str(tmp_path / "models"))
        assert preferences.stored_models_dir(settings) == str(tmp_path / "models")

    def test_no_app_settings_at_all_reads_as_the_default_folder(self):
        """A legacy, account-less install has no global dir and therefore no
        AppSettings — which must read as "nobody chose one", never as a
        failure: the settings tab still has to open."""
        assert preferences.stored_models_dir(None) == ""

    def test_an_empty_value_means_the_shared_default_folder(self, tmp_path,
                                                            monkeypatch):
        """Not the resolved path, which would freeze a data folder meant to be
        copyable to another machine."""
        monkeypatch.setattr(
            preferences.model_store, "default_models_dir",
            lambda: str(tmp_path / "shared"),
        )
        assert preferences.resolve_models_dir("") == str(tmp_path / "shared")
        assert preferences.resolve_models_dir(None) == str(tmp_path / "shared")
        assert preferences.resolve_models_dir(str(tmp_path / "elsewhere")) == str(
            tmp_path / "elsewhere"
        )

    def test_where_it_lists_from_is_where_it_would_download_to(self, tmp_path,
                                                               monkeypatch):
        """One call, because the risk is the two halves disagreeing.

        The settings tab has about five actions that each need the folder and
        what is inside it, and writing the glue — read the install-wide value,
        resolve it, list the folder — five times is five chances for "where I
        list from" to drift from "where I download to". The user would then be
        shown a model that is not the one a run uses.
        """
        monkeypatch.setattr(
            preferences.model_store, "default_models_dir",
            lambda: str(tmp_path / "shared"),
        )
        listed = []
        monkeypatch.setattr(
            preferences.model_store, "list_installed",
            lambda root: listed.append(root) or ("tiny",),
        )

        chosen = str(tmp_path / "elsewhere")
        assert preferences.models_folder(chosen) == (chosen, ("tiny",))
        assert preferences.models_folder("") == (str(tmp_path / "shared"), ("tiny",))
        assert listed == [chosen, str(tmp_path / "shared")]


class TestNothingHereMeasuresAnything:
    def test_the_probe_is_an_argument(self, monkeypatch):
        """Same rule as device.py's: a decision that measures its own inputs
        cannot be reproduced anywhere but on the hardware that made it."""
        def _fail():  # pragma: no cover - the assertion is that it is not called
            raise AssertionError("resolve() probed the hardware on its own")

        monkeypatch.setattr(device, "probe_hardware", _fail)
        monkeypatch.setattr(backend_module, "available_backend_ids", _fail)
        monkeypatch.setattr(backend_module, "get_backend", _fail)

        preferences.resolve({}, _CPU_ONLY, ui_language="pl")

    def test_resolution_does_not_restate_the_device_the_run_lands_on(self):
        """The job re-probes immediately before it decides — that is the whole
        point of a probe never being reused — so a copy kept here could only
        ever be the older answer."""
        resolved = preferences.resolve({}, _CPU_ONLY)
        assert not hasattr(resolved, "device")
        assert not hasattr(resolved, "compute_type")


class TestEveryStringThisModuleNamesIsTranslated:
    """I18n.t() renders a missing key as its own name, out loud."""

    @pytest.mark.parametrize("locale", LOCALES)
    def test_the_option_labels(self, locale):
        table = load_strings(locale)
        keys = (
            [preferences.OPTION_AUTO_I18N_KEY,
             preferences.LANGUAGE_DETECT_I18N_KEY,
             preferences.LANGUAGE_INTERFACE_I18N_KEY]
            + list(preferences.DEVICE_PREFERENCE_I18N_KEYS.values())
            + list(preferences.BACKEND_I18N_KEYS.values())
        )
        missing = sorted(key for key in keys if key not in table)
        assert missing == [], f"{locale} catalog would speak these key names: {missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_the_substitution_warnings(self, locale):
        table = load_strings(locale)
        missing = sorted(
            key for key in preferences.SUBSTITUTION_I18N_KEYS.values()
            if key not in table
        )
        assert missing == [], f"{locale} catalog is missing substitutions: {missing}"

    def test_every_device_preference_has_a_label(self):
        for preference in (device.PREFERENCE_AUTO, device.PREFERENCE_CUDA,
                           device.PREFERENCE_CPU):
            assert preferences.DEVICE_PREFERENCE_I18N_KEYS[preference]

    def test_every_backend_id_has_a_label(self):
        for backend_id in backend_module.BACKEND_IDS:
            assert preferences.BACKEND_I18N_KEYS[backend_id]

    def test_every_setting_that_can_be_substituted_has_a_warning(self):
        for setting in (preferences.SETTING_BACKEND, preferences.SETTING_MODEL,
                        preferences.SETTING_DEVICE, preferences.SETTING_LANGUAGE):
            assert preferences.SUBSTITUTION_I18N_KEYS[setting]

    @pytest.mark.parametrize("locale", LOCALES)
    def test_both_reasons_for_having_no_model(self, locale):
        """Without a sentence for each, a machine where nothing fits shows
        "Automático" selected and says nothing — which reads as "WinZapp chose
        something" to the one user who cannot see that it did not."""
        table = load_strings(locale)
        missing = sorted(
            key for key in preferences.MODEL_NONE_I18N_KEYS.values()
            if key not in table
        )
        assert missing == [], f"{locale} catalog is missing: {missing}"

    def test_every_reason_resolve_can_report_has_one(self):
        for reason in (preferences.MODEL_NONE_NOTHING_FITS,
                       preferences.MODEL_NONE_UNMEASURED):
            assert preferences.MODEL_NONE_I18N_KEYS[reason]
