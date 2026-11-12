"""Downloaded updates the user chose to install later.

Settings > General > "download updates in the background" finishes a WinZapp
or WPPConnect update while the user works, and then asked to install it with
an OK button only: both installs take the app (or its API) down for a while,
at a moment the user did not choose. Now the notice offers "Install now" and
"Install later" (the default, and what Escape answers). Later keeps what was
prepared — the extracted WinZapp package, the WPPConnect server built next to
the running one — and Help > "Install downloaded update" installs it when the
user wants; the item is enabled only while something waits.

What is installed, and every check on it, is unchanged: the install runs the
same code the notice's OK ran (update_background.py, wpp_background_update.py).
"""

import logging

import wx

from core.dialog_foreground import choice_box


def ask_install_now(mw, text, title) -> bool:
    """The ready notice as a question; True for "Install now"."""
    t = mw.i18n.t
    return choice_box(mw, text, title, t("update_install_now"), t("update_install_later"),
                      announce=lambda: mw.output(text, interrupt=True))


def app_pending(mw):
    checker = getattr(mw, "_update_checker", None)
    return getattr(checker, "_pending_install", None) if checker is not None else None


def wpp_pending(mw):
    return getattr(mw, "_pending_wpp_install", None)


def has_pending(mw) -> bool:
    return bool(app_pending(mw) or wpp_pending(mw))


def refresh_menu(mw) -> None:
    """Enable Help > "Install downloaded update" only while something waits."""
    item_id = getattr(mw, "_ID_INSTALL_PENDING", None)
    bar = mw.GetMenuBar() if hasattr(mw, "GetMenuBar") else None
    if item_id is None or bar is None:
        return
    item = bar.FindItemById(item_id)
    if item is not None:
        item.Enable(has_pending(mw))


def said_later(mw) -> None:
    """After "Install later": where to find it again."""
    t = mw.i18n.t
    mw.output(t("update_install_later_said").format(
        menu=t("menu_help").replace("&", ""),
        option=t("menu_install_pending_update").replace("&", "")), interrupt=True)
    refresh_menu(mw)


def install_pending(mw, may_interrupt=None) -> bool:
    """Help > "Install downloaded update". The WPPConnect server first (it
    only restarts the API); a WinZapp update after it, since that one closes
    the app. Returns whether an install started."""
    if may_interrupt is None:
        from update_background import may_interrupt_now as may_interrupt
    if not has_pending(mw):
        refresh_menu(mw)
        return False
    if not may_interrupt(mw):
        mw.output(mw.i18n.t("update_install_not_now"), interrupt=True)
        return False
    wpp = wpp_pending(mw)
    if wpp:
        mw._pending_wpp_install = None
        refresh_menu(mw)
        target_tag, staged, on_finished = wpp

        def finished(ok, chain=on_finished):
            if chain is not None:
                chain(ok)
            if app_pending(mw):
                wx.CallAfter(install_pending, mw)
        logging.info("[pending-install] installing the downloaded WPPConnect update")
        mw._install_staged_wpp_now(target_tag, staged, finished)
        return True
    logging.info("[pending-install] installing the downloaded WinZapp update")
    mw._update_checker.install_pending_update()
    refresh_menu(mw)
    return True
