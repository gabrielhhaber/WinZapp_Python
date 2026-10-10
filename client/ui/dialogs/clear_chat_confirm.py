"""Confirmation shown before clearing one or more conversations.

WhatsApp Web asks "clear this chat?" together with a "keep starred messages"
checkbox, ticked by default. WinZapp used to ask a bare yes/no and always kept
the starred ones; the choice now belongs to the user, same as on WhatsApp Web.

A second, unticked "don't show again" box turns the question off
(user_interface.confirm_clear_chat); once off, chats are cleared straight away
keeping the starred messages, the dialog's own default.

The dialog itself is ui.dialogs.checkbox_confirm (plain wx controls — see
there for why not wx.RichMessageDialog).
"""

from ui.dialogs.checkbox_confirm import confirm_with_checkbox, confirm_with_two_checkboxes
from ui.dialogs.confirmation_prefs import (
    CONFIRM_CLEAR_CHAT, confirmation_enabled, disable_confirmation,
)


def confirm_clear_chat(parent, message: str, title: str, keep_starred_label: str,
                       *, yes_label: str, no_label: str,
                       main_window=None, dont_ask_label: str = ""):
    """Ask whether to clear, and whether to keep the starred messages.

    Returns `(confirmed, keep_starred)`. `keep_starred` is only meaningful
    when `confirmed` is True.

    Yes is the Enter default and holds the initial focus, as the wx.MessageBox
    this replaced did. Focus must not start on the checkbox: a habitual Space
    there would untick "keep starred" and Enter would then clear them on the
    phone too — the one irreversible outcome here.

    With `main_window` and `dont_ask_label`, the question is skipped when the
    user turned it off, and offers to turn it off otherwise. "Don't show
    again" is only honoured together with Yes.
    """
    if main_window is None or not dont_ask_label:
        return confirm_with_checkbox(
            parent, message, title, keep_starred_label,
            yes_label=yes_label, no_label=no_label,
            checked=True, default_yes=True,
        )
    if not confirmation_enabled(main_window, CONFIRM_CLEAR_CHAT):
        return True, True
    confirmed, keep_starred, dont_ask = confirm_with_two_checkboxes(
        parent, message, title, keep_starred_label, dont_ask_label,
        yes_label=yes_label, no_label=no_label,
        checked=True, default_yes=True,
    )
    if confirmed and dont_ask:
        disable_confirmation(main_window, CONFIRM_CLEAR_CHAT)
    return confirmed, keep_starred
