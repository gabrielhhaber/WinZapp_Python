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

import atexit
import os
import shutil
import subprocess
import sys
from typing import NamedTuple

FOLDER = "neonize_bridge"
EXECUTABLE = "neonize_bridge"
#: neonize's name for each Mac architecture (neonize_bridge/neonize_platform.py)
NEONIZE_ARCH = {"arm64": "arm64", "x86_64": "amd64"}
#: how much of a failed freeze's output the build shows
LOG_TAIL = 8000


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


def require_go(which=shutil.which):
    """Stops the build before minutes of work when Go is missing."""
    if which("go") is None:
        sys.exit("Go is needed to build the Neonize bridge's neonize: brew install go "
                 "(or pass --no-bridge for a build without the Neonize engine)")


def _freezer(freezer):
    if freezer is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from winzapp_tools import bridge_build as freezer
    return freezer


def freeze(root: str, build_dir: str, *, log=print, python=sys.executable, freezer=None) -> str:
    """Freezes the bridge under ``<build_dir>/bridge``; returns the frozen folder."""
    try:
        return str(_freezer(freezer).build(root, os.path.join(build_dir, "bridge"),
                                           base_python=python, log=log))
    except Exception as error:
        sys.exit(f"the Neonize bridge could not be frozen: {error}")


def install(app: str, frozen: str, arch: str, *, log=print, freezer=None) -> str:
    """Stages a frozen bridge into the app and checks it can start. Exits the
    build on any failure: the app must not ship a dead engine."""
    staging = os.path.dirname(bundled_dir(app))
    count = _freezer(freezer).stage(frozen, staging)
    found = problems(bundled_dir(app), arch)
    if found:
        sys.exit("the bundled Neonize bridge cannot start: " + "; ".join(found))
    log(f"   {count} files in Contents/Resources/{FOLDER}")
    return bundled_dir(app)


def bundle(root: str, app: str, build_dir: str, arch: str, *, log=print, which=shutil.which,
           python=sys.executable, freezer=None):
    """Freeze the bridge and stage it into the app, one after the other."""
    require_go(which)
    frozen = freeze(root, build_dir, log=log, python=python, freezer=freezer)
    return install(app, frozen, arch, log=log, freezer=freezer)


class Freezing(NamedTuple):
    """A freeze running beside the app's own build (build_app.py)."""
    process: object
    log_path: str
    frozen: str


def start(build_dir: str, *, python=sys.executable, popen=subprocess.Popen) -> Freezing:
    """Starts freezing the bridge in a child process and returns at once.

    The freeze (prepare.py's Go build of neonize, then PyInstaller) shares
    nothing with the app's steps but the machine, so build_app.py runs it
    while it fetches Node, builds WPPConnect and freezes the app: the build
    takes about as long as before the bridge joined it. Its output goes to
    ``<build_dir>/bridge.log``, shown when it fails."""
    os.makedirs(build_dir, exist_ok=True)
    log_path = os.path.join(build_dir, "bridge.log")
    with open(log_path, "wb") as out:
        process = popen([python, os.path.abspath(__file__), "freeze", build_dir],
                        stdout=out, stderr=subprocess.STDOUT)
    # a build that stops early does not leave the freeze running
    atexit.register(stop, process)
    return Freezing(process, log_path, os.path.join(build_dir, "bridge", "dist", FOLDER))


def stop(process) -> None:
    """Ends a freeze still running (the build stopped before it needed it)."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def finish(freezing: Freezing, app: str, arch: str, *, log=print, freezer=None) -> str:
    """Waits for ``start``'s freeze and stages its bridge into the app."""
    code = freezing.process.wait()
    if code != 0:
        try:
            with open(freezing.log_path, encoding="utf-8", errors="replace") as handle:
                tail = handle.read()[-LOG_TAIL:]
        except OSError:
            tail = ""
        log(tail)
        sys.exit(f"the Neonize bridge could not be frozen (exit {code}; "
                 f"full output in {freezing.log_path})")
    return install(app, freezing.frozen, arch, log=log, freezer=freezer)


def main(argv=None):
    """``bridge_bundle.py freeze <build_dir>``: the child process ``start`` runs."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "freeze":
        sys.exit("usage: bridge_bundle.py freeze <build_dir>")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(freeze(root, argv[1], log=lambda *a: print(*a, flush=True)), flush=True)


if __name__ == "__main__":
    main()
