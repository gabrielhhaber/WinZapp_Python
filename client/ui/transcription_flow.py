"""transcription_flow.py — transcribing one message, from the keypress to the text.

`ConversationsPanel` only wires this in (a context-menu item and Alt+Shift+T);
everything between "the user asked" and "the text is in front of them" is
here, and everything slow is further down, in `core.transcription.message_run`,
which runs on a worker thread behind the progress dialog part 5c-2 built.

What the user hears, and what only appears. A finished transcription can carry
a headline and up to three caveats — about sixty words in a row, read out over
whatever the user does next. So:

* **Spoken**: the headline ("finished", "no speech was found", the error), and
  the voice-filter warning *always*. That warning is the one degradation a
  listener cannot detect — without the filter Whisper can end a note with a
  sentence nobody said, in the same voice as the real ones — so it may not
  depend on the user tabbing to it.
* **Shown only**: the language and confidence caveats. They sit in the result
  window's own read-only field, above the text, where Shift+Tab reaches them,
  and the spoken headline ends with a four-word pointer to them ("there are
  warnings above the text") when there are any — the pointer costs less
  attention than the notes themselves and still says they exist.

Each failure is reported exactly once. The media download goes through
`ConversationsPanel._download_media_to_disk()`, which says nothing, rather
than `_ensure_media_on_disk()`, which says it itself — with a message box
posted through `wx.CallAfter` that would surface on top of the modal progress
dialog, and a second sentence from here would then be the same failure told
twice. The sentences for "offline" and "the download failed" are the ones the
rest of the app already uses for media, chosen here from `run.media_status`.

Speech lands *after* the focus move it belongs to. Every outcome ends by
putting the focus somewhere — the message the user came from, the message
field, the result window — and a screen reader cancels its speech when the
focus changes. So the focus moves first, and the sentence follows through
`wx.CallAfter`; in the result window it is spoken from inside its own modal
loop for the same reason (see transcription_result.py).

The re-run on the processor is the one place a second progress dialog opens.
It is a new run the user explicitly agreed to after being told why the first
failed, not the same wait continuing, and the question has to be asked with no
progress dialog on screen — a Yes/No box on top of a modal whose only button
is Cancel would leave the user two dialogs deep in the middle of a run.

A finished transcription is kept with its message (part 7,
`core.transcription.stored`), and from then on the message offers it instead
of the wait: Alt+Shift+T and `transcription_view` open the same result window
with the stored text, saying when and with which model it was made, and
repeating every note that still applies — the voice-filter warning above all,
since a transcription made without the filter is exactly as untrustworthy the
tenth time it is read. `transcription_transcribe_again` is the run above; its result
replaces the stored one. `transcription_delete` asks first, because it throws
away minutes of work with one key. A run that finishes after the sender deleted
the message for everyone is neither kept nor shown: one sentence says so, and
no window opens (`WITHDRAWN_I18N_KEY`).

A locked chat's content stays behind the vault, and the vault can close in
the middle of a run: the auto-lock timer fires inside the progress dialog's
modal loop, closes the conversation and says so. So before anything of the
result is shown or said, the vault is asked again whether the chat is hidden
now (`MainWindow.is_chat_hidden_by_vault()`, `_hidden_by_vault()` here). If it
is, no window opens, no headline or note is said — "no speech was found" and
the detected language are about the recording too — and the focus stays where
the vault put it. The result is still kept when storing still finds the
message with the conversation closed: it goes into the message's encrypted
record like the rest of that chat, where nothing shows it until the vault is
opened, and it spares the user the minutes they already waited. Storing
finds it in the chat's own records; a note brought in by "mensagens
anteriores" lives only in the conversation panel's lists, which the closed
conversation no longer offers, so it is not kept (SAVE_MISSING) where it
would have been with the vault open. One sentence says which of the two
happened, and nothing more (`HIDDEN_BY_VAULT_I18N_KEY`). Failures and
cancellations are still said as always: they describe the run, not the note.
The same question is asked once more when the result window closes through
"Inserir na mensagem": the vault can close while the user is reading, and the
message field would carry the text into whichever conversation opens next.
That refusal has a sentence of its own (`INSERT_REFUSED_BY_VAULT_I18N_KEY`):
nothing finished then — the window may hold a transcription reopened from
storage — and what the user needs to hear is that the text was not inserted.

Nothing here logs the message, its id, the contact or a path: the same rule as
the whole transcription package, checked by `tests/test_transcription_flow.py`.
"""

import logging
import time
from datetime import datetime

import wx

from app_paths import data_path
from coord_locks import LockTimeout
from core.locale_format import get_datetime_format
from core.transcription import (
    audio_prep,
    backend as backend_module,
    errors,
    external_models,
    external_view,
    job as job_module,
    management,
    message_audio,
    message_run,
    model_names,
    narration,
    precision,
    preferences,
    stored as stored_transcription,
)
from core.utils import is_phone_like
from ui.dialogs.transcription_progress import TranscriptionProgressDialog
from ui.dialogs.transcription_result import TranscriptionResultDialog, default_file_name

#: The phase this layer adds, in the same shape as narration.PHASE_I18N_KEYS.
_OWN_PHASE_I18N_KEYS = {
    message_run.PHASE_DOWNLOADING_MEDIA: "transcription_phase_downloading_audio",
}

#: The app's existing media-download sentences, reused rather than translated
#: again: "wait for the connection" and "the link may have expired" are what a
#: user already hears when opening or saving the same media.
_MEDIA_STATUS_I18N_KEYS = {
    message_run.MEDIA_OFFLINE: "media_download_offline",
    message_run.MEDIA_FAILED: "media_download_failed",
    # The transcription's own: an unsent note whose file is still being
    # written is no download at all, and neither existing sentence is true
    # of it.
    message_run.MEDIA_PREPARING: "transcription_media_still_preparing",
    # Also the transcription's own, and deliberately not "try again": the
    # send failed before the file was written, so no later attempt finds it.
    message_run.MEDIA_SEND_FAILED: "transcription_media_send_failed",
}

