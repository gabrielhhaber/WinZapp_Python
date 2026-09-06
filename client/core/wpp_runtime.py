"""The homologated WPPConnect Server release, read from one committed file.

``client/wpp_minimum_version.txt`` holds the WPPConnect Server version this
build was validated against (plain text, just the version string). Four call
sites need it — the startup version gate (``main.py``), the in-app update
checker (``updater.py``), the end-user reinstall dialog
(``ui/dialogs/api_setup.py``) and the dev/CI setup script (``setup_api.py``,
which reaches the file by path rather than through ``resource_path``). Each of
them used to carry its own copy of the read, and each had picked a different
set of exceptions to swallow.

Deliberately stdlib-only, for the same reason the ``wppconnect_*_patch``
modules are: ``setup_api.py`` imports it, and that script has to run before any
client-side dependency is installed.
"""


def read_homologated_wpp_version(path: str) -> str:
    """The homologated version string, or "" when it cannot be read.

    ``ValueError`` is caught alongside ``OSError`` because a truncated or
    otherwise corrupted file raises ``UnicodeDecodeError`` — a ``ValueError``
    subclass, not an ``OSError``. That distinction is not academic here: the
    startup gate calls this outside any local try block, so the exception
    would climb to ``MainWindow.__init__``'s blanket handler and skip both
    ``ensure_wpp_version()`` and ``ensure_wpp_running()``, leaving the app open
    with no Node server behind it and nothing said out loud.
    """
    try:
        # utf-8-sig, not utf-8: the file is committed and edited by hand on
        # Windows, where an editor saving it with a BOM would otherwise leave
        # the BOM character glued to the front of the version — and to the
        # front of the tag every install path then tries to check out.
        with open(path, encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return ""


def homologated_wpp_tag(path: str) -> str:
    """The same value as the git tag every install path checks out ("v2.10.16").

    Empty when the file is unreadable, which every caller treats as "no
    homologated tag pinned" and falls back to resolving one over the network.
    """
    version = read_homologated_wpp_version(path)
    return f"v{version.lstrip('vV')}" if version else ""
