"""From "transcribe this message" to a finished job, behind one progress dialog.

`job.TranscriptionJob` starts from an audio file and a model id. A message has
neither: the model is a decision made against this machine *now*, the audio
may not have been downloaded yet, and what is on disk is encrypted. Every one
of those steps is slow somewhere — the hardware probe was measured at 0.67 s on
a machine with no graphics card, listing the models reads every file's size,
the download is a blocking HTTP call and an audio document may be 2 GB to
decrypt — so none of them may run on the wx thread. They run here, on one
worker, ahead of the job, and the progress dialog that is already on screen
covers all of them: a user who pressed Alt+Shift+T hears the dialog open at
once, and Cancel works from the first moment rather than from the moment the
job finally starts.

`MessageTranscription` has the shape the progress dialog already drives
(`start()`, `cancel()`, and `on_progress(tick)` / `on_finished(result, error)`
exactly once), so part 6b reuses `TranscriptionProgressDialog` as it stands.
Like job.py and management.py it imports no wx: every callback runs on a
worker thread and the UI side crosses with `wx.CallAfter`.

Four rules this file keeps:

* **Decide before starting, against a fresh measurement.** The probe is taken
  here, immediately before `preferences.resolve()`, and never borrowed from
  startup — free memory is what the model is chosen against, and Chromium has
  loaded WhatsApp Web since. The installed models are listed from the folder
  that is in force (install-wide, see `preferences.stored_models_dir()`). A
  run with no backend or no usable model stops *here*, before the media is
  downloaded or a byte is decrypted: those answers are the user's to act on in
  Settings, and a two-minute download first would be a wait for nothing.

* **Cancellation is honoured where it can be, and no further.** Between steps
  it is checked; *during* the media download (one synchronous HTTP call) and
  the decryption (one call into Fernet) it is not, and a cancel arriving there
  is noticed as soon as that call returns — the same honesty job.py writes
  down for loading the model. Once the job exists, `cancel()` is forwarded to
  it under a lock, so a cancel landing between "job built" and "job started"
  still reaches it.

* **The decrypted temporary is this file's, and it goes on every way out.** It
  is the audio of a private conversation in clear; success, failure,
  cancellation, an unexpected exception and a job that never started all
  leave through one `finally`. The converted audio a failed GPU run hands over
  (`prepared_handover`) is a different file with a different owner: this
  class only passes it on, and the caller that offers the processor re-run is
  the one who discards it.

* **The log says how, never what or whose.** The same rule job.py states:
  codes, model ids and durations, never the message, its id, the contact or a
  path. An unexpected exception is logged by its type and its frames — not by
  its text, which for anything touching a file quotes the path.

  That promise covers the lines *this module* writes, and no more. A run that
  has to download the audio first goes through the panel's
  `_download_media_to_disk()` and on into `MainWindow.handle_audio_message()`
  / `get_base64_from_media()`, which predate this part and log what the rest
  of the app's media downloads log — the message id, the cache path, the media
  URL. Those lines are there whenever a transcription triggers a download;
  bringing them under the same rule is the privacy audit's (part 8), not
  something this module can vouch for from here.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback

from core.transcription import (
    backend as backend_module,
    device,
    errors,
    external_ggml,
    external_models,
    job as job_module,
    management,
    message_audio,
    preferences,
    stored,
    vocabulary,
    whisper_cpp_builds,
    whisper_cpp_runtime,
)

# The one wait this layer adds in front of job.py's three. Announced before
# the download starts, exactly as the job announces its own phases — and not
# at all when the media is already on disk, which is the common case.
PHASE_DOWNLOADING_MEDIA = "downloading_media"

# What became of the media, for the one failure whose sentence depends on it.
# A message whose audio could not be fetched is MEDIA_NOT_DOWNLOADED to the
# rest of the pipeline, but "WhatsApp is disconnected, wait for it" and "the
# link may have expired" are different instructions — the same two the rest of
# the app already gives for a media download, and the UI reuses those.
MEDIA_PRESENT = "present"
MEDIA_OFFLINE = "offline"
MEDIA_FAILED = "failed"
# An own message not sent yet whose audio has not reached the disk: the
# recording is written by a worker thread after the encode (and the mix with
# the system audio adds an AAC encode to that wait), so a key pressed in the
# first seconds finds nothing. There is nothing to download either — the id
# is a local UUID WhatsApp has never seen — and "the link may have expired"
# would be false; the file is on its way.
MEDIA_PREPARING = "preparing"
# An own message whose send failed before its audio reached the disk: a mixed
# recording whose AAC encode or write failed deletes the half-written file and
# marks the row `_send_failed`, leaving the local UUID as its id. is_unsent()
# still says yes to it — so without this it was "still being prepared, try
# again in a moment", for ever. Nothing will ever write that file and nothing
# can be downloaded under a UUID WhatsApp never saw, so the sentence must not
# invite a retry. Its own status rather than MEDIA_FAILED, whose sentence
# ("the link may have expired") is about a download that never happened here.
MEDIA_SEND_FAILED = "send_failed"

# The fraction the job reports is turned into the throttle's integer scale.
# A thousand steps is finer than the gauge can show and coarse enough that the
# throttle's own arithmetic stays exact.
_PROGRESS_SCALE = 1000


class MessageTranscription:
    """One message's transcription, from the settings to the result.

    Started once and never reused, like the job it wraps. `on_phase(phase)`
    announces each wait before it begins (the job's own three plus
    PHASE_DOWNLOADING_MEDIA; terminal phases are not forwarded — the finished
    report is what speaks for those); `on_progress(tick)` receives
    `management.ProgressTick`s, the shape the progress dialog reads;
    `on_finished(result, error)` is called exactly once.

    After it has finished, `resolution`, `media_status`, `device` and
    `prepared_handover` say what the caller needs to choose its sentence.
    """

    def __init__(self, msg, settings, key, voice_dir, media_dir,
                 stored_models_dir="", ui_language="",
                 find_ffmpeg=None, is_online=None, fetch_media=None,
                 on_phase=None, on_progress=None, on_finished=None,
                 probe=None, list_installed=None, available_backends=None,
                 make_job=None, decrypt=None, clock=None, retry_of=None,
                 external_references=()):
        self._msg = msg
        self._settings = settings
        self._key = key
        self._voice_dir = voice_dir
        self._media_dir = media_dir
        self._stored_models_dir = stored_models_dir
        self._ui_language = ui_language or ""
        self._find_ffmpeg = find_ffmpeg or (lambda: "")
        self._is_online = is_online or (lambda: True)
        self._fetch_media = fetch_media
        self._on_phase = on_phase
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._probe = probe or device.probe_hardware
        # The models the user pointed WinZapp at in folders of their own, as
        # read when the run was made. Their *state* is not: a disk can be
        # unplugged between the keypress and the load, so it is looked at on
        # this run's worker (_decide) and again by the backend at load time.
        # None is "app.json could not be read" (external_models.
        # read_references()), which is not "none referenced": see _decide().
        self._external_references_known = external_references is not None
        self._external_references = tuple(external_references or ())
        self._list_installed = list_installed or self._usable_catalogue_ids
        self._available_backends = available_backends or backend_module.available_backend_ids
        self._make_job = make_job or job_module.TranscriptionJob
        self._decrypt = decrypt
        self._clock = clock or time.monotonic
        self._throttle = management.ProgressThrottle()
        self._retry_of = retry_of

        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="winzapp-transcription-message"
        )
        self._last_phase = None

        #: What `preferences.resolve()` answered — None until the settings
        #: have been read, which is the state a run cancelled at once is in.
        self.resolution = None
        #: The folder the models were listed from, so a CPU re-run reads the
        #: same one even if the setting changes in between.
        self.models_root = None
        self.ffmpeg = None
        #: MEDIA_PRESENT / MEDIA_OFFLINE / MEDIA_FAILED / MEDIA_PREPARING /
        #: MEDIA_SEND_FAILED once the media has been looked for; None before.
        self.media_status = None
        #: The TranscriptionJob, once there is one.
        self.job = None
        #: job.py's handover, passed on untouched. **Whoever reads it owns the
        #: file** — see TranscriptionJob.prepared_handover.
        self.prepared_handover = None

    @classmethod
    def retry_on_cpu(cls, previous, on_phase=None, on_progress=None, on_finished=None,
                     make_job=None, clock=None):
        """The same message again, on the processor, from the audio already converted.

        Nothing is re-read: the settings were resolved, the model listed and
        the audio converted by `previous`, and redoing any of it would be the
        whole wait a second time. The device preference is forced to the CPU
        because nothing else would put it there — job.py explains why a
        handed-over file alone goes straight back to the card.
        """
        return cls(
            previous._msg, previous._settings, previous._key,
            previous._voice_dir, previous._media_dir,
            on_phase=on_phase, on_progress=on_progress, on_finished=on_finished,
            make_job=make_job or previous._make_job, clock=clock,
            retry_of=previous,
            external_references=(previous._external_references
                                 if previous._external_references_known else None),
        )

    # ── Control ──────────────────────────────────────────────────────────────

    def start(self):
        self._thread.start()
        return self

    def cancel(self):
        """Ask the run to stop. Cooperative: see the module docstring."""
        with self._lock:
            self._cancelled.set()
            current = self.job
        if current is not None:
            current.cancel()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def join(self, timeout=None):
        self._thread.join(timeout)

    @property
    def model_id(self):
        return self.resolution.model_id if self.resolution is not None else None

    @property
    def model_name(self):
        """The model to name out loud: a catalogue id, or a custom model's
        folder name. Never the raw `external:<id>`; None when there is no
        model, or when it is a custom one whose reference was forgotten."""
        if self.resolution is None or self.resolution.model_id is None:
            return None
        return external_models.model_name(
            self.resolution.model_id, self._external_references
        )

    @property
    def backend_id(self):
        """The backend this run resolved to, or None before it has."""
        return self.resolution.backend_id if self.resolution is not None else None

    @property
    def external_references(self) -> tuple:
        """The references the run names a custom model's folder from."""
        return self._external_references

    @property
    def forced_language(self):
        """The language a single-language model runs this note in instead of
        the settings' (preferences.Resolution.language_forced), or None."""
        if self.resolution is None or not self.resolution.language_forced:
            return None
        return self.resolution.language

    @property
    def overridden_language(self):
        """The chosen language `forced_language` replaced, or None."""
        return self.resolution.language_overridden if self.resolution is not None else None

    @property
    def device(self):
        """The device the job chose — valid from its loading phase on, as in job.py."""
        return self.job.device if self.job is not None else None

    @property
    def device_reason(self):
        return self.job.device_reason if self.job is not None else None

    @property
    def precision(self):
        """The job's precision.PrecisionChoice — valid from its loading phase
        on, like `device`; None before."""
        return getattr(self.job, "precision", None) if self.job is not None else None

    # ── Worker ───────────────────────────────────────────────────────────────

    def _run(self):
        try:
            result = self._perform()
        except errors.TranscriptionError as exc:
            if self.job is None:
                # The job logs its own ending; a run that stopped before there
                # was one would otherwise leave no line at all.
                logging.info("[transcription] message run stopped before the job: %s",
                             exc.log_line)
            self._finish(None, exc)
        except Exception as exc:
            # The type and the frames, never the text — see the module
            # docstring. And no logging.exception(): its traceback prints the
            # message, and the message of anything that touched a file is the
            # file's path, whose name is the message id.
            logging.error(
                "[transcription] the message run failed unexpectedly: %s\n%s",
                type(exc).__name__,
                "".join(traceback.format_tb(exc.__traceback__)),
            )
            self._finish(None, errors.TranscriptionError(
                errors.BACKEND_ERROR, f"unexpected {type(exc).__name__}"
            ))
        else:
            self._finish(result, None)

    def _perform(self):
        self._check_cancel()
        previous = self._retry_of
        if previous is not None:
            self.resolution = previous.resolution
            self.models_root = previous.models_root
            self.ffmpeg = previous.ffmpeg
            self.media_status = previous.media_status
            return self._run_job(None, previous.prepared_handover, device.PREFERENCE_CPU)

        # The gauge moves from the first moment: the dialog is up and saying
        # "starting", and a bar that does nothing reads as hung.
        self._pulse()
        self._decide()
        # The probe and the folder listing are the slowest part of deciding,
        # and a cancel that arrived during them must be heard as a cancel —
        # not, offline, as "wait for the connection" from the download check
        # just below.
        self._check_cancel()

        media_path = message_audio.cached_media_path(
            self._msg, self._voice_dir, self._media_dir
        )
        if media_path is None:
            self.media_status = MEDIA_FAILED
            raise errors.TranscriptionError(errors.MEDIA_NOT_DOWNLOADED, "no id to look up")
        self._ensure_media(media_path)

        self._check_cancel()
        self._enter(job_module.PHASE_PREPARING_AUDIO)
        temp_path = message_audio.decrypt_to_temp(media_path, self._key, decrypt=self._decrypt)
        try:
            # A cancel that arrived while decrypting is honoured here, before
            # the job converts anything.
            self._check_cancel()
            return self._run_job(temp_path, None, self.resolution.device_preference)
        finally:
            message_audio.discard_temp(temp_path)

    def _usable_catalogue_ids(self, models_root):
        """The catalogue models a run could load now, of both backends: the
        folder's complete ones, and the ones a verified external folder or
        file holds (touching disks that may be unplugged, which is why this is
        on the worker). resolve() keeps the resolved backend's."""
        return external_models.usable_catalogue_ids(
            models_root, self._external_references
        ) + external_ggml.usable_ggml_ids(models_root, self._external_references)

    def _decide(self):
        """Resolve the settings against this machine as it is right now."""
        probe = self._probe()
        self.models_root = preferences.resolve_models_dir(self._stored_models_dir)
        installed = tuple(self._list_installed(self.models_root))
        resolution = preferences.resolve(
            self._settings, probe, installed,
            ui_language=self._ui_language,
            available_backends=self._available_backends(),
            # Not known is "not measured" to resolve(): a custom choice stays
            # what it is rather than being reported as retired.
            custom_model_ids=(
                external_models.custom_reference_backends(self._external_references)
                if self._external_references_known else None
            ),
            whisper_cpp_cuda_installed=_whisper_cpp_cuda_installed(),
        )
        self.resolution = resolution
        # Catalogue names and codes only, never the message's. Without this
        # line a run that stops at "no model is ready" left nothing in log.log
        # but the hardware probe.
        engine, catalogue_model = resolution.backend_id, resolution.model_id
        logging.info(
            "[transcription] resolved backend=%s model=%s device=%s none_reason=%s "
            "ready_models=%s references_known=%s",
            engine, catalogue_model, resolution.device_preference,
            resolution.model_none_reason, ",".join(installed) or "none",
            self._external_references_known,
        )
        if resolution.substitutions:
            # Setting names and the stored values that were replaced — ids and
            # codes, which `Substitution.stored` exists to carry to the log.
            logging.info(
                "[transcription] stored settings replaced for this run: %s",
                ", ".join(f"{s.setting}={s.stored}" for s in resolution.substitutions),
            )
        if resolution.backend_id is None:
            raise errors.TranscriptionError(errors.BACKEND_MISSING, "no usable backend")
        if resolution.model_id is None or resolution.model_id not in installed:
            self._check_references_were_read()
        if resolution.model_id is None:
            raise errors.TranscriptionError(
                errors.MODEL_NOT_INSTALLED, f"no model resolved: {resolution.model_none_reason}"
            )
        if resolution.model_id not in installed:
            # Checked here, not left to the job: the job would first convert
            # the whole recording and only then find no model to load it into.
            # model_directory() is what the backend will ask at load time, so
            # it answers with the code that is true: the folder of a model the
            # user pointed WinZapp at is gone (EXTERNAL_MODEL_MISSING) or was
            # changed (EXTERNAL_MODEL_CHANGED), the model is not downloaded
            # (MODEL_NOT_INSTALLED), WinZapp's own copy is incomplete
            # (MODEL_CORRUPTED — before part 10b this said "not installed";
            # Repair on the tab is what fixes it, and the tab is offered for
            # both) — or, for a custom model, which is never in `installed`,
            # that it is there and fine, and the run goes on.
            if resolution.backend_id == backend_module.BACKEND_WHISPER_CPP:
                external_ggml.model_file(
                    self.models_root, resolution.model_id, self._external_references
                )
            else:
                external_models.model_directory(
                    self.models_root, resolution.model_id, self._external_references
                )
        self.ffmpeg = self._find_ffmpeg()

    def _check_references_were_read(self):
        """Stop a run that would otherwise blame the wrong thing.

        With app.json unreadable the list of external models is unknown, and
        a model that is not in WinZapp's own folder may well be in one of
        them: "not installed" or "the folder is gone" would both be guesses.
        The file being held by another WinZapp window is what makes it
        unreadable, and that has a sentence of its own.
        """
        if not self._external_references_known:
            raise errors.TranscriptionError(
                errors.MODELS_BUSY, "app.json: the external models could not be read"
            )

    def _ensure_media(self, media_path):
        """Download the media when it is not on disk yet. Reports nothing.

        Saying what went wrong is the caller's, once, after the dialog has
        closed — this only records which of the two sentences applies. The
        panel's own `_ensure_media_on_disk()` could not be used as it stands
        for exactly that reason: it tells the user itself, with a message box
        that would appear on top of the modal progress dialog, and the
        transcription would then have to stay silent about its own failure.
        """
        if os.path.isfile(media_path):
            self.media_status = MEDIA_PRESENT
            return
        # Before the connection check: the file is written locally whether or
        # not WhatsApp is connected, so "wait for the connection" would be as
        # wrong here as a download. The same test that refuses to *store* a
        # transcription of an unsent message — running one is fine once the
        # file is there, which the branch above already let through.
        if stored.is_unsent(self._msg):
            # A failed send is unsent too, and its file is not coming — see
            # MEDIA_SEND_FAILED.
            if isinstance(self._msg, dict) and self._msg.get("_send_failed"):
                self.media_status = MEDIA_SEND_FAILED
                raise errors.TranscriptionError(
                    errors.MEDIA_NOT_DOWNLOADED, "media fetch: send failed, no file"
                )
            self.media_status = MEDIA_PREPARING
            raise errors.TranscriptionError(
                errors.MEDIA_NOT_DOWNLOADED, "media fetch: unsent, not written yet"
            )
        if not self._is_online():
            self.media_status = MEDIA_OFFLINE
            raise errors.TranscriptionError(errors.MEDIA_NOT_DOWNLOADED, "media fetch: offline")
        self._check_cancel()
        self._enter(PHASE_DOWNLOADING_MEDIA)
        fetched = False
        if self._fetch_media is not None:
            try:
                fetched = bool(self._fetch_media(self._msg, media_path))
            except Exception as exc:
                logging.info("[transcription] media download raised %s", type(exc).__name__)
        # The download is one blocking call; a cancel that came in during it
        # is honoured now, whatever it produced.
        self._check_cancel()
        if fetched and os.path.isfile(media_path):
            self.media_status = MEDIA_PRESENT
            return
        self.media_status = MEDIA_FAILED
        raise errors.TranscriptionError(errors.MEDIA_NOT_DOWNLOADED, "media fetch: failed")

    def _run_job(self, audio_path, prepared, device_preference):
        """Run the TranscriptionJob to its end on its own thread, and wait for it."""
        resolution = self.resolution
        outcome = {}

        def _finished(result, error):
            outcome["result"] = result
            outcome["error"] = error

        # Only named when there is one, so a job built without a vocabulary is
        # called exactly as it always was. Never logged (vocabulary.py).
        prompt = vocabulary.prompt_from_settings(self._settings)
        created = self._make_job(
            audio_path, self.ffmpeg, self.models_root, resolution.model_id,
            **({"initial_prompt": prompt} if prompt else {}),
            language=resolution.language,
            device_preference=device_preference,
            backend_id=resolution.backend_id,
            prepared=prepared,
            on_phase=self._forward_phase,
            on_progress=self._forward_progress,
            # Always passed, and not only for the report: a job without one
            # never hands the converted audio over, so the offer to redo the
            # run on the processor would have nothing to redo it from.
            on_finished=_finished,
            external_references=self._external_references,
            # The stored choice; the job resolves it against the device it
            # lands on, so the processor re-run gets one the processor runs.
            compute_type_preference=resolution.compute_type_preference,
        )
        with self._lock:
            self.job = created
            cancelled_already = self._cancelled.is_set()
        if cancelled_already:
            # Before start(), so the job's own first check sees it and the run
            # neither converts nor loads anything.
            created.cancel()
        created.start()
        created.join()
        self.prepared_handover = created.prepared_handover
        if "error" not in outcome:
            # job.py promises one report; a job that broke that promise must
            # not be read as a success with no result.
            raise errors.TranscriptionError(errors.BACKEND_ERROR, "the job ended without a report")
        if outcome["error"] is not None:
            raise outcome["error"]
        return outcome["result"]

    def _finish(self, result, error):
        self._call(self._on_finished, result, error)

    # ── Plumbing ─────────────────────────────────────────────────────────────

    def _forward_phase(self, phase):
        """The job's phases, minus the ones that are not news.

        Terminal phases are the finished report's to speak. PREPARING_AUDIO
        is dropped when it repeats: this run announced it already for the
        decryption, and the job announcing it again for the conversion would
        be the same sentence twice in a row for what the user hears as one
        wait.
        """
        if phase in job_module.TERMINAL_PHASES or phase == self._last_phase:
            return
        self._enter(phase)

    def _enter(self, phase):
        self._last_phase = phase
        if phase != job_module.PHASE_TRANSCRIBING:
            # Only the decoding reports a fraction; the other waits get a
            # moving bar, which is still "something is happening".
            self._pulse()
        self._call(self._on_phase, phase)

    def _pulse(self):
        self._call(self._on_progress, management.ProgressTick(0, None, None, True, False))

    def _forward_progress(self, fraction):
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            return
        if fraction != fraction:
            # NaN, which compares unequal to itself — and which the clamp
            # below would quietly turn into 0%, moving the bar backwards.
            return
        fraction = min(1.0, max(0.0, fraction))
        tick = self._throttle.update(
            int(fraction * _PROGRESS_SCALE), _PROGRESS_SCALE, self._clock()
        )
        if tick.update_bar or tick.speak:
            self._call(self._on_progress, tick)

    def _call(self, callback, *args):
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            # Same guard as job.py: a callback that raises must not cost the
            # run its finished report. Type only, as above.
            logging.error("[transcription] a message run callback raised %s",
                          type(exc).__name__)

    def _check_cancel(self):
        if self._cancelled.is_set():
            raise errors.TranscriptionError(errors.CANCELLED, "cancelled by the user")


def _whisper_cpp_cuda_installed() -> bool:
    """Whether whisper.cpp's graphics-card build is installed — a manifest read
    and a stat per file, on this run's worker. Never raises."""
    try:
        # Wherever it can be launched from: WinZapp's own folder or the one
        # the user asked to reuse.
        return whisper_cpp_runtime.usable_root(whisper_cpp_builds.BUILD_CUDA) is not None
    except Exception:
        return False