#: The failures whose answer lives on the Transcription tab — download a model,
#: pick another, repair the one that is there — and which are therefore
#: offered with a shortcut to it rather than only said.
SETTINGS_OFFER_CODES = (
    errors.MODEL_NOT_INSTALLED,
    errors.MODEL_CORRUPTED,
    # The folder of a model the user pointed WinZapp at is gone or changed:
    # the tab is where it is checked again, forgotten, or replaced by another.
    errors.EXTERNAL_MODEL_MISSING,
    errors.EXTERNAL_MODEL_CHANGED,
    # The whisper.cpp program is installed, repaired or removed there too.
    errors.WHISPER_CPP_NOT_INSTALLED,
    errors.WHISPER_CPP_CORRUPTED,
    # The device refused the precision chosen there (part 11).
    errors.PRECISION_UNSUPPORTED,
)

AUTO_MODEL_UNAVAILABLE_I18N_KEY = "transcription_model_unavailable_auto"
HAS_NOTES_I18N_KEY = "transcription_result_has_notes"

#: The headline of a stored transcription opened again, with and without the
#: model — a stored value always has one, but a sentence ending in "with the
#: model ." is what a damaged one would otherwise say.
SAVED_OPENED_I18N_KEY = "transcription_saved_opened"
SAVED_OPENED_NO_MODEL_I18N_KEY = "transcription_saved_opened_no_model"
#: The same two naming the backend too, once there are two that make
#: different text from the same note. Every record carries one (since
#: ffa5e5a5, which first stored them); a value that names none, or one this
#: version does not know, keeps the sentence without it.
SAVED_OPENED_BACKEND_I18N_KEY = "transcription_saved_opened_backend"
SAVED_OPENED_NO_MODEL_BACKEND_I18N_KEY = "transcription_saved_opened_no_model_backend"

#: Said when a transcription is put in the message's line: the text itself.
INLINE_SPOKEN_I18N_KEY = "transcription_inline_spoken"

#: The note for a fresh result that was not kept, by what storing answered.
NOT_SAVED_I18N_KEYS = {
    stored_transcription.SAVE_UNSENT: "transcription_not_saved_unsent",
    stored_transcription.SAVE_MISSING: "transcription_not_saved_missing",
    stored_transcription.SAVE_DISABLED: "transcription_not_saved_cache_off",
}

#: What is said, instead of opening the result window, when the message was
#: deleted for everyone while it was being transcribed (SAVE_WITHDRAWN).
WITHDRAWN_I18N_KEY = "transcription_discarded_withdrawn"

#: The note for a fresh result that storing raised on. MainWindow's own
#: sentence for a background write the database refused
#: (_say_transcription_not_stored()), so the two failures to keep the text
#: are said the same way.
STORE_FAILED_I18N_KEY = "transcription_store_failed"

#: What is said, instead of anything about the result, when the chat has been
#: locked away by the vault while it was being transcribed — kept, and not.
HIDDEN_BY_VAULT_I18N_KEY = "transcription_hidden_vault_closed"
HIDDEN_BY_VAULT_NOT_SAVED_I18N_KEY = "transcription_hidden_vault_closed_not_saved"

#: What is said when "Inserir na mensagem" is refused because the vault closed
#: while the result window was open.
INSERT_REFUSED_BY_VAULT_I18N_KEY = "transcription_insert_refused_vault_closed"

#: Every key this module asks for besides the ones narration/errors/preferences
#: own. The i18n test reads this rather than a list of its own.
FLOW_I18N_KEYS = (
    tuple(_OWN_PHASE_I18N_KEYS.values())
    + (
        AUTO_MODEL_UNAVAILABLE_I18N_KEY,
        HAS_NOTES_I18N_KEY,
        "transcription_no_audio",
        INLINE_SPOKEN_I18N_KEY,
        "transcription_starting",
        "transcription_open_settings_question",
        "transcription_result_title",
        SAVED_OPENED_I18N_KEY,
        SAVED_OPENED_NO_MODEL_I18N_KEY,
        SAVED_OPENED_BACKEND_I18N_KEY,
        SAVED_OPENED_NO_MODEL_BACKEND_I18N_KEY,
        WITHDRAWN_I18N_KEY,
        STORE_FAILED_I18N_KEY,
        HIDDEN_BY_VAULT_I18N_KEY,
        HIDDEN_BY_VAULT_NOT_SAVED_I18N_KEY,
        INSERT_REFUSED_BY_VAULT_I18N_KEY,
        "transcription_delete_question",
        "transcription_deleted",
        "transcription_delete_failed",
    )
    + tuple(NOT_SAVED_I18N_KEYS.values())
    + tuple(_MEDIA_STATUS_I18N_KEYS.values())
)


# ── Pure decisions (no wx.App needed to test them) ───────────────────────────


def phase_status_text(i18n, phase, run):
    """The line for entering `phase`, or None when the phase says nothing.

    **Called from the `on_phase` callback itself**, on the thread that entered
    the phase, and that is what makes the device sentence true: job.py fills
    in `device` / `device_reason` immediately before announcing the loading
    phase, and read any earlier they are None — which narration turns into no
    sentence at all rather than into "you asked for the processor" on a
    machine with a working graphics card. Folded into the same line as the
    phase so the dialog shows it and the screen reader says it in one breath.
    """
    key = narration.phase_i18n_key(phase) or _OWN_PHASE_I18N_KEYS.get(phase)
    if key is None:
        return None
    parts = [i18n.t(key)]
    if phase == job_module.PHASE_LOADING_MODEL and run is not None:
        chosen, used = precision.spoken_names(i18n, getattr(run, "precision", None))
        notes = narration.device_announcement(
            run.device, run.device_reason, spoken_model_name(i18n, run),
            backend_name=model_names.backend_name(i18n, getattr(run, "backend_id", None)),
            forced_language=getattr(run, "forced_language", None),
            overridden_language=getattr(run, "overridden_language", None),
            precision_chosen=chosen,
            precision_used=used,
        )
        for note in notes:
            parts.append(i18n.t(note.i18n_key).format(**note.values))
    return " ".join(parts)


