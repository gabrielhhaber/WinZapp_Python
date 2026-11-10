"""What a transcription tells the user, as keys and values rather than text.

Everything below the UI speaks in codes — phases, device reasons, error codes —
precisely so the sentence can be in the user's own language. This module is
where those codes become a sentence's *identity*: an i18n key and the values it
is formatted with. It does not format anything and it does not import wx, so
part 6b receives a list of things to say and only decides how to say them —
which is `management.announcement()`'s arrangement, reused here rather than
reinvented, down to the `Announcement` type itself so the UI handles one shape.

Two things this module exists to get right, both of which are silent failures
if it does not:

* **The voice-activity filter is the one downgrade a listener cannot detect.**
  `TranscriptionResult.vad_used` is False when the filter could not load, and
  Whisper without it answers the silence at the end of a voice note with an
  invented sentence in the same voice as the real ones. There is no way to hear
  the difference, so the only protection is saying it happened — unconditionally,
  even for a result that is otherwise perfect, and even for one with nothing in
  it. Everything else here is a convenience; this is the reason.

* **A reason that is not known is not announced.** `device_reason_i18n_key()`
  answers "you asked for the processor" for anything it does not recognise,
  including None — a deliberate choice there, since an unknown *reason* still
  ran somewhere and that is the safer of the two sentences. Taken at face value
  at the start of a run, though, it produces the app confidently telling a user
  with a working graphics card that they asked for the processor, which they
  did not. So a reason this module does not recognise produces no sentence at
  all, and job.py's own comment about announcing before the probe is the state
  that reaches it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.transcription import device, errors, job, preferences
from core.transcription.management import (
    Announcement,
    OUTCOME_CANCELLED,
    OUTCOME_DONE,
    OUTCOME_FAILED,
    OUTCOME_WARNING,
)

# ── The keys ─────────────────────────────────────────────────────────────────

PHASE_I18N_KEYS = {
    job.PHASE_PREPARING_AUDIO: "transcription_phase_preparing_audio",
    job.PHASE_WAITING_FOR_SLOT: "transcription_phase_waiting_for_slot",
    job.PHASE_LOADING_MODEL: "transcription_phase_loading_model",
    job.PHASE_TRANSCRIBING: "transcription_phase_transcribing",
}

DEVICE_I18N_KEYS = {
    device.DEVICE_CUDA: "transcription_running_on_cuda",
    device.DEVICE_CPU: "transcription_running_on_cpu",
}

# The same two sentences naming the backend too, now that there are two: which
# program is running is the first thing to know when the two disagree about a
# note. `{backend}` is the backend's own name (preferences.BACKEND_I18N_KEYS).
DEVICE_WITH_BACKEND_I18N_KEYS = {
    device.DEVICE_CUDA: "transcription_running_with_backend_on_cuda",
    device.DEVICE_CPU: "transcription_running_with_backend_on_cpu",
}

#: Said with them when a single-language model runs a note in its own
#: language instead of the one the settings would have used
#: (preferences.Resolution.language_forced): detected, or chosen and replaced.
#: Said before the wait, because afterwards the user is reading text in a
#: language they may not have expected.
LANGUAGE_FORCED_I18N_KEY = "transcription_note_language_forced"
LANGUAGE_OVERRIDDEN_I18N_KEY = "transcription_note_language_overridden"

#: Said with them when the user chose a precision (part 11): the one it runs
#: in, or — when the device cannot run that one — which one replaced it.
#: Nothing is said under "automatic", which is what the run said before.
PRECISION_USED_I18N_KEY = "transcription_note_precision_used"
PRECISION_REPLACED_I18N_KEY = "transcription_note_precision_replaced"

# The device reasons worth a second sentence. The two that are missing are
# missing on purpose: CUDA_SELECTED and CPU_REQUESTED say what the sentence
# above them has just said, and device.py's own note explains why
# NO_CUDA_FOUND joins them — under "auto" the user asked for nothing in
# particular, and telling them a request was denied invents a request. What is
# left is the three states the user can act on: the card that is there and
# unusable, the driver that could not be questioned, and the libraries that are
# not installed.
DEVICE_REASON_WORTH_SAYING = (
    device.REASON_CUDA_UNAVAILABLE,
    device.REASON_CUDA_DRIVER_ERROR,
    device.REASON_CUDA_LIBRARIES_MISSING,
    # whisper.cpp's two: a card its graphics build cannot run on, and a build
    # not installed yet. Both are "you have a card and it is not being used",
    # which is worth a sentence under "automatic" too.
    device.REASON_CUDA_BUILD_UNSUPPORTED,
    device.REASON_CUDA_BUILD_MISSING,
)

NO_SPEECH_I18N_KEY = "transcription_note_no_speech"
VAD_UNAVAILABLE_I18N_KEY = "transcription_note_vad_unavailable"
LANGUAGE_DIFFERS_I18N_KEY = "transcription_note_language_differs"
LOW_CONFIDENCE_I18N_KEY = "transcription_note_low_confidence"
FINISHED_I18N_KEY = "transcription_finished"

#: Every key this module can answer with, besides the ones errors.py and
#: device.py own. The test that pins the language files reads this rather
#: than a list of its own, so a key added here cannot be added untranslated.
NARRATION_I18N_KEYS = (
    tuple(PHASE_I18N_KEYS.values())
    + tuple(DEVICE_I18N_KEYS.values())
    + tuple(DEVICE_WITH_BACKEND_I18N_KEYS.values())
    + (
        LANGUAGE_FORCED_I18N_KEY,
        LANGUAGE_OVERRIDDEN_I18N_KEY,
        PRECISION_USED_I18N_KEY,
        PRECISION_REPLACED_I18N_KEY,
        NO_SPEECH_I18N_KEY,
        VAD_UNAVAILABLE_I18N_KEY,
        LANGUAGE_DIFFERS_I18N_KEY,
        LOW_CONFIDENCE_I18N_KEY,
        FINISHED_I18N_KEY,
    )
)

# Below this, the model's own best guess at the language is less likely than
# everything else it weighed put together — which is the point where "it was
# probably Spanish" stops being worth presenting as a fact. Whisper reads only
# the first 30 seconds to decide, and a voice note that opens with a greeting
# over background noise is exactly the case it gets wrong, so the threshold is
# deliberately not lower: a warning that almost never fires protects nobody.
# Not higher either — a confident, correct detection sits well above 0.9, and
# warning about those would teach the user to ignore the warning.
LOW_CONFIDENCE_THRESHOLD = 0.5


@dataclass(frozen=True)
class Note:
    """One caveat about a transcription: `i18n.t(key).format(**values)`.

    Deliberately not an `Announcement`: an announcement carries an outcome,
    and the outcome belongs to the run as a whole. Four notes each claiming one
    would leave the UI deciding which of them wins, and the answer it would
    reach is the one `outcome_announcement()` already gives.
    """

    i18n_key: str
    values: dict = field(default_factory=dict)


# ── While it runs ────────────────────────────────────────────────────────────


def phase_i18n_key(phase):
    """The sentence for entering `phase`, or None when there is none.

    The three terminal phases have no key here — `outcome_announcement()` is
    what speaks for those, and announcing both would say "transcribing" and
    "cancelled" in the same breath. An unrecognised phase is None for the same
    reason a missing error code is not invented into a key: I18n.t() would have
    the screen reader read the code itself out loud.
    """
    return PHASE_I18N_KEYS.get(phase)


def device_announcement(device_id, device_reason, model_id, backend_name=None,
                        forced_language=None, overridden_language=None,
                        precision_chosen=None, precision_used=None) -> tuple:
    """What to say as the model starts loading: which model, and where.

    A tuple, not one Note, because this is two sentences and the second is
    usually absent: which processor is running the transcription, and — only
    when there is something the user can act on — why it is not the other one.
    Empty when the device is not yet known, which is the state job.py warns
    about: read before the hardware probe, `device` and `device_reason` are
    both None, and `device_reason_i18n_key(None)` would answer "you asked for
    the processor" on a machine whose owner asked for nothing of the sort.

    `backend_name` is the backend as it is to be said (its label, already in
    the user's language), and picks the sentence that names it too; None keeps
    the sentence without it. `forced_language` is the one language a
    single-language model was run in instead of the settings' — and
    `overridden_language` the language chosen there, when one was: both are
    codes, said by their endonym as the other language notes are.

    `precision_chosen` and `precision_used` are the precision the user chose
    and the one the run loads with, as they are to be said
    (precision.spoken_names()); `precision_chosen` is None under "automatic",
    and then nothing is said about it. When the two differ the device could
    not run the choice, and the sentence says which one replaced it — after
    the device's own reason, which is usually why.
    """
    if backend_name:
        key = DEVICE_WITH_BACKEND_I18N_KEYS.get(device_id)
        values = {"model": str(model_id or ""), "backend": str(backend_name)}
    else:
        key = DEVICE_I18N_KEYS.get(device_id)
        values = {"model": str(model_id or "")}
    if key is None:
        return ()
    notes = [Note(key, values)]
    if device_reason in DEVICE_REASON_WORTH_SAYING:
        notes.append(Note(device.device_reason_i18n_key(device_reason)))
    if precision_chosen:
        if precision_used and precision_used != precision_chosen:
            notes.append(Note(PRECISION_REPLACED_I18N_KEY,
                              {"chosen": precision_chosen, "used": precision_used}))
        else:
            notes.append(Note(PRECISION_USED_I18N_KEY, {"precision": precision_chosen}))
    if forced_language:
        language = preferences.language_name(forced_language) or forced_language
        chosen = preferences.language_name(overridden_language) if overridden_language else None
        if chosen:
            notes.append(Note(LANGUAGE_OVERRIDDEN_I18N_KEY,
                              {"language": language, "chosen": chosen}))
        else:
            notes.append(Note(LANGUAGE_FORCED_I18N_KEY, {"language": language}))
    return tuple(notes)


# ── Once it has finished ─────────────────────────────────────────────────────


def result_notes(result, preferred_language=None) -> tuple:
    """The caveats about a finished result, in the order they should be said.

    `preferred_language` is `preferences.preferred_language()`'s answer — the
    language the user would rather hear, which is never imposed on the run and
    so can only be checked afterwards, here.
    """
    if result is None:
        return ()

    notes = []
    if result.is_empty:
        notes.append(Note(NO_SPEECH_I18N_KEY))

    if result.vad_used is False:
        # Unconditional, including on an empty result and including alongside
        # every other note: this is the one downgrade with no symptom. A result
        # produced without the filter is not a worse-looking transcription, it
        # is an identical-looking one that may end in a sentence nobody said.
        #
        # Which is why the sentence says the transcription may not be reliable
        # rather than describing the text: firing on an empty result is the
        # point, and there the text this warning is about does not exist.
        notes.append(Note(VAD_UNAVAILABLE_I18N_KEY))

    if result.is_empty:
        # Nothing was transcribed, so there is no text for a language to be
        # wrong about. Whisper still reports a language for a note of pure
        # noise, and passing that on as "this was transcribed as Finnish" would
        # be describing content that does not exist.
        return tuple(notes)

    detected = getattr(result, "language", None)
    endonym = preferences.language_name(detected)
    if preferred_language and detected and detected != preferred_language and endonym:
        # The comparison decides *whether* this is worth saying; the sentence
        # itself claims nothing about how the preference got there, and must
        # not. On factory settings `SETTING_LANGUAGE` is the `interface`
        # sentinel and `preferred_language()` resolves it to the UI language
        # without consulting `auto_detect_language` — which is on, which leaves
        # the language combobox greyed out in the tab. So a user who has never
        # been offered the choice still lands here on every message in another
        # language, and "not the language you chose" would name a decision they
        # were never allowed to make. A plain statement of fact stays true for
        # both of them.
        #
        # The endonym, never the code: "polski" is what a Polish user
        # recognises, and "pl" is what a screen reader spells out letter by
        # letter. A language Whisper reports and this table does not know is
        # left unmentioned rather than named by its code.
        notes.append(Note(LANGUAGE_DIFFERS_I18N_KEY, {"language": endonym}))

    probability = getattr(result, "language_probability", None)
    if probability is not None and probability < LOW_CONFIDENCE_THRESHOLD:
        # Carries no language on purpose, so that it still fires for a
        # detection this table cannot name — an unsure answer is worth saying
        # even when the answer itself is unsayable.
        notes.append(Note(LOW_CONFIDENCE_I18N_KEY))

    return tuple(notes)


def outcome_announcement(result=None, error=None) -> Announcement:
    """The one headline for a finished run, with the outcome the UI sounds on.

    Note the deliberate overlap with `result_notes()`: for a result holding no
    speech, the caveat *is* the headline, and both answer with
    NO_SPEECH_I18N_KEY rather than with two translations of the same sentence.
    A caller that speaks the announcement and then the notes drops the note
    whose key the announcement already used.
    """
    if error is not None:
        code = getattr(error, "code", None)
        outcome = OUTCOME_CANCELLED if code == errors.CANCELLED else OUTCOME_FAILED
        # errors.error_i18n_key() owns the code-to-sentence map, including what
        # an unrecognised code falls back to; a second copy of it here is how
        # the two start disagreeing.
        return Announcement(errors.error_i18n_key(code), outcome,
                            errors.error_i18n_values(code))

    if result is None:
        # Neither a result nor an error: nothing a job can legitimately report,
        # and saying "finished" about a transcription that produced nothing we
        # can see would be a guess — the same answer management.announcement()
        # gives an action it was never taught.
        return Announcement(errors.error_i18n_key(errors.BACKEND_ERROR),
                            OUTCOME_FAILED, {})

    if result.is_empty:
        return Announcement(NO_SPEECH_I18N_KEY, OUTCOME_WARNING, {})
    return Announcement(FINISHED_I18N_KEY, OUTCOME_DONE, {})


def cpu_retry_note(error, device_used=device.DEVICE_CUDA):
    """The offer to run this failed transcription again on the processor.

    None when there is no offer worth making — `device.should_retry_on_cpu()`
    decides that, and its allowlist explains why offering the whole wait a
    second time for a failure the processor would hit too is worse than
    offering nothing.
    """
    key = device.cpu_retry_i18n_key(error, device_used)
    return Note(key) if key else None
