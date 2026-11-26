"""What the call playback component's window says, and when its buttons are on.

Pure and free of wx, so every sentence can be checked in every locale.
"""

from core import call_neteq_component
from core.transcription import management, model_store

# Besides model_store's three states: a copy that came with the installation
# (the bridge's folder), and a version with no release pinned to download.
STATE_BUNDLED = "bundled"
STATE_UNAVAILABLE = "unavailable"

STATE_KEYS = {
    model_store.STATE_INSTALLED: "neteq_component_state_installed",
    model_store.STATE_INCOMPLETE: "neteq_component_state_incomplete",
    model_store.STATE_ABSENT: "neteq_component_state_absent",
    STATE_BUNDLED: "neteq_component_state_bundled",
    STATE_UNAVAILABLE: "neteq_component_state_unavailable",
}


def section_state(downloaded_state, pinned, bundled) -> str:
    """What the window shows: the download when it is whole, else the
    bundled copy calls use, else whether a download is possible at all."""
    if downloaded_state == model_store.STATE_INSTALLED:
        return model_store.STATE_INSTALLED
    if bundled:
        return STATE_BUNDLED
    if not pinned:
        return STATE_UNAVAILABLE
    return downloaded_state


def download_enabled(state, job_running=False) -> bool:
    """Offered only with something to fetch, a pinned release to fetch it
    from and no other download running."""
    return state in (model_store.STATE_ABSENT, model_store.STATE_INCOMPLETE) and not job_running


def remove_enabled(downloaded_state, job_running=False) -> bool:
    """Anything downloaded is on the disk (an interrupted part included)."""
    return downloaded_state != model_store.STATE_ABSENT and not job_running


def size_text(i18n, size_bytes) -> str:
    """Half a megabyte reads "0.5 MB", not "0 MB"."""
    sep = i18n.t("decimal_separator")
    return f"{size_bytes / 1024 ** 2:.1f} MB".replace(".", sep)


def state_text(i18n, state) -> str:
    text = i18n.t(STATE_KEYS.get(state, STATE_KEYS[STATE_UNAVAILABLE]))
    if state == model_store.STATE_ABSENT:
        return text.format(size=size_text(i18n, call_neteq_component.NETEQ.download_bytes))
    return text


def confirmation_text(i18n) -> str:
    return i18n.t("neteq_component_download_confirm").format(
        size=size_text(i18n, call_neteq_component.NETEQ.download_bytes)
    )


def outcome_text(i18n, result, error) -> str:
    """The sentence of a finished download. Failures use the sentences the
    transcription downloads already have (connection, disk space, cancelled)."""
    if error is None:
        return i18n.t("neteq_component_downloaded")
    ann = management.announcement(
        management.ACTION_DOWNLOAD_MODEL, result, error,
        model_id=call_neteq_component.NETEQ.id,
    )
    return i18n.t(ann.i18n_key).format(**dict(ann.values))


def removal_text(i18n, gone) -> str:
    return i18n.t("neteq_component_removed" if gone else "neteq_component_remove_in_use")
