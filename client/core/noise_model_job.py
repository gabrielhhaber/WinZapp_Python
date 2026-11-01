"""The background download of the noise reduction model.

Same worker as the transcription management actions (progress throttled before
it reaches the screen or the speech, cooperative cancel, one finished report),
so the progress dialog of the Transcription tab runs it unchanged. Only what
the worker does is different.
"""

from __future__ import annotations

from core import noise_model
from core.transcription import management


class NoiseModelDownloadJob(management.ManagementJob):
    def __init__(self, root=None, **kwargs):
        # The base class only needs an action it knows: it picks the error
        # code a failed run is reported with (MODEL_DOWNLOAD_FAILED).
        super().__init__(
            management.ACTION_DOWNLOAD_MODEL,
            model_id=noise_model.DPDFNET2_48K.id,
            **kwargs,
        )
        self._noise_root = root

    def _perform(self):
        self._check_cancel()
        return noise_model.download(
            self._noise_root,
            progress=self._report_progress,
            should_cancel=self._should_cancel,
            session=self._session,
        )
