"""The install-wide half of MainWindow that a SettingsDialog test frame needs.

The dialog loads the install-wide keys from the shared file
(MainWindow.install_wide_values()) and hands the ones the user changed to
MainWindow.choose_global_settings() on OK. A test frame built from
loose attributes has neither, so it gets the real methods bound onto it --
never a lambda standing in for them, which is how a wrong attribute name once
hid a bug here twice. With no `_app_settings` on the frame, both fall back to
frame.settings as they do on a legacy install, which is what the round-trip
tests read back.
"""

import threading
import types

from main import MainWindow

_METHODS = ("choose_global_settings", "install_wide_values", "_persist_global_settings",
            "_shared_shortcut_mirror", "_compose_shortcuts")


def give_global_settings(frame):
    frame._save_lock = threading.Lock()
    for name in _METHODS:
        setattr(frame, name, types.MethodType(getattr(MainWindow, name), frame))
    return frame
