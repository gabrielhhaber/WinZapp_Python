"""What the user chose for transcription, and what that means for one run.

Five settings live in each account's own settings.json — backend, model,
device, precision (faster-whisper's compute type, part 11) and language (plus
the checkbox that says whether the language is detected at all) — and one, the folder the models are downloaded into, deliberately does
not: it is install-wide, in app.json, because the model files themselves are
shared by every account (that is why `model_store.default_models_dir()` sits
under `global_dir()`) and one account pointing somewhere else while its
siblings keep reading the old folder is not a state that means anything.

Three rules shape everything below.

* **"Automatic" is a stored value, not an absent one.** Every setting here has
  a "let WinZapp decide" position and it reaches disk as a literal sentinel —
  `"auto"` for the backend, the model, the device and the precision,
  `"interface"` for the
  language. A blank would be ambiguous the day one of these defaults changes:
  nothing on disk would separate "the user picked what happened to be the
  default" from "the user never opened the dialog", which is precisely the
  state the one-shot migrations in `core/utils.py` exist to repair after the
  fact. Storing the sentinel is how this section avoids ever needing one.

  **`auto_detect_language` is the exception, and it is stored as a plain
  bool.** It is a checkbox: it has two positions and no third behaviour, so
  there is nothing for a sentinel to *mean* — a stored `"auto"` would have to
  be mapped back onto one of the two the first time the dialog was opened,
  which reintroduces the same ambiguity one OK later. What that costs is real
  and is accepted with open eyes: this is the default most likely to be
  reconsidered, and the day it flips, an install carrying True cannot be told
  from one that was never touched. The answer then is a one-shot migration
  with its own flag, exactly like `migrate_voice_message_mode_default()` —
  a known, bounded price rather than a novel one.

* **A stored value that no longer exists resolves to automatic, and says so.**
  A model id a later catalogue drops, a language code that was never valid, a
  backend this install does not have — each is the normal state of a
  settings.json that outlived the version which wrote it, not a fault, so none
  of them raises. But the substitution is *reported* through
  `Resolution.substitutions`: transcribing with a quietly different model than
  the settings dialog is showing is how a user spends an afternoon wondering
  why "large-v3" sounds like "tiny", and a blind user has no visual cue at all
  that anything was swapped.

* **Nothing here touches the machine.** The hardware probe, the installed model
  ids and the usable backend ids all arrive as arguments, for the reason
  `device.py`'s docstring gives at length: a decision that measures its own
  inputs cannot be reproduced anywhere but on the hardware that made it, and
  "which model does a 4 GB card get" is exactly the question no test runner can
  answer for itself.

The resolved fields are the ones `TranscriptionJob.__init__` takes, and no
more. The device the run ends up on and the compute type are deliberately *not*
here: the job re-probes immediately before it decides, which is the whole point
of `device.probe_hardware()` never being reused, and a second answer kept here
would only be the older one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from core.transcription import backend as backend_module
from core.transcription import (
    device,
    model_catalog,
    model_store,
    precision,
    whisper_cpp_catalog,
    whisper_cpp_store,
)

#: The section of settings.json this module owns.
SECTION = "transcription"

#: The stored spelling of "let WinZapp decide", for backend, model and device.
#: Shares device.PREFERENCE_AUTO's value on purpose — the device setting is
#: written straight through to `device.resolve_device()`.
AUTO = "auto"

#: The stored spelling of "the language WinZapp itself is in". A sentinel
#: rather than the resolved code, so that changing WinZapp's language moves the
#: transcription language with it; a "pl" written into settings.json the day the
#: user was on Polish would silently stay Polish forever after.
LANGUAGE_INTERFACE = "interface"

# The settings themselves, named once. These strings are the keys inside the
# section *and* the identifiers a substitution reports, so the dialog that
# writes a value and the warning that says it was replaced cannot drift apart.
SETTING_BACKEND = "backend"
SETTING_MODEL = "model"
SETTING_DEVICE = "device"
#: faster-whisper's compute type: AUTO or one of precision.COMPUTE_TYPES.
SETTING_COMPUTE_TYPE = "compute_type"
SETTING_LANGUAGE = "language"
SETTING_AUTO_DETECT_LANGUAGE = "auto_detect_language"
#: Whether the loaded model stays in memory between transcriptions, however
#: long the pause (core/transcription/residency.py). A plain bool, off by default.
SETTING_KEEP_MODEL_LOADED = "keep_model_loaded"
#: Whether received voice messages are transcribed on arrival and the text put
#: in the message's line (core/transcription/auto_queue.py). Off by default.
SETTING_AUTO_TRANSCRIBE_RECEIVED = "auto_transcribe_received"
#: Whether results are kept with the message so they are not redone (the
#: "cache"), on by default; and the per-model overrides of it, {model id: bool}.
#: See stored.cache_enabled_for().
SETTING_CACHE_ENABLED = "cache_enabled"
SETTING_CACHE_BY_MODEL = "cache_by_model"
#: The same two switches for status updates (core/transcription/statuses.py):
#: transcribe audio and video statuses as they arrive, off by default, and
#: keep a status's result with it, on by default.
SETTING_AUTO_TRANSCRIBE_STATUSES = "auto_transcribe_statuses"
SETTING_STATUS_CACHE_ENABLED = "status_cache_enabled"
#: Whether a transcription asked for by hand runs with no progress window: the
#: user stays on the list and the result arrives when ready. Off by default.
SETTING_RUN_WITHOUT_DIALOG = "run_without_dialog"

#: The section as a fresh install gets it. `core/utils.py`'s DEFAULT_SETTINGS
#: and `client/data/settings_default.json` both carry a copy — see the module
#: docstring of core/utils.py for why there are two — and a test pins all three
#: together.
DEFAULTS = {
    SETTING_BACKEND: AUTO,
    SETTING_MODEL: AUTO,
    SETTING_DEVICE: AUTO,
    SETTING_COMPUTE_TYPE: AUTO,
    # Meaningful only while auto-detection is off; see resolve() for why the
    # two are separate settings rather than one list with a "detect" entry.
    SETTING_LANGUAGE: LANGUAGE_INTERFACE,
    SETTING_AUTO_DETECT_LANGUAGE: True,
    SETTING_KEEP_MODEL_LOADED: False,
    SETTING_RUN_WITHOUT_DIALOG: False,
    SETTING_AUTO_TRANSCRIBE_RECEIVED: False,
    SETTING_CACHE_ENABLED: True,
    SETTING_CACHE_BY_MODEL: {},
    SETTING_AUTO_TRANSCRIBE_STATUSES: False,
    SETTING_STATUS_CACHE_ENABLED: True,
}

#: The install-wide key holding the models folder, in `app_settings.py`'s own
#: `_DEFAULTS`. Spelled out here rather than imported: app_settings is loaded
#: before almost anything else and importing this package (and, through it,
#: requests) from there would be a layering inversion. `tests/
#: test_transcription_preferences.py` pins the two spellings to each other.
MODELS_DIR_SETTING = "transcription_models_dir"

#: What the model setting starts with when it names a model the user pointed
#: WinZapp at in a folder of their own that no catalogue entry claims
#: (`external:<reference id>`, see external_models). It lives here, below
#: external_models in the import order, because resolve() and sanitize_section()
#: have to recognise it and external_models already imports this module.
CUSTOM_MODEL_PREFIX = "external:"

# i18n keys for the option labels this module defines as data. The concrete
# model ids are labelled by model_catalog's size classes and the concrete
# languages by their own endonyms, so those are not here.
OPTION_AUTO_I18N_KEY = "transcription_option_auto"
LANGUAGE_DETECT_I18N_KEY = "transcription_language_detect"
LANGUAGE_INTERFACE_I18N_KEY = "transcription_language_interface"

DEVICE_PREFERENCE_I18N_KEYS = {
    device.PREFERENCE_AUTO: OPTION_AUTO_I18N_KEY,
    device.PREFERENCE_CUDA: "transcription_device_preference_cuda",
    device.PREFERENCE_CPU: "transcription_device_preference_cpu",
}

# A backend id is not a name a screen reader should read out of a combobox, so
# every backend gets a label. AUTO is in here as well, which is what lets the
# picker be built by mapping over its own option list.
BACKEND_I18N_KEYS = {
    AUTO: OPTION_AUTO_I18N_KEY,
    backend_module.BACKEND_FASTER_WHISPER: "transcription_backend_faster_whisper",
    backend_module.BACKEND_WHISPER_CPP: "transcription_backend_whisper_cpp",
}

# What to say when a stored value could not be honoured. One sentence per
# setting, never the value itself: "large-v3" spoken back to a blind user says
# nothing they can act on, and the technical id belongs in log.log — the same
# code/detail split errors.py makes.
SUBSTITUTION_I18N_KEYS = {
    SETTING_BACKEND: "transcription_substituted_backend",
    SETTING_MODEL: "transcription_substituted_model",
    SETTING_DEVICE: "transcription_substituted_device",
    SETTING_COMPUTE_TYPE: "transcription_substituted_compute_type",
    SETTING_LANGUAGE: "transcription_substituted_language",
}

# Why there is no model to run with, when `Resolution.model_id` is None.
# Neither is an error and neither is a substitution — nothing was replaced —
# but they are two different situations and the settings tab owes each a
# different sentence. Told apart here rather than in the tab because the only
# way to redo it there is to call `device.available_memory_mb()` a second time
# against the same probe, which is a copy of this decision that can disagree
# with it.
MODEL_NONE_NOTHING_FITS = "nothing_fits"
#: The memory could not be measured at all and nothing is installed to fall
#: back on. `auto_select_model()` refuses to guess upwards on an unknown
#: machine, so this stays None however many models the catalogue holds — and
#: "download a model" is the instruction, not "your machine is too small".
MODEL_NONE_UNMEASURED = "unmeasured"

# The sentence for each of them. Without one the settings tab shows
# "Automatic" selected and says nothing at all, which reads as "WinZapp picked
# something" to the one user who cannot see that it did not.
MODEL_NONE_I18N_KEYS = {
    MODEL_NONE_NOTHING_FITS: "transcription_model_none_nothing_fits",
    MODEL_NONE_UNMEASURED: "transcription_model_none_unmeasured",
}

# Every language Whisper was trained on, with its **endonym** — the name of the
# language in that language. Not translated into the five app locales, and that
# is a decision rather than a shortcut: translating a hundred language names
# five times is five hundred strings nobody here can review, it goes stale the
# moment a name is corrected, and a user looking for their own language
# recognises "polski" in any UI far more reliably than a Portuguese or Spanish
# rendering of it. Every language selector does this for the same reason.
#
# Whisper's own codes, verbatim, including the two that are not the current ISO
# spelling ("jw" for Javanese, "no" for Norwegian Bokmål): the code is what the
# backend is handed, so a corrected spelling here would simply not be
# recognised by the model. Declared in Whisper's own order, which is roughly by
# training data volume; language_choices() decides presentation order.
LANGUAGE_NAMES = {
    "en": "English",
    "zh": "中文",
    "de": "Deutsch",
    "es": "español",
    "ru": "русский",
    "ko": "한국어",
    "fr": "français",
    "ja": "日本語",
    "pt": "português",
    "tr": "Türkçe",
    "pl": "polski",
    "ca": "català",
    "nl": "Nederlands",
    "ar": "العربية",
    "sv": "svenska",
    "it": "italiano",
    "id": "Bahasa Indonesia",
    "hi": "हिन्दी",
    "fi": "suomi",
    "vi": "Tiếng Việt",
    "he": "עברית",
    "uk": "українська",
    "el": "Ελληνικά",
    "ms": "Bahasa Melayu",
    "cs": "čeština",
    "ro": "română",
    "da": "dansk",
    "hu": "magyar",
    "ta": "தமிழ்",
    "no": "norsk",
    "th": "ไทย",
    "ur": "اردو",
    "hr": "hrvatski",
    "bg": "български",
    "lt": "lietuvių",
    "la": "Latina",
    "mi": "te reo Māori",
    "ml": "മലയാളം",
    "cy": "Cymraeg",
    "sk": "slovenčina",
    "te": "తెలుగు",
    "fa": "فارسی",
    "lv": "latviešu",
    "bn": "বাংলা",
    "sr": "српски",
    "az": "azərbaycan dili",
    "sl": "slovenščina",
    "kn": "ಕನ್ನಡ",
    "et": "eesti",
    "mk": "македонски",
    "br": "brezhoneg",
    "eu": "euskara",
    "is": "íslenska",
    "hy": "հայերեն",
    "ne": "नेपाली",
    "mn": "монгол",
    "bs": "bosanski",
    "kk": "қазақ тілі",
    "sq": "shqip",
    "sw": "Kiswahili",
    "gl": "galego",
    "mr": "मराठी",
    "pa": "ਪੰਜਾਬੀ",
    "si": "සිංහල",
    "km": "ភាសាខ្មែរ",
    "sn": "chiShona",
    "yo": "Yorùbá",
    "so": "Soomaali",
    "af": "Afrikaans",
    "oc": "occitan",
    "ka": "ქართული",
    "be": "беларуская",
    "tg": "тоҷикӣ",
    "sd": "سنڌي",
    "gu": "ગુજરાતી",
    "am": "አማርኛ",
    "yi": "ייִדיש",
    "lo": "ລາວ",
    "uz": "o‘zbek",
    "fo": "føroyskt",
    "ht": "kreyòl ayisyen",
    "ps": "پښتو",
    "tk": "türkmen dili",
    "nn": "nynorsk",
    "mt": "Malti",
    "sa": "संस्कृतम्",
    "lb": "Lëtzebuergesch",
    "my": "မြန်မာဘာသာ",
    "bo": "བོད་སྐད་",
    "tl": "Tagalog",
    "mg": "Malagasy",
    "as": "অসমীয়া",
    "tt": "татар теле",
    "haw": "ʻŌlelo Hawaiʻi",
    "ln": "lingála",
    "ha": "Hausa",
    "ba": "башҡорт теле",
    "jw": "basa Jawa",
    "su": "basa Sunda",
    "yue": "粵語",
}


@dataclass(frozen=True)
class Substitution:
    """One stored value that could not be honoured, and what replaced it.

    `stored` is log-facing only, exactly like `TranscriptionError.detail`: it is
    an id or a code, and reading one out to a blind user in place of a sentence
    tells them nothing they can act on. The sentence is `i18n_key`.
    """

    setting: str
    stored: str

    @property
    def i18n_key(self) -> str:
        return SUBSTITUTION_I18N_KEYS[self.setting]


@dataclass(frozen=True)
class Resolution:
    """The decisions one run is made with. Fields match TranscriptionJob's.

    `model_id` and `backend_id` may both be None, and neither is an error at
    this layer: "no model is installed and none fits" is what the settings tab
    turns into an offer to download one, and "no backend can run here" is what
    it turns into the same offer for the component. Failing here instead would
    put the error in front of the user before they had asked for anything.

    `language` is None for "let the model detect it", which is the same spelling
    `backend.TranscriptionRequest` uses.
    """

    backend_id: str | None
    model_id: str | None
    device_preference: str
    language: str | None
    substitutions: tuple[Substitution, ...] = field(default_factory=tuple)
    #: MODEL_NONE_NOTHING_FITS or MODEL_NONE_UNMEASURED when `model_id` is
    #: None, and None whenever it is not. The two need different sentences —
    #: "download a model" against "WinZapp could not measure this computer" —
    #: and a caller that only saw None would have to guess which, or measure
    #: again and risk disagreeing with the answer it was handed.
    model_none_reason: str | None = None
    #: The one language `model_id` knows (an `.en` or distilled model, a
    #: third-party fine-tune), or None for a multilingual one. A custom model
    #: is not known to be single-language, and counts as multilingual.
    model_language: str | None = None
    #: `language` is `model_language` because the model cannot do anything
    #: else: detection was on, or another language was chosen — the run uses
    #: the model's language and says so (narration), never silently.
    language_forced: bool = False
    #: The language the user chose that `model_language` replaced, or None
    #: (detection was on, or nothing was replaced).
    language_overridden: str | None = None
    #: The precision the user chose (one of precision.COMPUTE_TYPES), or AUTO.
    #: The stored choice, not what a run will load with: that depends on the
    #: device the job resolves, so the job resolves it too
    #: (precision.resolve_compute_type()), like the device.
    compute_type_preference: str = AUTO

    @property
    def substituted(self) -> bool:
        return bool(self.substitutions)


def read_section(settings) -> dict:
    """This section of a settings dict, with every missing key defaulted.

    Tolerant of anything: a settings.json written before this feature existed
    has no section at all, and a corrupt one may have something that is not a
    dict where the section belongs. Neither may keep the transcription tab from
    opening, so both read as "all defaults".
    """
    section = settings.get(SECTION) if isinstance(settings, dict) else None
    # A copy of the mutable default too: a caller that edits the answer must
    # not edit the defaults of everybody after it.
    resolved = {key: (dict(value) if isinstance(value, dict) else value)
                for key, value in DEFAULTS.items()}
    if isinstance(section, dict):
        for key in DEFAULTS:
            if key in section:
                resolved[key] = section[key]
    return resolved


def resolve(settings, probe, installed_ids=(), ui_language="",
            available_backends=None, custom_model_ids=None,
            whisper_cpp_cuda_installed=False) -> Resolution:
    """Everything one transcription needs, from settings plus measurements.

    `probe` is a `device.HardwareProbe` the caller took; `installed_ids` are the
    models on disk (`model_store.list_installed()`); `available_backends` are
    the ids that can actually run here (`backend.available_backend_ids()`), or
    None when nobody has measured — in which case a stored backend is only
    checked against the ids this version knows about, since refusing one we
    never asked about would be inventing an obstacle.

    `custom_model_ids` are the reference ids of the custom models the user has
    registered (`external_models.ExternalReference.id` of the ones with no
    catalogue model). A stored `external:<id>` is kept while its id is among
    them and replaced — with the substitution warning, like a retired model —
    once its reference was forgotten. None means "nobody measured", and then
    the choice is kept as it is, for the reason `available_backends` gives.
    Passed as a mapping {id: backend id}
    (`external_models.custom_reference_backends()`), a custom model of the
    other backend is replaced too: a whisper.cpp file is not a model
    faster-whisper can load, nor the other way round. `installed_ids` stays the
    catalogue models only — of both backends, each filtered by the backend
    resolved here: a custom one is never a candidate for the automatic choice
    (external_models.usable_catalogue_ids()).

    **The model has to be one of the resolved backend's.** A faster-whisper id
    stored while whisper.cpp is the backend (or a GGML id under faster-whisper)
    is replaced by the automatic choice of that backend, with the model
    substitution warning — the same treatment as a retired id, because to the
    run it is exactly as unloadable. `whisper_cpp_cuda_installed` is whether
    whisper.cpp's graphics-card build is on this machine: with whisper.cpp,
    the device the memory budget is read off follows its own rule
    (`device.resolve_whisper_cpp_device()`).

    **A single-language model** (English-only, or a third-party fine-tune) is
    run in its own language whatever the setting says — the only language it
    can produce — and the replacement is reported (`language_forced`,
    `language_overridden`) for the flow and the tab to say. The automatic
    choice picks one only when it is the user's language.

    `ui_language` is WinZapp's *effective* interface language ("pt-BR", "pl") —
    the settings tab passes `main_window.i18n.language`. That is the value that
    is always answerable: `I18n.get_language()` reads `general.language` and
    falls back to "pt-BR", and `MainWindow._ensure_language_selected()` keeps a
    dialog up until the setting has a value, so the two normally agree. An
    empty string means the caller had nothing to pass, and is treated as
    "nobody said" rather than as a language that stopped existing.
    """
    section = read_section(settings)
    substitutions = []

    backend_id = _resolve_backend(section, available_backends, substitutions)
    preference = _resolve_device_preference(section, substitutions)
    compute_preference = _resolve_compute_type(section, substitutions)
    # The model is chosen against the device this preference resolves to right
    # now, because the memory budget is the whole question and VRAM and RAM are
    # different numbers. The job re-probes and re-resolves before it loads
    # anything, so this can in principle disagree with where the run lands — the
    # alternative is picking a model against no device at all.
    if backend_id == backend_module.BACKEND_WHISPER_CPP:
        device_id, _reason = device.resolve_whisper_cpp_device(
            preference, probe, whisper_cpp_cuda_installed
        )
    else:
        device_id, _reason = device.resolve_device(preference, probe)
    # Before the model: whether a single-language model may be chosen
    # automatically depends on it.
    language = _resolve_language(section, ui_language, substitutions)
    # The precision the model would load with moves what fits: float32 weights
    # take twice float16's, 8-bit ones half. Only faster-whisper's — a
    # whisper.cpp file's precision is the file, already in its figures.
    memory_compute = (
        None if backend_id == backend_module.BACKEND_WHISPER_CPP
        else precision.memory_compute_type(compute_preference, device_id, probe)
    )
    model_id = _resolve_model(
        section, probe, device_id, installed_ids, substitutions, custom_model_ids,
        backend_id, language, memory_compute,
    )
    only = model_language(model_id)
    forced = only is not None and language != only
    overridden = language if forced else None
    if forced:
        language = only

    return Resolution(
        backend_id=backend_id,
        model_id=model_id,
        device_preference=preference,
        language=language,
        substitutions=tuple(substitutions),
        model_none_reason=_model_none_reason(model_id, probe, device_id),
        model_language=only,
        language_forced=forced,
        language_overridden=overridden,
        compute_type_preference=compute_preference,
    )


def preferred_language(settings, ui_language=""):
    """The language the user would rather hear, resolved but never imposed.

    Always answered — the `LANGUAGE_INTERFACE` sentinel turned into a Whisper
    code, an explicit choice returned as it stands, anything unusable as None —
    and never an instruction to transcribe in it. That is the whole difference
    from `Resolution.language`, which is None whenever detection is on and
    therefore cannot carry a preference at all.

    Both halves of this are needed because **both** answers fail quietly.
    Forcing the interface language is the worse one and `_resolve_language()`
    explains it at length: Whisper handed a language the audio is not in
    invents a confident transcription in that language. But detection is not
    the safe opposite — Whisper's identifier reads only the first 30 s window
    and gets a short, noisy voice note wrong often enough that the reporter's
    own case is the common one, and nothing about a wrong answer looks wrong.

    So the preference stays available to the layers that can use it without
    overriding anything: part 6 can say "this one was transcribed as Spanish"
    when the detected language differs from this, and a later part can weigh it
    against `TranscriptionResult.language_probability` when the detection is
    not confident. Kept out of `Resolution` on purpose — a field there is a
    decision the job acts on, and this is not one.
    """
    section = read_section(settings)
    stored = section[SETTING_LANGUAGE]
    if stored == LANGUAGE_INTERFACE:
        return interface_language_code(ui_language)
    return stored if isinstance(stored, str) and stored in LANGUAGE_NAMES else None


def sanitize_section(settings, custom_model_ids=None) -> bool:
    """Replace every permanently meaningless stored value. True if anything was.

    Same shape and same contract as `core.utils.backfill_missing_defaults()`:
    edits the dict in place and answers whether a save is warranted, so the
    caller decides when to write. It exists because `resolve()` reports a
    substitution and never records it — a model retired two versions ago earns
    the same warning every single time the tab is opened, and the combobox has
    no entry to select for the value on disk either.

    **Only what can never be valid again is rewritten**, which is a narrower
    set than what `resolve()` substitutes: a model the catalogue no longer
    knows, a device string nothing recognises, a language code that is not a
    language, a backend id this version has never heard of, a detection flag
    that is not a bool, a precision this version does not know — and a
    custom model (`external:<id>`) whose reference
    the user forgot, when the caller passes the ids that still exist as
    `custom_model_ids` (None: nobody measured, and the value is left alone, as
    resolve() does). A backend that is merely *unavailable on this machine
    today* is deliberately left alone — the same reasoning as
    `_resolve_backend()`'s, one step more expensive to get wrong: resolving
    around it costs one run, writing over it costs the user their choice
    permanently, and the component they had not installed yet is the one they
    are about to.

    Call `resolve()` first if the substitutions are to be *announced*; after
    this there is nothing left to announce, which is the point. **The caller
    takes on the whole of that responsibility**: this function cannot tell
    whether the warning it is about to make unrepeatable ever reached anybody,
    so calling it before the sentence has actually been delivered spends the
    only notice the user was ever going to get.
    """
    if not isinstance(settings, dict):
        return False
    section = settings.get(SECTION)
    # The model is checked against the stored backend when one is chosen
    # explicitly: under it, the other backend's model ids (and custom models)
    # can never be loaded. Under "automatic" the backend is a decision of the
    # machine, which can change, so any backend's model is left alone and
    # resolve() substitutes per run instead.
    stored_backend = section.get(SETTING_BACKEND) if isinstance(section, dict) else None
    model_backend = (stored_backend if isinstance(stored_backend, str)
                     and stored_backend in backend_module.BACKEND_IDS else None)
    if not isinstance(section, dict):
        # A settings.json from before this feature, or one with something that
        # is not a dict where the section belongs. `read_section()` already
        # tolerates both; writing the section out makes the tab's own OK a
        # normal edit of an existing block rather than a creation.
        settings[SECTION] = dict(DEFAULTS)
        return True

    changed = False
    for key, valid in (
        (SETTING_BACKEND, lambda v: v in backend_module.BACKEND_IDS),
        (SETTING_MODEL,
         lambda v: _model_choice_exists(v, custom_model_ids, model_backend)),
        (SETTING_DEVICE, lambda v: v in DEVICE_PREFERENCE_I18N_KEYS),
        (SETTING_COMPUTE_TYPE, precision.is_choice),
        (SETTING_LANGUAGE, lambda v: v in LANGUAGE_NAMES),
    ):
        # An absent key is left absent: `read_section()` defaults it and
        # `backfill_missing_defaults()` fills it at load, so writing it here
        # would only report a change the user did not make.
        if key not in section or section[key] == DEFAULTS[key]:
            continue
        stored = section[key]
        # `isinstance` guards the membership tests for the same reason
        # `_resolve_language()` does: a list or a dict left behind by a bad
        # merge is unhashable, and `in` against a dict raises TypeError rather
        # than answering False.
        if isinstance(stored, str) and valid(stored):
            continue
        section[key] = DEFAULTS[key]
        changed = True

    for flag in (SETTING_AUTO_DETECT_LANGUAGE, SETTING_KEEP_MODEL_LOADED,
                 SETTING_AUTO_TRANSCRIBE_RECEIVED, SETTING_CACHE_ENABLED,
                 SETTING_AUTO_TRANSCRIBE_STATUSES, SETTING_STATUS_CACHE_ENABLED,
                 SETTING_RUN_WITHOUT_DIALOG):
        if flag in section and not isinstance(section[flag], bool):
            section[flag] = DEFAULTS[flag]
            changed = True
    if SETTING_CACHE_BY_MODEL in section:
        own = section[SETTING_CACHE_BY_MODEL]
        clean = ({key: value for key, value in own.items()
                  if isinstance(key, str) and key and isinstance(value, bool)}
                 if isinstance(own, dict) else {})
        if clean != own:
            section[SETTING_CACHE_BY_MODEL] = clean
            changed = True
    return changed


def models_folder(stored=None) -> tuple[str, tuple[str, ...]]:
    """(the folder the models are in, the ids that are complete inside it).

    One call rather than the three-step glue every action of the settings tab
    would otherwise repeat — read the install-wide value, resolve it, list what
    is inside — because the risk that removes is the two halves disagreeing: a
    tab that lists from the default folder while downloading into the
    configured one shows the user a model that is not the one a run would use.
    `stored` is the value already read from `AppSettings`, so this module still
    does not decide where settings come from.

    Unlike `resolve()` this does touch the disk, and it is the caller's
    gatherer rather than part of the decision: its second half is exactly what
    `resolve()` then takes as `installed_ids`.
    """
    directory = resolve_models_dir(stored)
    # Both backends' models: they share the folder (whisper_cpp_store), and
    # resolve() keeps only the resolved backend's.
    return directory, (
        model_store.list_installed(directory)
        + whisper_cpp_store.list_installed(directory)
    )


def catalogue_models(backend_id) -> tuple:
    """The catalogue entries `backend_id` can load, in the picker's order.

    faster-whisper's catalogue for anything that is not whisper.cpp — None
    (nothing resolved) included, which is what "automatic" means wherever
    faster-whisper runs. The whisper.cpp voice-activity model is not a
    transcription model and is not in its list.
    """
    if backend_id == backend_module.BACKEND_WHISPER_CPP:
        return whisper_cpp_catalog.list_models()
    return model_catalog.list_models()


def catalogue_entry(model_id):
    """The entry of either catalogue with this id (not the VAD), or None."""
    entry = model_catalog.get_model(model_id)
    if entry is not None:
        return entry
    entry = whisper_cpp_catalog.get_model(model_id)
    return entry if entry in whisper_cpp_catalog.MODELS else None


def model_language(model_id):
    """The one language a catalogue model knows, or None (multilingual, custom,
    unknown)."""
    return getattr(catalogue_entry(model_id), "language", None)


def custom_model_reference_id(choice):
    """The reference id a model setting names, or None for a catalogue id.

    The one reading of the `external:<id>` spelling, for the code that has to
    tell the two kinds of choice apart (external_models, the model picker).
    """
    if isinstance(choice, str) and choice.startswith(CUSTOM_MODEL_PREFIX):
        return choice[len(CUSTOM_MODEL_PREFIX):] or None
    return None


def interface_language_code(ui_language):
    """The Whisper code for WinZapp's own interface language, or None.

    The app's locales are region-tagged ("pt-BR", "en-US") and Whisper's are
    not, so the region is dropped: "pt-BR" and "pt-PT" are both `pt`, which is
    the same model either way. None means Whisper has no such language — a
    perfectly ordinary answer the day WinZapp gains a locale Whisper was not
    trained on, and never a reason to refuse a transcription.
    """
    if not ui_language:
        return None
    code = str(ui_language).replace("_", "-").split("-")[0].lower()
    return code if code in LANGUAGE_NAMES else None


def language_name(code):
    """The endonym of a Whisper language code, or None for an unknown one."""
    return LANGUAGE_NAMES.get(code)


def language_choices(ui_language="") -> tuple:
    """((code, endonym), ...) for the picker, interface language first.

    Sorted by the endonym itself rather than by code, because that is the word
    the user is reading or hearing. The one exception is the language WinZapp
    is running in, which is moved to the front: it is by far the most likely
    pick, and a screen-reader user arrowing through a hundred entries to reach
    their own is the difference between a usable list and an unusable one. That
    is the *presentation* half of the "prefer the configured language" rule;
    resolve() holds the other half, and neither of them ever forces the
    interface language onto a message that was not asked to be in it.
    """
    ordered = sorted(LANGUAGE_NAMES.items(), key=lambda item: item[1].casefold())
    own = interface_language_code(ui_language)
    if own is None:
        return tuple(ordered)
    return ((own, LANGUAGE_NAMES[own]),) + tuple(
        item for item in ordered if item[0] != own
    )


def stored_models_dir(app_settings) -> str:
    """The install-wide models folder exactly as stored: "" means the default.

    A module-level function taking the `AppSettings` object rather than a
    method on the settings dialog, and the reason is not tidiness: "which
    folder is this install using" is the same question part 5c's download and
    part 6's run both have to answer, and an answer that can only be reached
    through a wx.Dialog is one they would each copy. It is also the shape a
    test can call — the version of this that lived on the dialog was reachable
    only through a stub, and the stub is what let it read a `main_window`
    attribute that production does not have.

    `None` — a legacy or account-less window with no app_settings at all —
    reads as the default folder rather than failing: the tab must still open.
    """
    if app_settings is None:
        return ""
    return str(app_settings.get(MODELS_DIR_SETTING) or "")


def resolve_models_dir(stored=None) -> str:
    """The folder the Whisper models live in.

    The stored value is install-wide (`MODELS_DIR_SETTING` in app.json) and its
    default is empty, not an absolute path: writing the resolved path at first
    launch would freeze a data directory that legitimately moves — WinZapp's
    whole data folder is meant to be copyable to another machine — and the copy
    would then keep reading a drive letter that is not there.
    """
    return str(stored) if stored else model_store.default_models_dir()


# ── Internals ────────────────────────────────────────────────────────────────


def _resolve_backend(section, available_backends, substitutions):
    """The backend id to run with, or None when nothing here can run.

    A configured backend that this install does not have falls through to the
    automatic choice rather than failing, for the reason `resolve_backend()`
    gives: a settings value outlives the installation it was written on.
    """
    known = tuple(available_backends) if available_backends is not None \
        else backend_module.BACKEND_IDS
    stored = section[SETTING_BACKEND]
    if stored != AUTO:
        if stored in known:
            return stored
        substitutions.append(Substitution(SETTING_BACKEND, str(stored)))
    return known[0] if known else None


def _resolve_device_preference(section, substitutions):
    """The device preference, which `device.resolve_device()` then interprets.

    `resolve_device()` already treats anything it does not recognise as "auto",
    so this check exists purely to *notice* that it happened: without it the
    fallback would be correct and silent, and silent is the half that costs the
    user an afternoon.
    """
    stored = section[SETTING_DEVICE]
    # `isinstance` before the membership test, and it is not defensive noise:
    # this module promises that no stored value raises, and `in` against a dict
    # hashes what it is given — a list or a dict left in settings.json by a
    # hand edit or a bad merge is a TypeError here, which in the settings tab
    # is a wx handler that never opens the dialog and at run time is a generic
    # BACKEND_ERROR out of the job's own `except Exception`.
    if isinstance(stored, str) and stored in DEVICE_PREFERENCE_I18N_KEYS:
        return stored
    substitutions.append(Substitution(SETTING_DEVICE, str(stored)))
    return device.PREFERENCE_AUTO


def _resolve_compute_type(section, substitutions):
    """The precision preference: AUTO or one of precision.COMPUTE_TYPES.

    A value this version does not know (a CTranslate2 type a later WinZapp
    offers, a hand edit) is "automatic", and reported, for the reason
    `_resolve_device_preference()` gives. Whether the device can run a known
    one is not decided here: the job decides it against its own probe.
    """
    stored = section[SETTING_COMPUTE_TYPE]
    if stored == AUTO or precision.is_choice(stored):
        return stored
    substitutions.append(Substitution(SETTING_COMPUTE_TYPE, str(stored)))
    return AUTO


def _resolve_model(section, probe, device_id, installed_ids, substitutions,
                   custom_model_ids=None, backend_id=None, language=None,
                   compute_type=None):
    """The model id, or None when nothing is installed and nothing fits.

    A model that is known but not downloaded is *kept*: that is not a
    substitution, it is MODEL_NOT_INSTALLED at run time and an offer to
    download in the settings tab. Only an id the catalogue no longer knows —
    a model retired by a later version — falls back to the automatic choice.
    A custom choice (`external:<id>`) follows the same rule with the references
    in place of the catalogue: its folder being gone is EXTERNAL_MODEL_MISSING
    at run time, and only a reference the user forgot is "no longer exists".
    """
    stored = section[SETTING_MODEL]
    if stored != AUTO:
        if _model_choice_exists(stored, custom_model_ids, backend_id):
            return stored
        substitutions.append(Substitution(SETTING_MODEL, str(stored)))
    return device.auto_select_model(
        probe, device_id, installed_ids, catalog=catalogue_models(backend_id),
        language=language, compute_type=compute_type,
    )


def _model_choice_exists(stored, custom_model_ids, backend_id=None) -> bool:
    """Whether a stored model value names something that can still exist —
    for `backend_id` when one is given, for either backend otherwise."""
    if not isinstance(stored, str):
        return False
    reference_id = custom_model_reference_id(stored)
    if reference_id is not None:
        if custom_model_ids is None:
            return True
        if reference_id not in custom_model_ids:
            return False
        if backend_id is not None and isinstance(custom_model_ids, Mapping):
            return custom_model_ids[reference_id] == backend_id
        return True
    if backend_id is None:
        return catalogue_entry(stored) is not None
    return any(model.id == stored for model in catalogue_models(backend_id))


def _resolve_language(section, ui_language, substitutions):
    """The language to transcribe in, or None to let the model detect it.

    Two settings rather than one list with a "detect automatically" entry at the
    top, because they answer two different questions and a checkbox that
    enables a combobox is what a screen reader reads best: "should each message
    be detected on its own?" and "which language, when it should not?".

    **The interface language is a preference, never an override.** The issue
    asks for the configured language to be preferred, and the obvious reading —
    "the app is in Polish, so transcribe in Polish" — is the wrong one: the
    author of that issue runs the app in Polish and receives voice notes in
    other languages, and Whisper handed a language the audio is not in does not
    fail, it confidently invents a transcription in that language. A listener
    cannot tell that from a real one. So the interface language is used in
    exactly one place, the one where the user has already said they do not want
    detection and simply never picked which language instead — the stored
    LANGUAGE_INTERFACE sentinel. With detection on, which is the default, it
    does not enter into it at all.

    An unusable stored language (a code that was never valid, or an interface
    language Whisper has no model for) falls back to detection rather than to
    some other language: detection is the automatic value for this setting, and
    guessing a second language would be the same confident-nonsense failure one
    step removed.
    """
    detect = section[SETTING_AUTO_DETECT_LANGUAGE]
    if not isinstance(detect, bool):
        # Not reported as a substitution: it is a checkbox, the corrupt value
        # carries no user intent to have been overridden, and the default is
        # also the safe answer.
        detect = DEFAULTS[SETTING_AUTO_DETECT_LANGUAGE]
    if detect:
        return None

    stored = section[SETTING_LANGUAGE]
    if stored == LANGUAGE_INTERFACE:
        own = interface_language_code(ui_language)
        if own is not None:
            return own
        if ui_language:
            substitutions.append(Substitution(SETTING_LANGUAGE, str(ui_language)))
        # An empty `ui_language` is the caller having nothing to pass, not a
        # value of the user's that stopped existing. Reporting it would say
        # "the language you chose is not available" to somebody who never chose
        # a language — a warning about a fault in the code above, spoken to the
        # one person who cannot act on it. Detection either way.
        return None
    # Guarded for the same reason as the device preference: the membership test
    # hashes, and a stored value that is not a string must fall back, not raise.
    if isinstance(stored, str) and stored in LANGUAGE_NAMES:
        return stored
    substitutions.append(Substitution(SETTING_LANGUAGE, str(stored)))
    return None


def _model_none_reason(model_id, probe, device_id):
    """Why `auto_select_model()` came back with nothing, or None if it did not.

    Read off the same probe and the same device the model was chosen against,
    which is what keeps it from becoming a second opinion: the settings tab
    asking `device.available_memory_mb()` for itself would be measuring one
    thing and displaying the outcome of another.
    """
    if model_id is not None:
        return None
    if device.available_memory_mb(probe, device_id) is None:
        return MODEL_NONE_UNMEASURED
    return MODEL_NONE_NOTHING_FITS
