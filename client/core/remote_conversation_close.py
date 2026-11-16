"""Close the open conversation after a change made on another device, and say why.

A chat locked on the phone or deleted on another device must leave the screen.
The panel-switch close keeps focus where it is, which is right when the user is
elsewhere but leaves a screen reader on hidden controls when they were reading
that conversation: then the Esc path returns focus to the list it came from.
"""
import wx


def focused_window():
    return wx.Window.FindFocus()


def focus_inside(window) -> bool:
    focused = focused_window() if window is not None else None
    while focused is not None:
        if focused is window:
            return True
        focused = focused.GetParent()
    return False


def _open_in(panel, aliases) -> bool:
    return (getattr(panel, "conversation", None) or {}).get("remoteJid") in aliases


def close_for_remote_change(mw, aliases, message_key) -> bool:
    """Close the conversation if one of *aliases* is open and announce
    *message_key*. Returns False only when it is still open afterwards."""
    panel = getattr(mw, "conversations_panel", None)
    if panel is None or not _open_in(panel, aliases):
        return True
    had_focus = focus_inside(getattr(panel, "conversation_panel", None))
    # The first call may only dismiss the mention-suggestions popup.
    for _attempt in range(2):
        if had_focus:
            panel.close_conversation()
        else:
            panel.close_conversation_for_panel_switch()
        if not _open_in(panel, aliases):
            break
    if _open_in(panel, aliases):
        return False
    mw.output(mw.i18n.t(message_key), interrupt=True)
    return True
