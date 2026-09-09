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


#: The npm package whose compiled output WinZapp patches in place (see
#: core/wppconnect_*_patch.py). Its version is pinned exactly by
#: api/package.json, and the patches are search-and-replace against that exact
#: version's source text.
WPPCONNECT_PACKAGE = "@wppconnect-team/wppconnect"


def _read_json(path: str) -> dict:
    """Parse a JSON file, or {} when it cannot be read at all.

    Same swallow-everything contract as read_homologated_wpp_version() above,
    and for the same reason: these run on the startup path, outside any local
    try block, and an exception here would skip the Node server coming up at
    all.
    """
    try:
        import json
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def installed_wppconnect_version(api_dir: str) -> str:
    """The wppconnect library version actually present in node_modules, or "".

    Deliberately NOT api/package.json's own "version" field, which is the
    WPPConnect *Server* version — a different number, and one that ships
    inside the release ZIP, so an update always rewrites it.
    """
    path = _join(api_dir, "node_modules", *WPPCONNECT_PACKAGE.split("/"),
                 "package.json")
    version = _read_json(path).get("version")
    return version.strip() if isinstance(version, str) else ""


def required_wppconnect_version(api_dir: str) -> str:
    """The exact version api/package.json pins, or "" when it is not exact.

    A range ("^2.3.3", "~2.3.3", "*", a git URL) answers "" on purpose:
    deciding whether an installed version satisfies a range needs a semver
    resolver this module cannot have (stdlib-only — setup_api.py imports it
    before any dependency is installed), and guessing wrong here would either
    miss a real drift or force a multi-minute reinstall on a healthy install.
    WinZapp pins exactly; anything else is somebody's local edit, and the
    honest answer to it is "I cannot tell".
    """
    deps = _read_json(_join(api_dir, "package.json")).get("dependencies")
    pin = deps.get(WPPCONNECT_PACKAGE) if isinstance(deps, dict) else None
    if not isinstance(pin, str):
        return ""
    pin = pin.strip()
    # An exact pin is digits and dots, optionally with a pre-release suffix.
    if not pin or not pin[0].isdigit():
        return ""
    return pin


def wppconnect_library_drift(api_dir: str):
    """(installed, required) when node_modules holds a different wppconnect
    than api/package.json pins; None when they agree or either is unreadable.

    This is the mismatch nothing checked, and the one that actually breaks
    people. The release ZIP excludes node_modules (build.py's
    API_EXCLUDE_DIRS), so an update ships a new dist/server.js and a new
    package.json over whatever wppconnect the machine already had. The
    startup gate could never catch it: it compares api/package.json's server
    version against wpp_minimum_version.txt, and BOTH of those files come out
    of the same ZIP, so after an update they always agree.

    What breaks is the patching. core/wppconnect_*_patch.py rewrites the
    library's compiled output by searching for exact source text, so against
    an unexpected version the patch does not fail — it is silently skipped
    ("DID NOT MATCH ... left untouched"). Observed live on a user's install
    whose host.layer.js checkQrCode and loginByCode patches were both skipped:
    the pairing-code path went unpatched and the session cycled
    CLOSED/INITIALIZING without ever reaching CONNECTED.

    Any difference counts, not just an older one — the patches target one
    exact version's source, so a newer library misses them just as an older
    one does.

    Returns None on anything unreadable rather than guessing: the caller's
    response to drift is a full re-download and rebuild of the server, which
    is far too expensive to trigger on a file this could not parse.
    """
    installed = installed_wppconnect_version(api_dir)
    required = required_wppconnect_version(api_dir)
    if not installed or not required or installed == required:
        return None
    return (installed, required)


def _join(*parts: str) -> str:
    import os
    return os.path.join(*parts)
