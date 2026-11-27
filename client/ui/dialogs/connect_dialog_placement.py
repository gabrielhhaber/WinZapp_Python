"""Where the "Connect to WinZapp" dialog sits relative to the main window.

The dialog used to be created with no parent. With no parent, Windows does not
keep it above the main window: after File > Disconnect the main window (chat
list and navigation menu) stayed in front of the QR code while keyboard focus
and the screen reader were already on the dialog. A person using Be My Eyes or
any visual check saw the chat list and never the QR code, so the phone could not
scan it (reported 2026-11-27 on the WPPConnect engine).

A dialog owned by a visible main window always stays above it. With the main
window not shown yet (first launch) or minimised/hidden to the tray, an owner
would drag a hidden window along, so the dialog stays parentless there, as before.

Kept free of wx imports so it can be tested with plain stubs.
"""


def connection_dialog_parent(main_window):
    """The main window when it is on screen, else None (no parent)."""
    if main_window is None:
        return None
    try:
        if main_window.IsShown() and not main_window.IsIconized():
            return main_window
    except Exception:
        # A frame being destroyed raises on any call; no parent is safe.
        pass
    return None


def bring_to_front(dialog):
    """Raise a dialog above every other window of the app, best effort."""
    for name in ("Raise", "SetFocus"):
        try:
            getattr(dialog, name)()
        except Exception:
            pass
