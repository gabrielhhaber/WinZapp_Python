"""Let Windows hand ``whatsapp:`` links to WinZapp (opt-in, current user only).

The page behind an invitation link (https://chat.whatsapp.com/...) has one
button, "Join chat", which asks the system to open ``whatsapp://chat?code=...``.
Whoever is registered for the ``whatsapp`` scheme receives it. Registering
WinZapp there is what makes a link clicked in a browser, an e-mail or another
program end in WinZapp's join dialog.

The https link itself cannot be claimed: Windows gives web links only to
packaged (Microsoft Store) apps, which is how the official WhatsApp does it.

What is written, all under HKEY_CURRENT_USER (no administrator rights):

* ``Software\\Classes\\WinZapp.WhatsAppLink``: the handler, with its command;
* ``Software\\WinZapp\\Capabilities`` and ``Software\\RegisteredApplications``:
  what lists WinZapp in Windows' "Default apps" for the scheme;
* ``Software\\Classes\\whatsapp``: the scheme itself, ONLY when nothing is
  there yet (or what is there was put by WinZapp). Another program's handler
  is never overwritten; the user picks WinZapp in "Default apps" instead.

Windows decides who answers. With more than one handler it asks the user the
first time; nothing here can (or tries to) force that choice.

The registry is reached through a three-method object so the rules are tested
against a dict, never against the machine running the tests.
"""

import os
import sys

PROG_ID = "WinZapp.WhatsAppLink"
SCHEME = "whatsapp"
APP_NAME = "WinZapp"

_CLASSES = "Software\\Classes"
_HANDLER = f"{_CLASSES}\\{PROG_ID}"
_SCHEME = f"{_CLASSES}\\{SCHEME}"
_CAPABILITIES = "Software\\WinZapp\\Capabilities"
_URL_ASSOCIATIONS = f"{_CAPABILITIES}\\URLAssociations"
_REGISTERED_APPS = "Software\\RegisteredApplications"
_COMMAND = "\\shell\\open\\command"
#: Marks a ``whatsapp`` scheme key as WinZapp's own, so turning the option
#: off removes exactly what turning it on created.
_OWNER_VALUE = "WinZappOwned"


def handler_command(argv0=None, executable=None, frozen=None) -> str:
    """The command Windows runs for a link; ``%1`` is the link.

    Mirrors autostart.get_autostart_command(): a compiled build launches the
    exe, a source checkout the interpreter with main.py."""
    argv0 = os.path.abspath(argv0 if argv0 is not None else sys.argv[0])
    executable = executable if executable is not None else sys.executable
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False)) or argv0.lower().endswith(".exe")
    if frozen:
        return f'"{argv0}" "%1"'
    return f'"{executable}" "{argv0}" "%1"'


class _UserRegistry:
    """HKEY_CURRENT_USER through the three operations the rules need."""

    def get(self, path, name):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None

    def merged_scheme_command(self):
        """The command the *merged* view (HKEY_CLASSES_ROOT: this user's keys
        over the machine's) holds for ``whatsapp:``, so a handler installed for
        every user, such as WhatsApp Desktop's, is seen too."""
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, SCHEME + _COMMAND, 0, winreg.KEY_READ) as key:
                return winreg.QueryValueEx(key, "")[0]
        except OSError:
            return None

    def set(self, path, name, value):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, path, name=None):
        """Remove one value, or with no *name* the key and everything under it."""
        import winreg
        try:
            if name is not None:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, name)
                return
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
                children = []
                while True:
                    try:
                        children.append(winreg.EnumKey(key, len(children)))
                    except OSError:
                        break
            for child in children:
                self.delete(f"{path}\\{child}")
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass  # already gone


def _notify_shell():
    """Tell Explorer the associations changed (best effort)."""
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
    except Exception:
        pass


def is_registered(command=None, registry=None) -> bool:
    """True when WinZapp is registered as a ``whatsapp:`` handler with this
    command (a moved or reinstalled WinZapp reads as not registered)."""
    registry = registry or _UserRegistry()
    command = command or handler_command()
    return (registry.get(_HANDLER + _COMMAND, "") == command
            and registry.get(_URL_ASSOCIATIONS, SCHEME) == PROG_ID
            and registry.get(_REGISTERED_APPS, APP_NAME) == _CAPABILITIES)


def register(command=None, registry=None) -> None:
    """Register WinZapp for ``whatsapp:`` links. Raises OSError on failure."""
    notify = registry is None
    registry = registry or _UserRegistry()
    command = command or handler_command()
    registry.set(_HANDLER, "", "URL:WhatsApp link")
    registry.set(_HANDLER, "URL Protocol", "")
    registry.set(_HANDLER + _COMMAND, "", command)
    registry.set(_CAPABILITIES, "ApplicationName", APP_NAME)
    registry.set(_CAPABILITIES, "ApplicationDescription", APP_NAME)
    registry.set(_URL_ASSOCIATIONS, SCHEME, PROG_ID)
    registry.set(_REGISTERED_APPS, APP_NAME, _CAPABILITIES)
    # The scheme itself, only when it is free or already WinZapp's.
    # Looked up in the merged view: this user's own key would otherwise hide a
    # handler installed machine-wide, and writing ours would override it.
    existing = registry.merged_scheme_command()
    if existing is None or registry.get(_SCHEME, _OWNER_VALUE) == "1":
        registry.set(_SCHEME, "", "URL:WhatsApp link")
        registry.set(_SCHEME, "URL Protocol", "")
        registry.set(_SCHEME, _OWNER_VALUE, "1")
        registry.set(_SCHEME + _COMMAND, "", command)
    if notify:
        _notify_shell()


def unregister(registry=None) -> None:
    """Remove what register() wrote, and nothing else."""
    notify = registry is None
    registry = registry or _UserRegistry()
    registry.delete(_HANDLER)
    registry.delete(_CAPABILITIES)
    registry.delete(_REGISTERED_APPS, APP_NAME)
    if registry.get(_SCHEME, _OWNER_VALUE) == "1":
        registry.delete(_SCHEME)
    if notify:
        _notify_shell()
