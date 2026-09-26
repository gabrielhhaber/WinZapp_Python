"""Consent to capture microphone and computer audio, including all outputs
when NVDA process separation is active.

Like the stereo warning: No is default; don't-show only counts with Yes.
Settings > Interface can restore this warning.
"""
from ui.dialogs.checkbox_confirm import confirm_with_checkbox

SETTING_SECTION = "user_interface"
SETTING_KEY = "warn_system_audio_recording"
CONSENT_REVISION_KEY = "system_audio_consent_revision"
CONSENT_REVISION = 2


def system_audio_warning_enabled(settings) -> bool:
    ui = (settings or {}).get(SETTING_SECTION, {})
    revision = ui.get(CONSENT_REVISION_KEY, 0)
    # Old endpoint-only opt-outs do not authorize capture across all outputs.
    if type(revision) is not int or revision < CONSENT_REVISION:
        return True
    return bool(ui.get(SETTING_KEY, True))


def remember_system_audio_consent(settings, dont_ask_again):
    """Call only after Yes; preserve unrelated preferences and restore opt-in."""
    ui = settings.setdefault(SETTING_SECTION, {})
    ui[CONSENT_REVISION_KEY] = CONSENT_REVISION
    ui[SETTING_KEY] = not dont_ask_again


def ask_system_audio(parent, i18n):
    """Return (confirmed, dont_ask_again) using the standard confirmation."""
    t = i18n.t
    return confirm_with_checkbox(
        parent,
        t("system_audio_recording_warning"),
        t("system_audio_recording_title"),
        t("mark_all_read_dont_show_again"),
        yes_label=t("yes_button"),
        no_label=t("no_button"),
        checked=False,
        default_yes=False,
    )
