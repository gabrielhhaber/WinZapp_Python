"""Short, project-local commands for common WinZapp development tasks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _run(script: str, *extra_args: str) -> None:
    """Run a repository script with the same interpreter uv selected."""
    completed = subprocess.run(
        [sys.executable, str(ROOT / script), *extra_args, *sys.argv[1:]],
        cwd=ROOT,
    )
    raise SystemExit(completed.returncode)


def app() -> None:
    """Start WinZapp; it launches and manages its local API itself."""
    _run("client/main.py")


def api() -> None:
    """Start only the already-prepared local WPPConnect API server."""
    _run("start_api.py")


def setup_api() -> None:
    """Clone, patch and build the WPPConnect API used by development builds."""
    _run("setup_api.py")


def build_onefile() -> None:
    """Build the portable one-file executable (does not require GCC/windres)."""
    _run("build.py", "--onefile")


def build_installer() -> None:
    """Build the installer and portable ZIP (requires GCC and windres)."""
    _run("build.py")


def test() -> None:
    """Run pytest without foreground wx dialogs by default."""
    completed = subprocess.run([sys.executable, "-m", "pytest", *sys.argv[1:]], cwd=ROOT)
    raise SystemExit(completed.returncode)
