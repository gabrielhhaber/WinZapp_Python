"""One transcription, off the UI thread, in phases the UI can announce.

A transcription is three waits stacked on each other — converting the audio,
loading the model (tens of seconds for large-v3), decoding it — and a blind
user staring at a window that says nothing has no way to tell any of them from
a crash. So the job announces which wait it is in, before entering it, and the
phases are symbolic codes: what they are *called* is part 6's business, in the
user's own language, exactly as with the error codes and the device reasons.

Rules this file exists to keep:

* **Nothing here imports wx.** Every callback is invoked on the worker thread,
  and part 6 wraps each one in `wx.CallAfter` — that is the same split
  core/message_queue.py makes, and it is what lets the whole pipeline be tested
  without a wx.App.

* **Exactly one finished report, whatever happens.** The UI is holding a row
  and a spoken "transcribing…" until it hears back; a path that ends without
  reporting leaves that state forever. So the callbacks are called through a
  guard that logs and swallows, and even a programming error on this thread
  becomes a BACKEND_ERROR the user is told about.

* **Cancellation is honoured in two phases and a half.** Converting is
  interruptible (the ffmpeg process is killed, not abandoned) and so is
  decoding (the backend stops consuming the segment generator). *Loading is
  not*: `WhisperModel(...)` is one blocking call into C++ with nothing to hang
  a check on, so a cancel arriving there is noticed only once the load returns
  — which for large-v3 can be tens of seconds later. Saying "checked in every
  phase" would be the comfortable version and it would be false. Whatever the
  phase, the converted temporary file is deleted before this thread ends —
  with exactly one exception, `prepared_handover`, where it is handed to the
  caller instead of deleted and the caller becomes the one who deletes it.

* **Progress is reported only while decoding.** Converting and loading have no
  fraction to give — ffmpeg's own progress is not parsed and CTranslate2's load
  reports nothing — and loading is the *longest* of the three on a large model.
  So part 6 needs an indeterminate indicator for those two phases; the phase
  callback is what tells it to put one up.

* **The hardware is probed here, immediately before the device is chosen.**
  device.py's docstring is explicit that a probe taken at startup describes a
  machine that no longer exists by the time a model is loaded: free VRAM is the
  figure being planned against, and Chromium has since loaded WhatsApp Web.

**What the log may say about a transcription.** Backend, model, device, compute
type, the language asked for and the one detected, how long the audio was and
how long the run took, and the technical text of a failure. Never the
transcribed text or any part of it, never the contact, the number or the
message id — and therefore never the audio path either, since the media file is
named after the message id. `tests/test_transcription_backend.py` walks the
logging calls of this package and fails if the text could reach one.
"""

from __future__ import annotations

import logging
import threading
import time

from core.transcription import audio_prep, backend as backend_module, device, errors, precision
from core.transcription import machine_slot, residency as residency_module

# Announced before the wait each one names, so the UI is never silent during
# one. No i18n keys here on purpose: part 6 owns the wording, and these are the
# codes it maps — the same arrangement as errors.ERROR_CODES.
PHASE_PREPARING_AUDIO = "preparing_audio"
#: Only when another run (this account's or another's) holds the machine slot:
#: without it the wait for that run is silence (machine_slot).
PHASE_WAITING_FOR_SLOT = "waiting_for_slot"
PHASE_LOADING_MODEL = "loading_model"
PHASE_TRANSCRIBING = "transcribing"
PHASE_DONE = "done"
PHASE_CANCELLED = "cancelled"
PHASE_FAILED = "failed"

PHASES = (
    PHASE_PREPARING_AUDIO,
    PHASE_WAITING_FOR_SLOT,
    PHASE_LOADING_MODEL,
    PHASE_TRANSCRIBING,
    PHASE_DONE,
    PHASE_CANCELLED,
    PHASE_FAILED,
)

TERMINAL_PHASES = (PHASE_DONE, PHASE_CANCELLED, PHASE_FAILED)


