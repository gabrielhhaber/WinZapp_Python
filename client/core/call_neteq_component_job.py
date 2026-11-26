"""The background download of the call playback component.

The noise reduction model's worker (core/noise_model_job.py) with another
store: progress throttled, cooperative cancel, one finished report, so the
transcription progress dialog and the main window's background run take it
unchanged.
"""

from __future__ import annotations

from core import call_neteq_component
from core.transcription import management


class NetEqComponentDownloadJob(management.ManagementJob):
    def __init__(self, root=None, **kwargs):
        # The base class only needs an action it knows: it picks the error
        # code a failed run is reported with (MODEL_DOWNLOAD_FAILED).
        super().__init__(
            management.ACTION_DOWNLOAD_MODEL,
            model_id=call_neteq_component.NETEQ.id,
            **kwargs,
        )
        self._component_root = root

    def _perform(self):
        self._check_cancel()
        return call_neteq_component.download(
            self._component_root,
            progress=self._report_progress,
            should_cancel=self._should_cancel,
            session=self._session,
        )

    def announcement(self, result, error):
        """The component's own sentence on success (also when the main window
        says it, for a download in the background)."""
        if error is None:
            return management.Announcement("neteq_component_downloaded", management.OUTCOME_DONE)
        return super().announcement(result, error)
