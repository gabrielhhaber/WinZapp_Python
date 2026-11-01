"""What the noise reduction model section says, and when its button is on.

Pure and free of wx, so every sentence can be checked in every locale.
"""

from core import noise_model
from core.transcription import management, model_store

STATE_KEYS = {
    model_store.STATE_INSTALLED: "noise_model_state_installed",
    model_store.STATE_INCOMPLETE: "noise_model_state_incomplete",
    model_store.STATE_ABSENT: "noise_model_state_absent",
}


def installation_state(root=None) -> str:
    return model_store.installation_state(
        root or noise_model.models_dir(), noise_model.DPDFNET2_48K
    ).state


def state_text(i18n, state) -> str:
    return i18n.t(STATE_KEYS.get(state, STATE_KEYS[model_store.STATE_ABSENT]))


def download_enabled(state, job_running=False) -> bool:
    """Offered while there is something to download and nothing else running."""
    return state != model_store.STATE_INSTALLED and not job_running


def size_text(i18n, size_bytes) -> str:
    sep = i18n.t("decimal_separator")
    return f"{size_bytes / 1024 ** 2:.0f} MB".replace(".", sep)


def confirmation_text(i18n) -> str:
    return i18n.t("noise_model_download_confirm").format(
        size=size_text(i18n, noise_model.DPDFNET2_48K.download_bytes)
    )


def outcome_text(i18n, result, error) -> str:
    """The sentence of a finished download. Failures use the sentences the
    transcription downloads already have (connection, disk space, cancelled)."""
    if error is None:
        return i18n.t("noise_model_downloaded")
    ann = management.announcement(
        management.ACTION_DOWNLOAD_MODEL, result, error,
        model_id=noise_model.DPDFNET2_48K.id,
    )
    return i18n.t(ann.i18n_key).format(**dict(ann.values))