def spoken_model_name(i18n, run):
    """The run's model as the picker names it ("small, 5 bits", "small.en,
    English only"), or its folder's name for a custom model.

    `run.model_name` is the fallback — what a run whose reference list is not
    known names, and never the raw `external:<id>`.
    """
    return model_names.display_name(
        i18n, getattr(run, "model_id", None), getattr(run, "external_references", ())
    ) or run.model_name


def split_result_notes(announcement, notes):
    """(spoken, shown) — which caveats are said out loud and which the window shows.

    `spoken` is the voice-filter warning when there is one, and only that; see
    the module docstring for why it alone may not wait to be found. `shown` is
    every note except the one the headline already is (narration's no-speech
    overlap), including the spoken warning — the window has to hold everything
    the user was told, for a second reading.
    """
    shown = tuple(n for n in notes if n.i18n_key != announcement.i18n_key)
    spoken = tuple(n for n in shown if n.i18n_key == narration.VAD_UNAVAILABLE_I18N_KEY)
    return spoken, shown


def result_speech(i18n, announcement, notes, window):
    """The one sentence said when a transcription finishes.

    `window` is whether a result window will show the notes; without one (an
    empty result) there is nowhere to point to, so no pointer is added.
    """
    spoken, shown = split_result_notes(announcement, notes)
    parts = [i18n.t(announcement.i18n_key).format(**announcement.values)]
    parts.extend(i18n.t(n.i18n_key).format(**n.values) for n in spoken)
    if window and len(shown) > len(spoken):
        parts.append(i18n.t(HAS_NOTES_I18N_KEY))
    return " ".join(parts)


def inline_speech(i18n, text, spoken_notes):
    """What is said when a transcription lands in the message's line: the text,
    then the voice-filter warning when there is one (it may never wait to be
    found — see the module docstring)."""
    parts = [i18n.t(INLINE_SPOKEN_I18N_KEY).format(text=" ".join((text or "").split()))]
    parts.extend(i18n.t(n.i18n_key).format(**n.values) for n in spoken_notes)
    return " ".join(parts)


def model_problem_i18n_key(error_code, resolution, settings):
    """The sentence for "there is no model to run this with".

    Three situations reach it, and one sentence would be false for two of
    them: nothing fits this machine (or it could not be measured) —
    preferences' own keys; the user chose a model that is not downloaded —
    errors' own key, which says "the model you chose"; and the automatic
    choice landed on a model that is not downloaded, where "the model you
    chose" names a choice nobody made. And one that is not about the model:
    the device refused the precision chosen on the same tab, whose own
    sentence says so.
    """
    if error_code == errors.PRECISION_UNSUPPORTED:
        return errors.error_i18n_key(error_code)
    if error_code in (errors.MODEL_CORRUPTED, errors.EXTERNAL_MODEL_MISSING,
                      errors.EXTERNAL_MODEL_CHANGED,
                      errors.WHISPER_CPP_NOT_INSTALLED, errors.WHISPER_CPP_CORRUPTED):
        # The code already says which; the stored choice cannot make them any
        # truer (an external model's folder is gone whether it was picked or
        # chosen automatically). A whisper.cpp model elsewhere is one file,
        # and is called one.
        is_file = (getattr(resolution, "backend_id", None)
                   == backend_module.BACKEND_WHISPER_CPP)
        return external_view.for_reference(errors.error_i18n_key(error_code), is_file)
    if resolution is None:
        return errors.error_i18n_key(errors.MODEL_NOT_INSTALLED)
    if resolution.model_id is None:
        return preferences.MODEL_NONE_I18N_KEYS.get(
            resolution.model_none_reason, preferences.MODEL_NONE_I18N_KEYS[
                preferences.MODEL_NONE_NOTHING_FITS]
        )
    stored = preferences.read_section(settings)[preferences.SETTING_MODEL]
    replaced = any(s.setting == preferences.SETTING_MODEL for s in resolution.substitutions)
    if stored == preferences.AUTO or replaced:
        return AUTO_MODEL_UNAVAILABLE_I18N_KEY
    return errors.error_i18n_key(errors.MODEL_NOT_INSTALLED)


def insertion_text(text, before, after):
    """`text` as it should go into the message field between `before` and `after`.

    Only spaces are added, and only where the text would otherwise run into a
    word the user had typed: inserting "see you at five" after "ok" must not
    produce "oksee you at five". After the text, only before a letter or a
    digit — a space in front of the comma the user already typed would be
    one more thing to fix by ear.
    """
    text = (text or "").strip()
    if not text:
        return ""
    if before and not before.isspace():
        text = " " + text
    if after and after.isalnum():
        text = text + " "
    return text


def title_name(panel, msg):
    """Who the message is from, by name — never a JID.

    The conversation's own name as the panel shows it, and in a group the
    participant who sent it first, since "a message in the family group" does
    not say whose voice it is.

    Only a participant who resolves to a *name*. `_get_participant_name()`
    falls back to a formatted phone number, and for an `@lid` nobody has
    mapped yet to the bare digits of the JID; either one in the title is a
    string NVDA reads digit by digit on every focus of the window, and the
    same digits would go on into the file name the Save dialog offers. The
    group's name alone says less, but everything it says is readable.
    """
    chat = getattr(panel, "conversation_name", "") or ""
    key = msg.get("key") or {}
    remote = key.get("remoteJid") or ""
    if remote.endswith("@g.us") and not key.get("fromMe"):
        participant = key.get("participant") or msg.get("participant") or ""
        if participant:
            who = panel._get_participant_name(participant, msg)
            if who and "@" not in who and not is_phone_like(who):
                return f"{who}, {chat}" if chat else who
    return chat


