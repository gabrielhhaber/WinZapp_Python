"""Confirmation shown before clearing one or more conversations.

WhatsApp Web asks "clear this chat?" together with a "keep starred messages"
checkbox, ticked by default. WinZapp used to ask a bare yes/no and always kept
the starred ones; the choice now belongs to the user, same as on WhatsApp Web.

The dialog itself is ui.dialogs.checkbox_confirm (plain wx controls — see
there for why not wx.RichMessageDialog).
"""

from ui.dialogs.checkbox_confirm import confirm_with_checkbox


def confirm_clear_chat(parent, message: str, title: str, keep_starred_label: str,
                       *, yes_label: str, no_label: str):
    """Ask whether to clear, and whether to keep the starred messages.

    Returns `(confirmed, keep_starred)`. `keep_starred` is only meaningful
    when `confirmed` is True.

    Yes is the Enter default and holds the initial focus, as the wx.MessageBox
    this replaced did. Focus must not start on the checkbox: a habitual Space
    there would untick "keep starred" and Enter would then clear them on the
    phone too — the one irreversible outcome here.
    """
    return confirm_with_checkbox(
        parent, message, title, keep_starred_label,
        yes_label=yes_label, no_label=no_label,
        checked=True, default_yes=True,
    )
