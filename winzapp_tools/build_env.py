"""Decisions build.py and setup_api.py have to make before importing anything heavy.

Kept out of build.py itself because that script parses argv and downloads
ffmpeg/libopus at import time, so none of its logic can be reached from a
test. setup_api.py is the second consumer, for the Node.js version rules at
the bottom of this file. Standard library only: a bare system interpreter may
run this before it hands the build over to a virtual environment, and
setup_api.py runs before client-side dependencies are installed at all.
"""

from __future__ import annotations

import os
import subprocess

# Set in the child build.py runs under, so the hand-over happens at most once.
REEXEC_MARKER = "WINZAPP_BUILD_PYTHON_SELECTED"


def _venv_python(venv_dir: str) -> str:
    return os.path.join(venv_dir, "Scripts", "python.exe")


def select_build_python(
    environ,
    running_python: str,
    running_in_virtualenv: bool,
    root_dir: str,
    isfile=os.path.isfile,
    running_has_pyinstaller: bool = True,
) -> str:
    """Return the interpreter the build must run under.

    Every rung is a way somebody already runs build.py, and none may stop
    working because uv became an option:

      1. ``WINZAPP_VENV`` names a virtual environment explicitly
         (build_zip_only.py's ``venv_build``, forks with their own layout).
      2. The running interpreter, when it already is a virtual environment
         that can build: ``uv run build-onefile``, an activated venv, or
         ``venv\\Scripts\\python.exe build.py``. A venv without PyInstaller
         is some unrelated environment an editor happened to activate, and
         build.py used to ignore it and build with ``venv\\`` anyway.
      3. The repository's ``venv\\`` and then uv's ``.venv\\``. ``python
         build.py`` from a bare system interpreter always built with
         ``venv\\`` — build.py hardcoded it — and must keep doing so.
      4. The running interpreter, when there is nothing better.
    """
    explicit = (environ.get("WINZAPP_VENV") or "").strip()
    if explicit:
        return _venv_python(explicit)
    if running_in_virtualenv and running_has_pyinstaller:
        return running_python
    for name in ("venv", ".venv"):
        candidate = _venv_python(os.path.join(root_dir, name))
        if isfile(candidate):
            return candidate
    return running_python


def same_interpreter(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def hand_over_to_build_python(script: str, root_dir: str) -> None:
    """Re-run ``script`` under select_build_python()'s pick, if it isn't this one.

    Must run before the build touches site-packages, whose location depends
    on the interpreter. Every entry point that builds calls it — build.py and
    build_zip_only.py — since importing build.py never runs its __main__.
    Returns only when the current interpreter is the right one.
    """
    import importlib.util
    import sys

    if os.environ.get(REEXEC_MARKER):
        return
    target = select_build_python(
        os.environ,
        sys.executable,
        sys.prefix != sys.base_prefix,
        root_dir,
        running_has_pyinstaller=importlib.util.find_spec("PyInstaller") is not None,
    )
    if same_interpreter(target, sys.executable):
        return
    if not os.path.isfile(target):
        print(f"[ERROR] WINZAPP_VENV points at {target}, which does not exist.")
        sys.exit(1)
    print(f"  [python] Building with {target}", flush=True)
    env = dict(os.environ, **{REEXEC_MARKER: "1"})
    try:
        completed = subprocess.run([target, os.path.abspath(script), *sys.argv[1:]], env=env)
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(completed.returncode)


def portable_node_version(node_exe: str, run=subprocess.run) -> str:
    """The version ``node_exe`` reports, without its ``v``; "" if it cannot say."""
    if not os.path.isfile(node_exe):
        return ""
    try:
        probe = run(
            [node_exe, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if probe.returncode != 0:
        return ""
    return (probe.stdout or "").strip().lstrip("vV")


def portable_node_needs_replacing(installed_version: str, homologated_version: str) -> bool:
    """Whether the Node.js a build would bundle is not the homologated one.

    Exact match, not a floor, and deliberately stricter than the app's own
    runtime gate (``node_runtime_needs_download()`` in main.py only upgrades
    an older runtime). A build decides which Node every user receives, and
    WPPConnect Server pins ``engines.node`` exactly: a newer local copy
    — 24.x was what CI shipped until 22.22.2 was homologated — would go on
    being bundled with no warning at all.
    """
    return installed_version != homologated_version


def node_major(version: str):
    """The integer major of a ``X.Y.Z`` version string, or None if unreadable."""
    head = (version or "").strip().lstrip("vV").split(".", 1)[0]
    return int(head) if head.isdigit() else None


def system_node_is_refused(installed_version: str, homologated_version: str) -> bool:
    """Whether a system Node.js must NOT stand in for the homologated runtime.

    setup_api.py prefers ``client/node/node.exe`` and falls back to whatever
    ``node`` is on PATH when that folder is absent — which is the state of
    every fresh checkout, since only build.py and CI provision it. That
    fallback accepted any version at all, and the way it fails is the reason
    this exists: on Node 26, puppeteer's ``extract-zip@2.0.1`` never settles
    the promisified ``stream.pipeline`` of the first multi-chunk zip entry, so
    the Chromium download stops two files in, throws nothing and resolves
    nothing. Puppeteer then finds a browser folder with no ``chrome.exe``,
    refuses to re-download, and every later run fails on the stub it left
    behind. Nothing in that chain names Node, and none of it is recoverable by
    re-running the command.

    Compared by MAJOR only, against the one runtime this path is actually
    verified on: upstream pins ``engines.node`` exactly, ``client/node/``
    ships exactly that, and CI builds on nothing else.

    **Only an answer refuses.** An empty version is a probe that could not
    speak — node.exe missing from PATH, or ``--version`` timing out on a cold
    machine — never a verdict, and the same rule setup_api.py's own npm health
    probe was taught after a 10s timeout failed a release build: a check must
    not be more fatal than the thing it stands in for. A genuinely broken Node
    still fails loudly, and legibly, at ``npm install`` a moment later.
    """
    installed_major = node_major(installed_version)
    homologated_major = node_major(homologated_version)
    if installed_major is None or homologated_major is None:
        return False
    return installed_major != homologated_major
