"""A dictation's transcription: what was just said, with no message behind it.

``MessageTranscription`` already decides the backend, model and device against
this machine right now, runs the job and classifies the failures; what differs
for a dictation is only where the audio comes from. It is PCM the microphone
just captured, so there is no media to find, download or decrypt, and the
decision is taken **without downloading anything**: a machine with no usable
model (or no program) stops in ``_decide()`` with the same codes a message run
gives, and the caller says that reason. The user's vocabulary (``initial_
prompt``) and every Settings > Local transcription choice apply unchanged,
because ``_run_job`` reads them from the same settings.

The audio exists on disk only as a WAV, written here into a private temporary
folder for ffmpeg to convert and removed by the ``with`` that owns it on every
way out (success, failure, cancellation). It is named ``TEMP_PREFIX`` so a
folder a killed run left is swept at the next start (``temp_sweep``).
The log says what job.py's says: codes and durations, never the text.
"""

from __future__ import annotations

import os

from core import dictation
from core.transcription import _fileops, job as job_module, message_run

TEMP_PREFIX = "winzapp-dictation-"


class DictationTranscription(message_run.MessageTranscription):
    """One dictation, from the captured PCM to a ``TranscriptionResult``.

    ``chunks`` may be empty for ``preflight()``-only use. The chunks are
    dropped from memory as soon as they are on the WAV.
    """

    def __init__(self, settings, chunks=(), rate=16000, channels=1, **kwargs):
        super().__init__(None, settings, None, "", "", **kwargs)
        self._chunks = list(chunks)
        self._rate = rate
        self._channels = channels

    def preflight(self) -> None:
        """Raise the TranscriptionError a run would stop at for want of a
        model, a program or a readable settings file; nothing is loaded or
        downloaded. For a worker thread (it probes the hardware)."""
        self._decide()

    def _perform(self):
        self._check_cancel()
        self._pulse()
        self._decide()
        self._check_cancel()
        with _fileops.private_temp_dir(TEMP_PREFIX) as folder:
            path = os.path.join(folder, "dictation.wav")
            dictation.write_wav(path, self._chunks, self._rate, self._channels)
            self._chunks = []
            self._check_cancel()
            self._enter(job_module.PHASE_PREPARING_AUDIO)
            return self._run_job(path, None, self.resolution.device_preference)
