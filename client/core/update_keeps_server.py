"""A WinZapp update must not put back an older WPPConnect Server.

The release ZIP carries api/package.json and api/dist/, built from the tag in
wpp_minimum_version.txt, and the installer's xcopy writes them over the
installed api/. A user who had updated the server past that tag since (the
periodic check, Help > Force Reinstall, background or in place) was silently
put back by the next WinZapp update: package.json said the bundled version
again, so the same server update was offered again and the user concluded it
had never happened. Reported live on the alpha channel, which ships several
builds a day: 2.10.36 installed at 08:22 (the server itself logged it), back
to 2.10.30 after the WinZapp update at 10:32.

Only package.json is kept, never dist/. dist/ is how WinZapp's own patched
controllers (api_patches/src/) reach an install, and the WinZapp being
installed may call routes only its own dist/ serves, so it still comes from
the release. What package.json keeps is what describes the installed tree: its
version, which is what every version check reads, and its dependency ranges,
which match the node_modules xcopy never touches. The pins WinZapp homologates
(_PATCHED_DEPENDENCY_KEYS) are taken from the release instead, so a build that
moves one still trips the library drift gate (core/wpp_runtime.library_drifts)
and offers the reinstall.

What that leaves: upstream source that changed between the bundled tag and
the installed one runs as the bundled tag's until the next server update. Every
upstream release from 2.10.27 to 2.10.37 changed dependencies only, which live
in node_modules, so today that is nothing.

Plain functions over paths, no wx, so the decision is tested on temporary
directories.
"""

import json
import logging
import os


def _read_package(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_newer_server(installed: str, shipped: str) -> bool:
    """Whether *installed* is strictly newer than *shipped*. Anything that
    cannot be compared answers False: the release's package.json is then
    copied as it always was."""
    if not installed or not shipped:
        return False
    try:
        from packaging.version import Version
        return Version(installed.lstrip("vV")) > Version(shipped.lstrip("vV"))
    except Exception:
        return False


def merged_package(installed: dict, shipped: dict, pinned_keys) -> dict:
    """The installed package.json with the release's homologated pins."""
    merged = dict(installed)
    deps = dict(merged.get("dependencies") or {})
    shipped_deps = shipped.get("dependencies") or {}
    for key in pinned_keys:
        if key in shipped_deps:
            deps[key] = shipped_deps[key]
    merged["dependencies"] = deps
    return merged


def keep_newer_installed_server(payload_api_dir: str, installed_api_dir: str,
                                pinned_keys) -> str:
    """Before the installer runs: when the installed server is newer than the
    one in the extracted release, rewrite the release's api/package.json so
    xcopy writes the installed one back (merged_package()). Returns the
    version kept, or "" when the release's file is left as it is.

    Never raises: a failure here leaves the update as it was before this
    existed, which is a downgrade, not a broken install.
    """
    payload_pkg = os.path.join(payload_api_dir, "package.json")
    shipped = _read_package(payload_pkg)
    installed = _read_package(os.path.join(installed_api_dir, "package.json"))
    shipped_version = str(shipped.get("version") or "")
    installed_version = str(installed.get("version") or "")
    if not is_newer_server(installed_version, shipped_version):
        return ""
    tmp = payload_pkg + ".winzapp-tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged_package(installed, shipped, pinned_keys), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, payload_pkg)
    except OSError as exc:
        logging.warning("[update] Could not keep WPPConnect Server %s over the %s "
                        "this release ships: %s", installed_version, shipped_version, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ""
    logging.info("[update] Keeping WPPConnect Server %s: this release ships %s, "
                 "and its package.json would have put the older one back.",
                 installed_version, shipped_version)
    return installed_version
