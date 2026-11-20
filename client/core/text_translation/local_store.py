"""Where the offline translation models live, and their download and removal.

Through core/transcription/model_store, like the noise reduction model
(core/noise_model.py): the pinned revision, the digest checked while the bytes
stream in, the resume, the install-wide lock and the free-space gate are that
module's, written once. The folder is install-wide (``global_dir()``), because
every account process translates with the same weights, and it is not the
Whisper folder: the Whisper picker lists every folder it does not know as
foreign.
"""

from __future__ import annotations

from app_paths import global_dir
from core.transcription import management, model_store

MODELS_DIRNAME = "translation_models"

STATE_ABSENT = model_store.STATE_ABSENT
STATE_INCOMPLETE = model_store.STATE_INCOMPLETE
STATE_INSTALLED = model_store.STATE_INSTALLED


def models_dir() -> str:
    return global_dir(MODELS_DIRNAME)


def model_path(model, root=None) -> str:
    return model_store.model_dir(root or models_dir(), model.id)


def state(model, root=None) -> str:
    """Absent, incomplete or installed: names and exact sizes, no hashing."""
    return model_store.installation_state(root or models_dir(), model).state


def is_installed(model, root=None) -> bool:
    return model is not None and state(model, root) == STATE_INSTALLED


def download(model, root=None, progress=None, should_cancel=None, session=None) -> str:
    return model_store.download_model(model, root or models_dir(), progress,
                                      should_cancel, session)


def remove(model, root=None) -> bool:
    """Delete exactly the catalogue's file names, then the empty folder. A
    model loaded in this process is dropped first, so Windows lets go of it."""
    from core.text_translation import local_engine

    local_engine.unload()
    return model_store.remove_model_files(root or models_dir(), model)


ACTION_DOWNLOAD = management.ACTION_DOWNLOAD_MODEL
ACTION_REMOVE = management.ACTION_REMOVE_MODEL


class TranslationModelJob(management.ManagementJob):
    """A download or removal on the transcription management worker, so the
    progress dialog of the Transcription tab (throttled bar, three spoken
    figures, cooperative Cancel) runs it unchanged."""

    def __init__(self, action, model, root=None, **kwargs):
        super().__init__(action, model_id=model.id, **kwargs)
        self._model = model
        self._root = root

    def _perform(self):
        self._check_cancel()
        if self.action == ACTION_REMOVE:
            # A bulk translation holds the model for minutes: Cancel must be
            # able to leave this wait.
            from core.text_translation import local_engine

            if not local_engine.unload(self._should_cancel):
                self._check_cancel()
            return remove(self._model, self._root)
        return download(self._model, self._root, progress=self._report_progress,
                        should_cancel=self._should_cancel, session=self._session)