class TranscriptionJob:
    """A single transcription running on its own thread.

    One job per transcription, started once and never reused: the phases are a
    sequence, and a job that could be restarted would have to answer what its
    phase means while it is being restarted.
    """

    def __init__(self, audio_path, ffmpeg, models_root, model_id,
                 language=None, device_preference=device.PREFERENCE_AUTO,
                 backend=None, backend_id=None, prepared=None,
                 on_phase=None, on_progress=None, on_finished=None,
                 probe=None, external_references=(),
                 compute_type_preference=precision.AUTO):
        self._audio_path = audio_path
        self._ffmpeg = ffmpeg
        self._models_root = models_root
        self._model_id = model_id
        # Handed to the backend as they are: where a model id is loaded from
        # (WinZapp's own folder, or one the user pointed it at) is decided by
        # external_models.model_directory() at load time, since a disk can be
        # unplugged between the decision and the load.
        self._external_references = tuple(external_references)
        self._language = language or None
        self._device_preference = device_preference
        # The precision the user chose (part 11), or "auto". Resolved against
        # the device in _decode(), like the device itself: a CPU re-run of a
        # card's choice resolves it again, to one the processor can run.
        self._compute_type_preference = compute_type_preference
        # `backend` is what the tests (and part 6, which keeps one warm) hand
        # in; `backend_id` is the settings value resolved when the run starts,
        # so a backend that stopped being usable is noticed then and not at
        # construction time.
        self._backend = backend
        self._backend_id = backend_id
        # An already-converted PreparedAudio, when the caller has one: part 4's
        # "that failed on the GPU, try it on the CPU" would otherwise run
        # ffmpeg over the same file a second time, which on a 40-minute
        # recording is the whole wait again. A file handed in is **not** ours,
        # so it is neither re-created nor deleted — its owner disposes of it.
        #
        # Handing one in does *not* imply the CPU: the run re-probes and
        # re-decides like any other, and after INSUFFICIENT_VRAM the card is
        # still counted and its libraries still load, so a re-run built without
        # `device_preference=device.PREFERENCE_CPU` goes straight back to CUDA
        # and fails identically — having paid for the model load again, and
        # having told the user it was trying the processor. Whoever builds the
        # re-run passes that preference. (Part 6: a forced CPU run resolves to
        # REASON_CPU_REQUESTED, "you asked for the processor", which this user
        # did not; that announcement is the one to suppress on this path.)
        self._prepared = prepared
        self._on_phase = on_phase
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._probe = probe or device.probe_hardware

        self._cancelled = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="winzapp-transcription"
        )

        #: Readable from the callbacks: the device decision is made during the
        #: loading phase, and part 6 announces it (device.device_reason_i18n_key)
        #: alongside that phase rather than waiting for the result.
        self.phase = None
        self.device = None
        self.device_reason = None
        self.compute_type = None
        #: The precision.PrecisionChoice behind `compute_type`: what the user
        #: chose and whether the device could run it, which part 11 announces
        #: with the device.
        self.precision = None

        #: The converted audio, handed to the caller instead of being deleted,
        #: when the run failed on the GPU in a way a CPU re-run could cure.
        #: Set before the finished callback is called, so part 6 can read it
        #: from there — and **whoever reads it owns the file**: pass it to the
        #: CPU job as `prepared=` (with `device_preference=PREFERENCE_CPU`) and
        #: call `audio_prep.discard()` on it once that job is done or the offer
        #: is declined. It stays None on every other path, and those paths
        #: delete the file themselves as before.
        #:
        #: A handover needs somebody to hand it to, so a job built without an
        #: `on_finished` never makes one: nothing would ever learn the file
        #: exists, and a WAV of the whole recording would sit in %TEMP% with no
        #: owner for good.
        self.prepared_handover = None

    # ── Control ──────────────────────────────────────────────────────────────

    def start(self):
        self._thread.start()
        return self

    def cancel(self):
        """Ask the run to stop. Cooperative: it stops at the next check."""
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def join(self, timeout=None):
        self._thread.join(timeout)

    # ── Worker ───────────────────────────────────────────────────────────────

    def _run(self):
        started = time.monotonic()
        try:
            result = self._transcribe()
        except errors.TranscriptionError as exc:
            self._finish(None, exc, started)
        except Exception as exc:
            # A programming error on a worker thread has nothing above it to
            # catch it, and the UI would sit waiting for a report that never
            # comes — the same reason MessageQueue guards its own worker.
            # Not logging.exception(): its lines are the exception's text
            # verbatim, and the text of anything that touched the audio can
            # name the media file — the message id. The same report, chain and
            # all, with that name taken out (errors.exception_report()).
            logging.error("[transcription] the job failed unexpectedly: %s",
                          errors.exception_report(exc))
            self._finish(
                None,
                # The text is kept for the diagnosis; the constructor takes
                # any media file name out of it (TranscriptionError.__init__).
                errors.TranscriptionError(
                    errors.BACKEND_ERROR, f"{type(exc).__name__}: {exc}"
                ),
                started,
            )
        else:
            self._finish(result, None, started)

    def _transcribe(self):
        self._check_cancel()
        if self._prepared is not None:
            # Nothing to convert, so nothing to announce converting — and
            # nothing to delete either, since the file belongs to the caller.
            # This is the one path whose first phase is `loading_model`, which
            # part 6 has to accept: it is part 4's "that failed on the GPU, run
            # it on the CPU", where the audio was converted by the run before.
            return self._decode(self._resolve_backend(), self._prepared)

        # On the converting path the first phase is announced before the
        # backend is resolved: part 6 opens its progress window on the first
        # phase it hears, and a job that falls at the first hurdle ("no usable
        # backend") would otherwise report `failed` with no phase before it.
        self._enter(PHASE_PREPARING_AUDIO)
        backend = self._resolve_backend()
        prepared = audio_prep.prepare_audio(
            self._ffmpeg, self._audio_path, should_cancel=self._should_cancel
        )
        # Not a context manager that always deletes: whether this file may
        # outlive the run depends on the error and on the device it happened
        # on, and neither is anything such a wrapper can see.
        try:
            return self._decode(backend, prepared)
        except errors.TranscriptionError as exc:
            if self._on_finished is not None and device.should_retry_on_cpu(
                exc, self.device
            ):
                # Converting a 40-minute recording again would be the entire
                # wait a second time, for a file that is already correct. From
                # here the file is the caller's, and the `finally` below leaves
                # it alone.
                self.prepared_handover = prepared
                logging.info(
                    "[transcription] keeping the converted audio for a "
                    "possible re-run on the processor"
                )
            raise
        finally:
            if self.prepared_handover is None:
                audio_prep.discard(prepared)

    def _resolve_backend(self):
        return self._backend or backend_module.resolve_backend(self._backend_id)

    def _decode(self, backend, prepared):
        """From a prepared file to a result: decide, load, transcribe — one
        model in memory at a time on the whole machine (machine_slot): a
        second run, here or in another account's process, waits for this one
        instead of loading another copy."""
        with machine_slot.slot.held(
            self._check_cancel,
            on_wait=lambda: self._enter(PHASE_WAITING_FOR_SLOT),
        ):
            return self._decode_held(backend, prepared)

    def _decode_held(self, backend, prepared):
        # Probed here and used immediately: a probe is a statement about free
        # memory at one instant, and this is the instant the decision is about.
        probe = self._probe()
        # Through the backend, which picks device.py's rule for itself: the
        # whisper.cpp one resolves a card its graphics-card build cannot run on
        # to the processor here, up front, rather than failing on every run.
        self.device, self.device_reason = backend.resolve_device(
            self._device_preference, probe
        )
        # Through the backend too: faster-whisper honours the user's
        # precision, whisper.cpp's is the model file.
        self.precision = backend.resolve_compute_type(
            self._compute_type_preference, self.device, probe
        )
        self.compute_type = self.precision.compute_type
        if self.precision.replaced:
            logging.info(
                "[transcription] precision %s cannot run on %s here — using %s",
                self.precision.requested, self.device, self.compute_type,
            )

        request = backend_module.TranscriptionRequest(
            audio_path=prepared.path,
            models_root=self._models_root,
            model_id=self._model_id,
            device=self.device,
            compute_type=self.compute_type,
            language=self._language,
            duration_seconds=prepared.duration_seconds,
            external_references=self._external_references,
            compute_capability=probe.compute_capability,
        )

        # Announced only now, and deliberately not before the probe: part 6
        # reads `job.device` / `job.device_reason` from inside this very
        # callback to say which processor is about to be used. Announced any
        # earlier they are still None, and device_reason_i18n_key(None) falls
        # back to "you asked for the CPU" — so a user on a machine with a GPU
        # would hear the app confidently name the wrong reason while the model
        # loads onto CUDA. Checked before announcing, too: a cancellation
        # during the conversion must not produce "loading the model" and then,
        # immediately, "cancelled".
        self._check_cancel()
        self._enter(PHASE_LOADING_MODEL)
        # The model is let go after a while with nobody using it, unless the
        # user asked to keep it loaded (core/transcription/residency.py): the
        # clock starts when this block ends, whatever way it ends.
        with residency_module.residency.using(backend):
            backend.load_model(request, should_cancel=self._should_cancel)

            self._check_cancel()
            self._enter(PHASE_TRANSCRIBING)
            return backend.transcribe(
                request,
                progress=self._report_progress,
                should_cancel=self._should_cancel,
            )

    def _finish(self, result, error, started):
        elapsed = time.monotonic() - started
        if error is None:
            phase = PHASE_DONE
        elif error.code == errors.CANCELLED:
            phase = PHASE_CANCELLED
        else:
            phase = PHASE_FAILED

        # Everything the log is allowed to keep about a transcription, and
        # nothing else — see the module docstring. `error.log_line` is the
        # technical text; the result's own text is never touched here.
        logging.info(
            "[transcription] %s backend=%s model=%s device=%s compute=%s "
            "audio=%.1fs asked=%s detected=%s took=%.1fs%s",
            phase,
            getattr(result, "backend", None) or self._backend_id or "auto",
            self._model_id,
            self.device,
            self.compute_type,
            getattr(result, "duration_seconds", None) or 0.0,
            self._language or "auto",
            getattr(result, "language", None),
            elapsed,
            f" — {error.log_line}" if error is not None else "",
        )

        self._enter(phase)
        if not self._call(self._on_finished, result, error):
            # The handover is the caller's to discard, and a caller whose
            # finished-callback raised never took it: the WAV of the whole
            # recording would stay in %TEMP% for good.
            audio_prep.discard(self.prepared_handover)
            self.prepared_handover = None

    # ── Plumbing ─────────────────────────────────────────────────────────────

    def _enter(self, phase):
        self.phase = phase
        self._call(self._on_phase, phase)

    def _report_progress(self, fraction):
        self._call(self._on_progress, fraction)

    def _call(self, callback, *args):
        """Run a callback; False if it raised."""
        if callback is None:
            return True
        try:
            callback(*args)
            return True
        except Exception as exc:
            # A callback that raises must not cost the run its remaining
            # phases, and above all must not cost it the finished report the
            # UI is waiting on.
            logging.error("[transcription] a job callback raised: %s",
                          errors.exception_report(exc))
            return False

    def _should_cancel(self) -> bool:
        return self._cancelled.is_set()

    def _check_cancel(self):
        if self._cancelled.is_set():
            raise errors.TranscriptionError(errors.CANCELLED, "cancelled by the user")
