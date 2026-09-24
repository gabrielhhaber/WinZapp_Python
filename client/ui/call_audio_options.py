"""Behaviour shared by the call audio options wherever they are shown."""

import wx


def confirm_exclusive_output(parent, i18n, event, check) -> None:
    """Warn when the speaker is about to be held exclusively.

    Only on the way ON, and only for the output device. Exclusive access to the
    speaker silences every other application on it for the whole call, screen
    reader included -- which for this app's users means losing the spoken
    labels of the call window's own controls, with no way to find out why. The
    microphone box has no equivalent cost and therefore no equivalent
    interruption.

    A modal box rather than a spoken line: the screen reader reads it natively,
    and it cannot be missed the way a passing announcement can.

    And a confirmation rather than a notice. An OK-only box leaves the option
    ticked whatever the user does, so a warning whose stated cost is "your
    screen reader will not speak during the call" could be dismissed by reflex.
    Answering No unticks it, which makes the default outcome of not reading the
    box the safe one.
    """
    if not event.IsChecked():
        event.Skip()
        return
    confirmed = wx.MessageBox(
        i18n.t("calls_exclusive_output_warning"),
        i18n.t("tab_calls"),
        wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        parent,
    )
    if confirmed != wx.YES:
        check.SetValue(False)
    event.Skip()
