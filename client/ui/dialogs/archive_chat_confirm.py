"""Confirmation shown before archiving a conversation.

Carries an unticked "don't show again" box (user_interface.confirm_archive_chat;
Settings > User Interface turns it back on). It is only honoured together with
the confirming button, which is also the Enter default.
"""

from ui.dialogs.checkbox_confirm import confirm_with_checkbox
from ui.dialogs.confirmation_prefs import (
    CONFIRM_ARCHIVE_CHAT, confirmation_enabled, disable_confirmation,
)


def confirm_archive_chat(parent, main_window, message: str, title: str,
                         dont_ask_label: str, *, yes_label: str, no_label: str) -> bool:
    """Return True when the chat should be archived."""
    if not confirmation_enabled(main_window, CONFIRM_ARCHIVE_CHAT):
        return True
    confirmed, dont_ask = confirm_with_checkbox(
        parent, message, title, dont_ask_label,
        yes_label=yes_label, no_label=no_label,
        checked=False, default_yes=True,
    )
    if confirmed and dont_ask:
        disable_confirmation(main_window, CONFIRM_ARCHIVE_CHAT)
    return confirmed
