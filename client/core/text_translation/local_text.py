"""What the translation settings say about the offline models. No wx, so
every sentence can be checked in every locale."""

from core.cuda_components.text import size_text
from core.text_translation import local_store
from core.transcription import errors, management

STATE_KEYS = {
    local_store.STATE_INSTALLED: "translation_local_model_installed",
    local_store.STATE_INCOMPLETE: "translation_local_model_incomplete",
    local_store.STATE_ABSENT: "translation_local_model_available",
}


def model_label(i18n, model, state):
    """One item of the model list: name, publisher, licence, languages, size
    and whether it is on this computer."""
    return i18n.t(STATE_KEYS.get(state, STATE_KEYS[local_store.STATE_ABSENT])).format(
        name=model.name, publisher=model.publisher, license=model.license,
        count=model.languages, size=size_text(i18n, model.download_bytes))


def download_confirmation(i18n, model):
    return i18n.t("translation_local_download_confirm").format(
        name=model.name, size=size_text(i18n, model.download_bytes), license=model.license)


def remove_confirmation(i18n, model):
    return i18n.t("translation_local_remove_confirm").format(name=model.name)


def outcome_text(i18n, action, model, result, error):
    """The sentence of a finished download or removal; failures use the
    transcription downloads' sentences (connection, disk space, cancelled)."""
    if getattr(error, "code", None) == errors.MODEL_DOWNLOAD_FAILED:
        # The stock sentence names "the transcription model".
        return i18n.t("translation_local_download_failed")
    ann = management.announcement(action, result, error, model_id=model.name)
    return i18n.t(ann.i18n_key).format(**dict(ann.values))