def saved_when(i18n, at) -> str:
    """When a stored transcription was made, in the user's own regional format.

    The same pattern the conversation uses for a message's date
    (`get_datetime_format()` over the language file's `datetime_fmt`), so the
    two read alike.
    """
    try:
        return datetime.fromtimestamp(at).strftime(get_datetime_format(i18n.t("datetime_fmt")))
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def saved_announcement(i18n, value, external_references=()) -> management.Announcement:
    """The spoken headline for opening `value`, a stored transcription.

    When and with which model go in the headline, not among the notes: the
    notes field is announced as "there are warnings above the text", and the
    date is not a warning — putting it there would make that pointer fire on
    every reopening and teach the user to ignore it.
    """
    # When the text was made, not when it was last decided about (clearing
    # another model's results promotes this one at a later decision time).
    when = saved_when(i18n, stored_transcription.made_time(value))
    # A custom model is named by its folder, and by nothing once its
    # reference was forgotten: the raw `external:<id>` is not a name.
    model = model_names.display_name(
        i18n, str(value.get("model_id") or ""), external_references
    ) or ""
    backend = model_names.backend_name(i18n, value.get("backend"))
    if model and backend:
        return management.Announcement(
            SAVED_OPENED_BACKEND_I18N_KEY, management.OUTCOME_DONE,
            {"when": when, "model": model, "backend": backend},
        )
    if model:
        return management.Announcement(
            SAVED_OPENED_I18N_KEY, management.OUTCOME_DONE, {"when": when, "model": model}
        )
    if backend:
        return management.Announcement(
            SAVED_OPENED_NO_MODEL_BACKEND_I18N_KEY, management.OUTCOME_DONE,
            {"when": when, "backend": backend},
        )
    return management.Announcement(
        SAVED_OPENED_NO_MODEL_I18N_KEY, management.OUTCOME_DONE, {"when": when}
    )


def open_transcription_settings(main_window):
    """Open Settings directly on the Transcription tab.

    Selecting the tab is the dialog's own `show_transcription_tab()`, not
    done from here: selecting it the ordinary way, before `ShowModal()`,
    enters the tab with no window on screen and spends its one-time warning
    where nobody hears it — the dialog's docstring says how.
    """
    from ui.dialogs.settings_dialog import SettingsDialog

    dlg = SettingsDialog(main_window)
    try:
        dlg.show_transcription_tab()
    finally:
        dlg.Destroy()


# ── The flow ─────────────────────────────────────────────────────────────────


