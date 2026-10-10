"""Confirmation shown before deleting one or more conversations.

Carries an unticked "don't show again" box (user_interface.confirm_delete_chat;
Settings > User Interface turns it back on). It is only honoured together with
Yes. Yes is the Enter default, as the wx.MessageBox this replaced did.
"""

from ui.dialogs.checkbox_confirm import confirm_with_checkbox
from ui.dialogs.confirmation_prefs import (
    CONFIRM_DELETE_CHAT, confirmation_enabled, disable_confirmation,
)


def confirm_delete_chat(parent, main_window, message: str, title: str,
                        dont_ask_label: str, *, yes_label: str, no_label: str) -> bool:
    """Return True when the chat(s) should be deleted."""
    if not confirmation_enabled(main_window, CONFIRM_DELETE_CHAT):
        return True
    confirmed, dont_ask = confirm_with_checkbox(
        parent, message, title, dont_ask_label,
        yes_label=yes_label, no_label=no_label,
        checked=False, default_yes=True,
    )
    if confirmed and dont_ask:
        disable_confirmation(main_window, CONFIRM_DELETE_CHAT)
    return confirmed
