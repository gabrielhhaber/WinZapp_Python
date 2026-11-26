"""Short, project-local commands for common WinZapp development tasks.

Installed by ``uv sync`` as ``uv run winzapp``, ``uv run test`` and so on.
Each one only runs the script it names, so the plain ``venv`` workflow —
``cd client; python main.py``, ``python build.py`` — stays exactly as valid.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "client"

# name -> (script, fixed args, working directory)
#
# The client runs from client/, never from the repository root: in dev mode
# app_paths._data_root() is os.getcwd()/data, so the documented
# `cd client; python main.py` keeps its accounts, pairing and messages under
# client/data/. Launched from the root it would silently start a second,
# empty install under data/ and ask to pair again.
_COMMANDS = {
    "app": ("main.py", (), CLIENT),
    "api": ("start_api.py", (), ROOT),
    "setup_api": ("setup_api.py", (), ROOT),
    "build_onefile": ("build.py", ("--onefile",), ROOT),
    "build_installer": ("build.py", (), ROOT),
}


def command_for(name: str, argv, python: str = sys.executable):
    """The command line and working directory a project command runs."""
    script, fixed, cwd = _COMMANDS[name]
    return [python, str(cwd / script), *fixed, *argv], cwd


def _exec(cmd, cwd) -> None:
    try:
        completed = subprocess.run(cmd, cwd=cwd)
    except KeyboardInterrupt:
        # The child received the same Ctrl+C; don't bury its output under a
        # traceback from this wrapper.
        raise SystemExit(130)
    raise SystemExit(completed.returncode)


def _run(name: str) -> None:
    _exec(*command_for(name, sys.argv[1:]))


def app() -> None:
    """Start WinZapp; it launches and manages its local API itself."""
    _run("app")


def api() -> None:
    """Start only the already-prepared local WPPConnect API server."""
    _run("api")


def setup_api() -> None:
    """Clone, patch and build the WPPConnect API used by development builds."""
    _run("setup_api")


def setup_neonize() -> None:
    """Prepare the Neonize bridge; ``--with-neteq`` also installs Rust and C++ tools."""
    from .neonize_setup import ensure_build_tools
    args = [arg for arg in sys.argv[1:] if arg != "--with-neteq"]
    ensure_build_tools(with_neteq=len(args) != len(sys.argv) - 1)
    _exec([sys.executable, str(ROOT / "neonize_bridge" / "prepare.py"), *args], ROOT)


def build_onefile() -> None:
    """Build the portable one-file executable (does not require GCC/windres)."""
    _run("build_onefile")


def build_installer() -> None:
    """Build the installer and portable ZIP (requires GCC and windres)."""
    _run("build_installer")


def test() -> None:
    """Run pytest without foreground wx dialogs by default."""
    _exec([sys.executable, "-m", "pytest", *sys.argv[1:]], ROOT)


def _translations(*fixed_args: str) -> int:
    """Run the gettext workflow from the repository root."""
    from .translations import main
    return main([*fixed_args, *sys.argv[1:]])


def translations_update() -> int:
    """Merge the English PO source into every registered locale."""
    return _translations("update")


def translations_check() -> int:
    """Validate complete translation catalogs for a release."""
    return _translations("check")


def translations_check_draft() -> int:
    """Validate catalog syntax while translators still have pending entries."""
    return _translations("check", "--allow-incomplete")


def translations_compile() -> int:
    """Validate and compile MO resources for local builds."""
    return _translations("compile")