class MessageTranscriptionFlow:
    """One transcription of one message. Built on the wx thread, used once.

    Methods run on the wx thread except the two marked otherwise, which the
    run calls from its worker threads and which may only cross back with
    `wx.CallAfter` (pinned by a test).
    """

    def __init__(self, panel, msg, inline=False):
        self._panel = panel
        #: (backend, model) selected now: a stored transcription counts only if
        #: that model made it (stored.saved_transcription()).
        self._selection = stored_transcription.selection_from_settings(
            getattr(panel.main_window, "settings", None))
        #: Whether the text goes into the message's own line of the list
        #: instead of a result window (Alt+Shift+N, or Enter set to transcribe).
        self._inline = bool(inline)
        self._main_window = panel.main_window
        self._i18n = self._main_window.i18n
        self._msg = msg
        self._msg_id = (msg.get("key") or {}).get("id", "") if isinstance(msg, dict) else ""
        # The chat the message is shown in, as the panel's own writes use it
        # (`_persist_message_local_flags()`): that is the jid its row is
        # stored under, which a group member's or an @lid's key need not be.
        conversation = getattr(panel, "conversation", None)
        self._jid = conversation.get("remoteJid", "") if isinstance(conversation, dict) else ""
        #: The progress dialog on screen, while there is one.
        self._dialog = None
        #: The run currently behind it — read by `_on_phase` for the device.
        self._run = None
        #: True for a run with no progress window (`_start_in_background`).
        self._background = False

    def start(self):
        """Transcribe the message, report, and put the focus back."""
        if self._hidden_by_vault():
            # Not reachable from the conversation — a locked chat is open only
            # while the vault is — and kept so that no future entry point can
            # spend minutes producing a text it may not show.
            return
        if not message_audio.is_transcribable(self._msg):
            # Nothing opens: the user pressed a shortcut on a message that
            # holds no speech, and the answer is one short sentence.
            self._main_window.output(self._i18n.t("transcription_no_audio"))
            return

        if self._runs_without_dialog():
            self._start_in_background()
            return
        self._prepare()
        try:
            run, result, error = self._run_behind_dialog(self._make_first_run)
            self._finish(run, result, error)
        finally:
            self._cleanup()

    def _prepare(self):
        """Before the run is made; `_cleanup()` after it has been reported."""

    def _cleanup(self):
        pass

    def _finish(self, run, result, error):
        """From a finished run to its report: the offer to redo it on the
        processor, then what the user is told."""
        handover = getattr(run, "prepared_handover", None)
        declined = False
        try:
            if error is not None and handover is not None:
                offer = narration.cpu_retry_note(error, run.device)
                if offer is not None:
                    if self._ask_retry(self._i18n.t(offer.i18n_key)):
                        first = run
                        run, result, error = self._run_behind_dialog(
                            lambda on_progress, on_finished: self._make_retry_run(
                                first, on_progress, on_finished
                            )
                        )
                    else:
                        declined = True
        finally:
            # The converted audio of the whole recording, handed over by a run
            # that failed on the card. Ours whether the re-run happened, was
            # declined, or was never offered — and gone before the result
            # window opens, since nothing past this point needs it.
            audio_prep.discard(handover)

        if declined:
            # The question already said what failed and why; the user has
            # answered it. Saying the failure again now would be the same
            # report twice.
            self._focus_message()
            return
        self._report(run, result, error)

    # ── Without the progress window ──────────────────────────────────────────
    # "Transcribe without the progress window" (transcription.run_without_dialog):
    # the user stays where they are, and the result arrives when it is ready.
    # One such run at a time, kept on the main window so the same shortcut can
    # cancel it and quitting can stop it (MainWindow.stop_transcription_runs).

    def _runs_without_dialog(self):
        section = self._main_window.settings.get("transcription")
        return isinstance(section, dict) and section.get("run_without_dialog") is True

    def _start_in_background(self):
        mw = self._main_window
        i18n = self._i18n
        active = getattr(mw, "_background_transcription", None)
        if active is not None:
            if active.targets(self):
                # The same shortcut on the same message: that is Cancel.
                active.cancel_background()
                mw.output(i18n.t("transcription_background_cancelling"))
            else:
                mw.output(i18n.t("transcription_background_busy"))
            return
        self._background = True
        self._prepare()
        mw._background_transcription = self
        mw.output(i18n.t("transcription_background_started"))
        try:
            self._make_first_run(lambda tick: None, self._background_done).start()
        except Exception as exc:
            # Nothing would ever report: say it failed rather than stay "busy".
            logging.error("[transcription] a run without the progress window could not start: %s",
                          errors.exception_report(exc))
            self._background_finished(None, errors.TranscriptionError(
                errors.BACKEND_ERROR, f"{type(exc).__name__}: {exc}"))

    def targets(self, other):
        """Whether `other` asks for the message (or status) this one is on."""
        return type(other) is type(self) and other._msg_id == self._msg_id

    def cancel_background(self):
        """The run this flow has under way, to cancel it; None when none."""
        run = self._run
        if run is not None:
            run.cancel()
        return run

    def _background_done(self, result, error):
        # On the run's thread: see "Called on the run's threads" below.
        wx.CallAfter(self._background_finished, result, error)

    def _background_finished(self, result, error):
        mw = self._main_window
        if getattr(mw, "_background_transcription", None) is self:
            mw._background_transcription = None
        try:
            self._finish(self._run, result, error)
        finally:
            self._cleanup()

    # ── Running ──────────────────────────────────────────────────────────────

    def _run_behind_dialog(self, make_run):
        """(run, result, error) — one run, behind one progress dialog."""
        dialog = TranscriptionProgressDialog(
            self._main_window,
            self._i18n,
            self._main_window.speak_output,
            make_run,
            self._i18n.t("transcription_starting"),
        )
        self._dialog = dialog
        try:
            dialog.run()
            return dialog.job, dialog.result, dialog.error
        finally:
            self._dialog = None
            dialog.Destroy()

    def _make_first_run(self, on_progress, on_finished):
        mw = self._main_window
        stored_models_dir, references = self._install_wide_models()
        run = message_run.MessageTranscription(
            self._msg,
            mw.settings,
            mw.key,
            data_path("voice_messages"),
            data_path("media"),
            stored_models_dir=stored_models_dir,
            ui_language=getattr(self._i18n, "language", ""),
            external_references=references,
            find_ffmpeg=mw._find_api_ffmpeg,
            is_online=lambda: bool(getattr(mw, "_wa_connected", False)),
            fetch_media=self._panel._download_media_to_disk,
            on_phase=self._on_phase,
            on_progress=on_progress,
            on_finished=on_finished,
        )
        self._run = run
        return run

    def _install_wide_models(self):
        """(stored models folder, external references or None) from app.json.

        `_app_settings`, with the underscore — the attribute the window really
        has. SettingsDialog._install_wide_settings() records what the other
        spelling cost: a folder that was never read back.

        Read here, on the wx thread, inside the progress dialog's constructor,
        where a LockTimeout (another WinZapp window holding app.json past the
        lock's wait) would escape the key handler. Instead the run is made
        with the default folder and the references unknown (None): a model
        complete in the default folder still runs, and anything else stops on
        the worker with "another window is busy, try again"
        (MessageTranscription._check_references_were_read()).
        """
        app_settings = getattr(self._main_window, "_app_settings", None)
        try:
            stored_models_dir = preferences.stored_models_dir(app_settings)
        except LockTimeout:
            logging.warning("[transcription] app.json was held; the run reads no install-wide settings")
            return "", None
        references, known = external_models.read_references(app_settings)
        return stored_models_dir, references if known else None

    def _make_retry_run(self, previous, on_progress, on_finished):
        run = message_run.MessageTranscription.retry_on_cpu(
            previous,
            on_phase=self._on_phase,
            on_progress=on_progress,
            on_finished=on_finished,
        )
        self._run = run
        return run

    # ── Called on the run's threads ──────────────────────────────────────────
    # Nothing below this line may touch a wx control, the panel or the speech
    # output directly — only through wx.CallAfter.

    def _on_phase(self, phase):
        text = phase_status_text(self._i18n, phase, self._run)
        if text:
            wx.CallAfter(self._show_status, text)

    # ── Back on the wx thread ────────────────────────────────────────────────

    def _show_status(self, text):
        dialog = self._dialog
        if dialog:
            dialog.set_status(text)

    def _ask_retry(self, question):
        """Yes/No on redoing the run on the processor.

        The sentence is the dialog's text and is not spoken as well: every
        screen reader reads a message box as it opens, and speaking it too
        would say the same thing twice, the second copy cut by the first.
        """
        self._play_error_sound()
        dlg = wx.MessageDialog(
            self._main_window, question, self._i18n.t("transcription_progress_title"),
            wx.YES_NO | wx.ICON_QUESTION,
        )
        try:
            return dlg.ShowModal() == wx.ID_YES
        finally:
            dlg.Destroy()

    def _report(self, run, result, error):
        i18n = self._i18n
        if error is not None:
            code = getattr(error, "code", None)
            media_status = getattr(run, "media_status", None)
            if code == errors.MEDIA_NOT_DOWNLOADED and media_status in _MEDIA_STATUS_I18N_KEYS:
                self._say_after_focus(i18n.t(_MEDIA_STATUS_I18N_KEYS[media_status]), failed=True)
                return
            if code in SETTINGS_OFFER_CODES:
                key = model_problem_i18n_key(
                    code, getattr(run, "resolution", None), self._main_window.settings
                )
                self._offer_settings(i18n.t(key))
                return
            announcement = narration.outcome_announcement(None, error)
            self._say_after_focus(
                i18n.t(announcement.i18n_key).format(**announcement.values),
                failed=announcement.outcome == management.OUTCOME_FAILED,
            )
            return

        announcement = narration.outcome_announcement(result, None)
        notes = narration.result_notes(
            result,
            preferences.preferred_language(
                self._main_window.settings, getattr(i18n, "language", "")
            ),
        )
        if result is None or result.is_empty:
            if self._hidden_by_vault():
                # "No speech was found" is about the recording, as much as
                # its text would be. Nothing was kept either — see below.
                self._say_hidden_by_vault(kept=False)
                return
            # No window for an empty result: an empty text box is
            # indistinguishable from a bug to someone who cannot see it. The
            # sentence says there was no speech, and the filter warning still
            # goes with it — a note of pure noise transcribed without the
            # filter is exactly where the invented sentence would have been.
            # Nor is it stored: there is nothing to reuse, and a second run
            # that heard nothing must not replace a first one that did.
            self._say_after_focus(result_speech(i18n, announcement, notes, window=False))
            return

        store_raised = False
        try:
            answer = self._store(result)
        except Exception as exc:
            # Storing runs here, on the wx thread, before the window below has
            # opened, and nothing above this call catches anything but
            # sys.excepthook — whose generic dialog would cost the user the
            # text of minutes of transcription. The text is what they waited
            # for, so it is shown anyway, and the note says it was not kept.
            logging.error("[transcription] storing a transcription raised: %s",
                          errors.exception_report(exc))
            # Only noted: the window opens after this block, not inside it.
            # Raised from in here, anything the dialog raised would carry this
            # exception along as its __context__ to sys.excepthook, which
            # writes format_exception() to the log with nothing scrubbed.
            store_raised = True
            answer = None
        if self._hidden_by_vault():
            # Asked after storing, not before: keeping it reveals nothing (see
            # the module docstring), and the sentence has to say whether the
            # text will be there once the vault is opened. Ahead of every
            # answer below, a withdrawal included — that one is news about
            # the locked chat as well, and "not kept" is all that is true of
            # it that the user can act on.
            self._say_hidden_by_vault(kept=answer == stored_transcription.SAVE_STORED)
            return
        if store_raised:
            if not self._withdrawn_now():
                # Once, here: the window plays nothing of its own, and the
                # note inside it is what says why the sound played.
                self._play_error_sound()
                notes = tuple(notes) + (narration.Note(STORE_FAILED_I18N_KEY),)
                self._open_result_window(result, announcement, notes)
                return
            # The one answer that still outranks showing the text (see just
            # below), even when storing never got as far as asking.
            answer = stored_transcription.SAVE_WITHDRAWN
        if answer == stored_transcription.SAVE_WITHDRAWN:
            # Not shown either, only said. The sender deleted the message for
            # everyone while it was being transcribed, and WinZapp takes a
            # withdrawn message's content off the screen the moment the
            # revoke arrives, playable audio included
            # (MainWindow._apply_remote_revoke()). The result window would
            # put that content back in another form — one that can be saved
            # to a file or inserted into the message field. The minutes of
            # work are lost, and the sentence says why.
            #
            # With the error sound, like every other run that ends without
            # the text the user asked for — a media status (the note expired
            # or was deleted) is the closest of them, and it sounds. The one
            # quiet ending, "no speech", is the recording's answer about
            # itself; here the user asked, waited, and gets nothing to read,
            # and the sound is what says so before the sentence does.
            self._say_after_focus(i18n.t(WITHDRAWN_I18N_KEY), failed=True)
            return
        not_saved = NOT_SAVED_I18N_KEYS.get(answer)
        if not_saved:
            # Said in the window, among the notes, because it changes what the
            # user can expect: this text will not be there to reopen.
            notes = tuple(notes) + (narration.Note(not_saved),)
        if self._inline and answer == stored_transcription.SAVE_STORED:
            self._present_inline(result, announcement, notes)
            return
        # Not kept, so the row cannot carry it: the window is the one place
        # left where the text those minutes produced can be read.
        self._open_result_window(result, announcement, notes)

    def _withdrawn_now(self):
        """Whether the message has been deleted for everyone, asked of the
        record memory holds now.

        Not of `self._msg` alone: that is the dict the flow captured when the
        run started, and in the minutes since a sync may have swapped it for a
        new one (store_message_transcription() says the same) — a revoke
        applied to the new one leaves the old one still looking like audio.
        Found the way storing finds it, every copy by id or `_local_id`. The
        flow's own dict still counts too: a withdrawal never undoes itself, so
        either one saying so is enough.
        """
        try:
            copies = self._main_window._transcription_copies(self._jid, self._msg_id)
            current = stored_transcription.find_record(copies, self._msg_id)
        except Exception as exc:
            # Asked right after storing raised, and whatever broke that may
            # break this too; the text on screen outranks a second traceback.
            logging.warning("[transcription] looking the message up again raised: %s",
                            errors.exception_report(exc))
            current = None
        return (stored_transcription.is_withdrawn(current)
                or stored_transcription.is_withdrawn(self._msg))

    def _hidden_by_vault(self):
        """Whether the vault has the message's chat locked away right now.

        Asked every time, never remembered: the vault can close at any moment
        of a run. Of the chat the flow was started in and of the message's
        own `remoteJid` both, either one hidden being enough — the vault
        resolves @lid and phone forms itself, and a check that fails closed on
        an odd key costs one sentence, where one that fails open costs the
        note.
        """
        key = (self._msg.get("key") or {}) if isinstance(self._msg, dict) else {}
        hidden = self._main_window.is_chat_hidden_by_vault
        return hidden(self._jid) or hidden(key.get("remoteJid", ""))

    def _say_hidden_by_vault(self, kept):
        """The one sentence for a result the vault has taken off the screen.

        No focus move first, unlike `_say_after_focus()`: the vault closed
        the conversation and put the focus on the chat list, and that is
        where it stays. Still through `wx.CallAfter`, for the progress dialog
        that has just closed and whose focus change the screen reader is
        announcing. With the error sound when nothing was kept, like every run
        that ends without the text the user asked for.
        """
        if not kept:
            self._play_error_sound()
        key = HIDDEN_BY_VAULT_I18N_KEY if kept else HIDDEN_BY_VAULT_NOT_SAVED_I18N_KEY
        wx.CallAfter(self._main_window.output, self._i18n.t(key))

    def _store(self, result):
        """Keep `result` with the message; storing's SAVE_* answer.

        Dated after the decision the message already holds, whatever the
        clock says — `transcription_transcribe_again` after a clock was set back must
        still be the later decision (`stored_transcription.next_decision_time`).
        """
        previous = self._msg.get(stored_transcription.TRANSCRIPTION_KEY) if isinstance(self._msg, dict) else None
        value = stored_transcription.with_result(previous, stored_transcription.record_from_result(
            result, stored_transcription.next_decision_time(time.time(), previous),
            # A message that already shows its text in the line keeps showing
            # it when the window runs the transcription again.
            inline=self._inline or stored_transcription.is_inline(previous),
        ))
        return self._save(value)

    # Where the text is kept, who the result window names and whether it
    # offers "insert into the message": the four things that differ for a
    # status update (ui/status_transcription_flow.py overrides them).

    #: Whether the result window offers "Insert into the message".
    _offers_insert = True
    #: The question delete_saved() asks. It says what stays (the message and
    #: its audio), which is not what stays when the thing is a status.
    _delete_question_key = "transcription_delete_question"

    def _save(self, value, existing=False):
        """Storing's SAVE_* answer for `value`. `existing`: a text already
        kept that is only being marked, which the cache switches let through."""
        if existing:
            return self._main_window.store_message_transcription(
                self._jid, self._msg_id, value, existing=True)
        return self._main_window.store_message_transcription(self._jid, self._msg_id, value)

    def _delete(self, on_done):
        self._main_window.delete_message_transcription(self._jid, self._msg_id, on_done)

    def _title_name(self):
        return title_name(self._panel, self._msg)

    def show_saved(self):
        """Open the stored transcription — no model, no wait, same window."""
        value = stored_transcription.saved_transcription(self._msg, self._selection)
        if value is None:
            # Nothing stored after all (deleted from another copy between the
            # menu opening and the click): the ordinary path, which is what the
            # same key does on a message with nothing stored.
            self.start()
            return
        result = stored_transcription.as_result(value)
        notes = narration.result_notes(
            result,
            preferences.preferred_language(
                self._main_window.settings, getattr(self._i18n, "language", "")
            ),
        )
        # Only for the model's name in the headline: load_references() reads an
        # app.json that is held or unreadable as "none", and the headline then
        # leaves a custom model unnamed rather than the handler raising.
        references = external_models.load_references(
            getattr(self._main_window, "_app_settings", None)
        )
        self._open_result_window(
            result, saved_announcement(self._i18n, value, references), notes
        )

    def delete_saved(self):
        """Ask, delete the stored transcription, and say how it went."""
        if self._hidden_by_vault():
            # Unreachable from the conversation, like start(): a locked chat is
            # open only while the vault is. Kept so that no future entry point
            # can ask about, or confirm the existence of, a note it may not show.
            return
        i18n = self._i18n
        dlg = wx.MessageDialog(
            self._main_window,
            i18n.t(self._delete_question_key),
            i18n.t("transcription_progress_title"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        try:
            answer = dlg.ShowModal()
        finally:
            dlg.Destroy()
        # Back on the message now, whatever the answer: the deletion answers
        # from a background thread, and a focus move made when it does would
        # pull the user back from wherever they went in the meantime.
        self._focus_message()
        if answer != wx.ID_YES:
            return
        self._delete(self._after_delete)

    def _after_delete(self, ok):
        """On the wx thread, once the database has answered."""
        if ok:
            # The row carried the text in its line: redraw it without, so that
            # arrowing back does not read the deleted text. No focus move: the
            # focus went back to the message when the question closed.
            if not self._hidden_by_vault():
                self._repaint_row()
            self._main_window.output(self._i18n.t("transcription_deleted"))
            return
        self._play_error_sound()
        self._main_window.output(self._i18n.t("transcription_delete_failed"))

    def _open_result_window(self, result, announcement, notes):
        """The result window over `result` — fresh or stored, the same one."""
        if self._hidden_by_vault():
            # The one place the text reaches the screen, so the last word on
            # it. A fresh result has already been answered for in _report();
            # `transcription_view` gets here only from an open conversation, which
            # a locked chat is not while the vault is closed.
            return
        i18n = self._i18n
        _spoken, shown = split_result_notes(announcement, notes)
        name = self._title_name()
        title = (i18n.t("transcription_result_title").format(name=name) if name
                 else i18n.t("transcription_progress_title"))
        dialog = TranscriptionResultDialog(
            self._main_window,
            self._main_window,
            title,
            result.text,
            notes=[i18n.t(n.i18n_key).format(**n.values) for n in shown],
            spoken=result_speech(i18n, announcement, notes, window=True),
            default_file=default_file_name(i18n, name),
            # Named only when it differs from the window's default, so the
            # conversation's call stays exactly what it was.
            **({} if self._offers_insert else {"can_insert": False}),
        )
        try:
            dialog.run()
            insert = dialog.insert_requested
        finally:
            dialog.Destroy()
        if insert and self._hidden_by_vault():
            # Asked again after the window, not only before it: the user can
            # read in it for as long as they like, keys pressed in a dialog
            # never reach the vault's timer, and the timer closes the chat
            # meanwhile. The message field does not belong to the chat: it
            # outlives closing the conversation (only a send, an edit or an
            # attachment empties it), so the next conversation opened, whoever
            # it is with, would show the note's text ready for Enter to send
            # to them. Nothing is written, and the focus stays on the chat
            # list where the vault put it.
            #
            # Copy and Save in the window are left alone, on purpose: they put
            # the text where the user explicitly sends it, from a window that
            # is still showing it — like the message-text popup, which the
            # vault does not close either — and not into a place that belongs
            # to another conversation.
            #
            # Not _say_hidden_by_vault(): its sentences are for the end of a
            # run, and none ran here — this may be a stored transcription
            # opened again. A text that was not kept, the window's notes have
            # already said so; the one answer owed now is that the insertion
            # asked for did not happen, and the error sound says so first.
            # Like there, no focus move, and after the window has closed.
            self._play_error_sound()
            wx.CallAfter(self._main_window.output, self._i18n.t(INSERT_REFUSED_BY_VAULT_I18N_KEY))
            return
        if insert:
            self._insert_into_message_field(result.text)
        else:
            self._focus_message()

    def _present_inline(self, result, announcement, notes):
        """The text is in the message's line now: redraw it, say it, stay put."""
        if self._hidden_by_vault():
            return
        self._repaint_row()
        self._focus_message()
        spoken, _shown = split_result_notes(announcement, notes)
        # After the focus move, as every sentence here (see the module docstring).
        wx.CallAfter(self._main_window.output, inline_speech(self._i18n, result.text, spoken))

    def _repaint_row(self):
        """Redraw the message's row, which carries the stored text from now on.

        Only in the message's own conversation: an answer arriving after the
        user moved on would find no row there, and _repaint_or_repopulate()
        would rebuild the conversation they are reading instead.
        """
        conversation = getattr(self._panel, "conversation", None)
        if not isinstance(conversation, dict) or conversation.get("remoteJid", "") != self._jid:
            return
        repaint = getattr(self._panel, "_repaint_or_repopulate", None)
        if repaint is not None and self._msg_id:
            repaint([self._msg_id])

    def inline_saved(self):
        """Alt+Shift+N on a message that already has a transcription: no run.

        Put in the line (marking the stored one as wanted there) and read
        aloud, exactly as a fresh one would be; the model, the date and the
        caveats stay in the window (Alt+Shift+T), as before.
        """
        value = stored_transcription.saved_transcription(self._msg, self._selection)
        if value is None:
            self.start()
            return
        if not stored_transcription.is_inline(value):
            previous = self._msg.get(stored_transcription.TRANSCRIPTION_KEY)
            marked = stored_transcription.marked_inline(value, previous, time.time())
            # existing=True: a text already kept, only marked — the cache
            # switches are about keeping new ones.
            if self._save(marked, existing=True) != stored_transcription.SAVE_STORED:
                # Nowhere to keep the mark: open the window, where the text can
                # still be read.
                self._inline = False
                self.show_saved()
                return
        result = stored_transcription.as_result(value)
        notes = narration.result_notes(
            result,
            preferences.preferred_language(
                self._main_window.settings, getattr(self._i18n, "language", "")
            ),
        )
        spoken = tuple(n for n in notes if n.i18n_key == narration.VAD_UNAVAILABLE_I18N_KEY)
        self._repaint_row()
        self._focus_message()
        wx.CallAfter(self._main_window.output, inline_speech(self._i18n, result.text, spoken))

    def _offer_settings(self, sentence):
        """Say what is missing, and offer to open the tab that fixes it."""
        i18n = self._i18n
        dlg = wx.MessageDialog(
            self._main_window,
            f"{sentence}\n\n{i18n.t('transcription_open_settings_question')}",
            i18n.t("transcription_progress_title"),
            wx.YES_NO | wx.ICON_WARNING,
        )
        try:
            answer = dlg.ShowModal()
        finally:
            dlg.Destroy()
        if answer == wx.ID_YES:
            open_transcription_settings(self._main_window)
        self._focus_message()

    def _say_after_focus(self, sentence, failed=False):
        if failed:
            self._play_error_sound()
        if not self._background:
            # A run without the progress window took the focus from nowhere:
            # the user may be typing, or in another chat by now.
            self._focus_message()
        # After the focus move, not before: see the module docstring.
        wx.CallAfter(self._main_window.output, sentence)

    def _play_error_sound(self):
        # Guarded: a sound is never worth the outcome it goes with
        # (docs/traps/audio-devices.md). The output device can vanish or be
        # mid-switch, and Sound.play() then raises from BASS — here, before
        # the result window has opened with the text minutes of work
        # produced, or before the sentence that says what failed.
        sound = getattr(self._main_window, "error_sound", None)
        if sound is None:
            return
        try:
            sound.play()
        except Exception as exc:
            logging.warning("[transcription] could not play the error sound: %s",
                            errors.exception_report(exc))

    def _focus_message(self):
        """Back to the message the user came from — found by its id.

        Not by the row it was on: a transcription takes minutes, and messages
        arriving meanwhile re-sort `_sorted_messages`, so the old index is some
        other message by now. A message no longer in the list (deleted,
        revoked, scrolled out of the loaded window) leaves the focus on the
        list itself rather than on a neighbour it was never on.

        Nowhere at all when the vault closed the conversation meanwhile: it
        put the focus on the chat list, and pulling it back to a message list
        of that chat is the one thing the vault closed it to prevent.
        """
        if self._hidden_by_vault():
            return
        panel = self._panel
        index = panel._find_index_by_msg_id(self._msg_id)
        if index >= 0:
            panel._focus_message_row(index)
            return
        messages_list = panel.messages_list
        if messages_list.IsShown():
            messages_list.SetFocus()

    def _insert_into_message_field(self, text):
        """Put the text in at the cursor, keeping everything already typed.

        `WriteText()` rather than rebuilding the value: it lets Windows do the
        caret arithmetic, which is UTF-16 and breaks around emoji when done by
        hand (`insert_emoji()` learned that). It also *replaces* a selection,
        so a selection is first collapsed to its end — "without erasing what
        was typed" includes what was typed and happened to be selected.
        """
        field = self._panel.message_field
        start, end = field.GetSelection()
        if start != end:
            field.SetInsertionPoint(end)
        position = field.GetInsertionPoint()
        before = field.GetRange(position - 1, position) if position > 0 else ""
        after = field.GetRange(position, position + 1)
        piece = insertion_text(text, before, after)
        if piece:
            field.WriteText(piece)
        field.SetFocus()


def transcribe_message(panel, msg):
    """Run a transcription — `transcription_transcribe_again`, or a message with none."""
    MessageTranscriptionFlow(panel, msg).start()


def open_or_transcribe(panel, msg):
    """Entry point for Alt+Shift+T and the menu's first transcription item.

    The stored transcription when there is one — the key never starts minutes
    of work to produce what is already there — and a run otherwise.
    """
    flow = MessageTranscriptionFlow(panel, msg)
    if stored_transcription.saved_transcription(msg, flow._selection) is not None:
        flow.show_saved()
    else:
        flow.start()


def transcribe_inline(panel, msg):
    """Entry point for Alt+Shift+N (and Enter when it is set to transcribe).

    The text goes into the message's line of the list and is read aloud; no
    window opens. A transcription already stored is used instead of a run.
    """
    flow = MessageTranscriptionFlow(panel, msg, inline=True)
    if stored_transcription.saved_transcription(msg, flow._selection) is not None:
        flow.inline_saved()
    else:
        flow.start()


def delete_transcription(panel, msg):
    """Entry point for `transcription_delete`."""
    MessageTranscriptionFlow(panel, msg).delete_saved()
