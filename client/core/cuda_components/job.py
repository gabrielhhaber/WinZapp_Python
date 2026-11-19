"""The background install of the components a device is missing, on the same
worker as the transcription's downloads (progress throttled, cooperative
cancel, one finished report), so the same progress dialog runs it."""

from core.cuda_components import store
from core.cuda_components.model import SHARED_CUBLAS_CU12
from core.transcription import errors, management

#: ComponentError code -> the transcription's error code with a sentence
_CODES = {
    "no_space": errors.NO_DISK_SPACE,
    "corrupted": errors.CUDA_RUNTIME_CORRUPTED,
    "busy": errors.CUDA_RUNTIME_BUSY,
    "in_use": errors.CUDA_RUNTIME_IN_USE,
}


def total_bytes(component_ids) -> int:
    """The whole wait, download and extraction, of `component_ids`."""
    from core.cuda_components.catalog import COMPONENTS
    from core.transcription import cuda_runtime
    return sum(cuda_runtime.INSTALL_BYTES if cid == SHARED_CUBLAS_CU12
               else COMPONENTS[cid].install_bytes for cid in component_ids)


class ComponentsInstallJob(management.ManagementJob):
    def __init__(self, component_ids, root=None, **kwargs):
        super().__init__(management.ACTION_INSTALL_CUDA_RUNTIME, **kwargs)
        self._component_ids = tuple(component_ids)
        self._root = root

    def _perform(self):
        from core.cuda_components.catalog import COMPONENTS
        from core.transcription import cuda_runtime
        total = total_bytes(self._component_ids) or 1
        done = 0
        for cid in self._component_ids:
            self._check_cancel()
            size = (cuda_runtime.INSTALL_BYTES if cid == SHARED_CUBLAS_CU12
                    else COMPONENTS[cid].install_bytes)

            def progress(current, _whole, base=done):
                self._report_progress(base + current, total)

            try:
                store.install(cid, progress=progress, should_cancel=self._should_cancel,
                              session=self._session, root=self._root)
            except store.ComponentError as exc:
                raise errors.TranscriptionError(
                    _CODES.get(exc.code, errors.CUDA_RUNTIME_DOWNLOAD_FAILED), str(exc)
                ) from exc
            done += size
        return tuple(self._component_ids)

    def announcement(self, result, error):
        """The noise reducer's own sentence on success; a failure is said with
        the transcription's CUDA sentences (connection, disk, in use...)."""
        if error is None:
            return management.Announcement("noise_reducer_components_done",
                                           management.OUTCOME_DONE)
        return management.announcement(management.ACTION_INSTALL_CUDA_RUNTIME, None, error)
