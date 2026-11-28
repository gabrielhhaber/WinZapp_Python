"""One phrase of a call through the local Whisper (ACC-04).

A phrase is PCM the engine just cut from the call's received audio, which is
exactly what a dictation is (``core/transcription/dictation_run.py``): there is
no media to find, the model/backend/device are decided against this machine
right now, and **nothing is downloaded**: a machine with no usable model stops
with the code a message run gives, and the window says that reason.

The audio exists on disk only as the WAV ``DictationTranscription`` writes into
a private ``winzapp-dictation-`` folder for ffmpeg, removed by the ``with`` that
owns it on every way out and swept at startup if the process was killed
(``temp_sweep``). The log says what job.py's says: codes and durations, never
the text.
"""

from __future__ import annotations

import threading

from core import dictation
from core.transcription import errors
# The failures that mean "the model is not set up", as auto_queue defines them.
from core.transcription.auto_queue import SETUP_FAILURES
from core.transcription.dictation_run import DictationTranscription


class PhraseTranscriber:
    """Transcribe phrases one after another; `cancel()` reaches the run in flight."""

    def __init__(self, settings, *, ui_language="", find_ffmpeg=None, arguments=None,
                 make_run=DictationTranscription):
        self._settings = settings
        self._ui_language = ui_language
        self._find_ffmpeg = find_ffmpeg
        self._arguments = dict(arguments or {})
        self._make_run = make_run
        self._lock = threading.Lock()
        self._current = None
        self._cancelled = False
        #: The last run's resolution, for the sentence of a setup failure.
        self.resolution = None

    def _run(self, chunks=()):
        return self._make_run(
            self._settings, chunks, 16000, 1,
            ui_language=self._ui_language, find_ffmpeg=self._find_ffmpeg,
            **self._arguments)

    def preflight(self) -> None:
        """Raise the TranscriptionError a phrase would stop at (no model, no
        program); nothing is loaded. For a worker thread (it probes hardware)."""
        run = self._run()
        try:
            run.preflight()
        finally:
            self.resolution = getattr(run, "resolution", None)

    def transcribe(self, pcm: bytes) -> str:
        """The phrase's text ('' for none); raises TranscriptionError."""
        outcome = {}

        def _done(result, error):
            outcome["result"], outcome["error"] = result, error

        with self._lock:
            if self._cancelled:
                raise errors.TranscriptionError(errors.CANCELLED, "captions stopped")
            run = self._run([pcm], on_finished=_done)
            self._current = run
        try:
            run.start()
            run.join()
        finally:
            with self._lock:
                self._current = None
            self.resolution = getattr(run, "resolution", None) or self.resolution
        if outcome.get("error") is not None:
            raise outcome["error"]
        result = outcome.get("result")
        if result is None or result.is_empty:
            return ""
        return dictation.normalize_transcript(result.text)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            run = self._current
        if run is not None:
            run.cancel()


def is_setup_failure(error) -> bool:
    return getattr(error, "code", None) in SETUP_FAILURES
