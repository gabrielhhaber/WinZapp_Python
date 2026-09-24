"""The warning before a stereo voice message: iPhone cannot play one.

Shown in two places -- saving Settings with stereo newly on, and the second
record button when it records in stereo -- with the same "don't show again"
contract as the other confirmations (mark all as read, F5): No is the default
button, the box only counts together with Yes, and
user_interface.warn_stereo_voice_iphone (Settings > Interface) is both what it
clears and the way back.
"""

from ui.dialogs.checkbox_confirm import confirm_with_checkbox

SETTING_SECTION = "user_interface"
SETTING_KEY = "warn_stereo_voice_iphone"


def stereo_warning_enabled(settings) -> bool:
    return bool((settings or {}).get(SETTING_SECTION, {}).get(SETTING_KEY, True))


def ask_stereo_voice(parent, i18n):
    """Ask; returns (confirmed, dont_ask_again)."""
    t = i18n.t
    return confirm_with_checkbox(
        parent,
        t("stereo_voice_iphone_warning"),
        t("stereo_voice_warning_title"),
        t("mark_all_read_dont_show_again"),
        yes_label=t("yes_button"),
        no_label=t("no_button"),
        checked=False,
        default_yes=False,
    )
