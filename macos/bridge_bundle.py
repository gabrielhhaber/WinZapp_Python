"""The Neonize engine in WinZapp.app: the frozen bridge in
Contents/Resources/neonize_bridge.

The bridge is the same program the Windows build ships as
neonize_bridge.exe (winzapp_tools/bridge_build.py freezes it with PyInstaller
from neonize_bridge/.venv, which neonize_bridge/prepare.py makes and fills
with WinZapp's build of neonize: on the Mac, neonize-darwin-<arch>.dylib,
compiled from source here, so the build needs Go). It sits in
Contents/Resources like the WPPConnect runtime, and runs from there: it writes
only to its data folder in Application Support (client/core/
neonize_bridge_process.py data_dir). winzapp_mac/paths_mac.py points
resource_path("neonize_bridge") at it.

libmagic is not bundled: without Homebrew's, the bridge takes mimetypes from
the file signature (neonize_bridge/media_probe.py ensure_magic_module).
"""

import os
import shutil
import sys

FOLDER = "neonize_bridge"
EXECUTABLE = "neonize_bridge"
#: neonize's name for each Mac architecture (neonize_bridge/neonize_platform.py)
NEONIZE_ARCH = {"arm64": "arm64", "x86_64": "amd64"}


def library_name(arch: str) -> str:
    """The dylib neonize loads on this Mac."""
    return f"neonize-darwin-{NEONIZE_ARCH[arch]}.dylib"


def bundled_dir(app: str) -> str:
    return os.path.join(app, "Contents", "Resources", FOLDER)


def problems(bridge: str, arch: str) -> list:
    """What a bundled bridge lacks to start (empty: nothing). Checked before
    the app is signed: an app without a working bridge would offer the Neonize
    engine and fail at its first start."""
    found = []
    exe = os.path.join(bridge, EXECUTABLE)
    if not os.path.isfile(exe):
        found.append(f"{EXECUTABLE} is missing")
    elif not os.access(exe, os.X_OK):
        found.append(f"{EXECUTABLE} is not executable")
    dylib = os.path.join(bridge, "_internal", "neonize", library_name(arch))
    if not os.path.isfile(dylib):
        found.append(f"_internal/neonize/{library_name(arch)} is missing")
    return found


def bundle(root: str, app: str, build_dir: str, arch: str, *, log=print, which=shutil.which,
           python=sys.executable, freezer=None):
    """Freeze the bridge and stage it into the app. Exits the build on any
    failure: the app must not ship a dead engine."""
    if which("go") is None:
        sys.exit("Go is needed to build the Neonize bridge's neonize: brew install go "
                 "(or pass --no-bridge for a build without the Neonize engine)")
    if freezer is None:
        sys.path.insert(0, root)
        from winzapp_tools import bridge_build as freezer
    try:
        frozen = freezer.build(root, os.path.join(build_dir, "bridge"), base_python=python, log=log)
    except Exception as error:
        sys.exit(f"the Neonize bridge could not be frozen: {error}")
    staging = os.path.dirname(bundled_dir(app))
    count = freezer.stage(frozen, staging)
    found = problems(bundled_dir(app), arch)
    if found:
        sys.exit("the bundled Neonize bridge cannot start: " + "; ".join(found))
    log(f"   {count} files in Contents/Resources/{FOLDER}")
    return bundled_dir(app)
