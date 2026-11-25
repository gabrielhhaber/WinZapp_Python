"""Freezes the Neonize bridge (``neonize_bridge/``) so a compiled build carries it.

The bridge runs under its own virtualenv (``neonize_bridge/.venv``, made by
``neonize_bridge/prepare.py``, which also installs WinZapp's build of neonize:
official neonize plus ``neonize_bridge/neonize_patches/``, compiled by
``build_neonize.py`` or downloaded as published). A compiled WinZapp has no such virtualenv, so the build freezes the
bridge with PyInstaller, in that same virtualenv, into ``neonize_bridge.exe`` +
``_internal/``; the client already starts a ``neonize_bridge.exe`` next to it when
there is one (``client/core/neonize_bridge_process.py``).

Everything that touches the system is a parameter, so the rules are tested
without running PyInstaller.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

BRIDGE_FOLDER = "neonize_bridge"
#: what PyInstaller names the frozen bridge: an .exe on Windows, a plain
#: executable in the Mac app (client/core/neonize_bridge_process.PACKAGED_EXE)
EXE_NAME = "neonize_bridge.exe" if sys.platform == "win32" else "neonize_bridge"

#: python-socketio loads its aiohttp driver by name at run time, so PyInstaller's
#: static analysis does not see it.
HIDDEN_IMPORTS = ("engineio.async_drivers.aiohttp",)
COLLECTED_SUBMODULES = ("engineio.async_drivers",)
#: neonize carries the Go DLL and the generated protobuf modules.
COLLECTED_PACKAGES = ("neonize",)


class BridgeBuildError(RuntimeError):
    """The bridge could not be frozen; the build must not ship without it."""


def bridge_dir(root: Path) -> Path:
    return Path(root) / BRIDGE_FOLDER


def venv_python(root: Path, platform_name: str = None) -> Path:
    if (platform_name or sys.platform) == "win32":
        return bridge_dir(root) / ".venv" / "Scripts" / "python.exe"
    return bridge_dir(root) / ".venv" / "bin" / "python"


def frozen_dir(out_root: Path) -> Path:
    return Path(out_root) / "dist" / "neonize_bridge"


def freeze_command(python: Path, root: Path, out_root: Path) -> list:
    """The PyInstaller command line that freezes ``server.py``."""
    out_root = Path(out_root)
    command = [str(python), "-m", "PyInstaller", "--noconfirm", "--onedir",
               "--name", "neonize_bridge",
               "--distpath", str(out_root / "dist"),
               "--workpath", str(out_root / "work"),
               "--specpath", str(out_root / "spec"),
               "--paths", str(bridge_dir(root))]
    for package in COLLECTED_PACKAGES:
        command += ["--collect-all", package]
    for package in COLLECTED_SUBMODULES:
        command += ["--collect-submodules", package]
    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]
    command.append(str(bridge_dir(root) / "server.py"))
    return command


def build_lock(root: Path, platform_name: str = None) -> Path:
    """The hashed lock of the build tools (PyInstaller and its hooks), the same
    versions as the client build. PyInstaller's wheels differ by system: the
    Mac build has its own lock, at the same versions."""
    if (platform_name or sys.platform) == "win32":
        return bridge_dir(root) / "requirements-build.lock"
    return bridge_dir(root) / "requirements-build-macos.lock"


def _upstream(root: Path):
    """neonize_bridge/neonize_upstream.py of this checkout (stdlib only)."""
    spec = importlib.util.spec_from_file_location("bridge_neonize_upstream",
                                                  bridge_dir(root) / "neonize_upstream.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_build_identity(root: Path) -> str:
    """The identity of the neonize this checkout calls for: the approved upstream
    commit, the pins and every patch in neonize_bridge/neonize_patches/."""
    return _upstream(root).checkout_identity(str(bridge_dir(root)))


def environment_is_current(root: Path) -> bool:
    """True when the bridge virtualenv holds the neonize build this checkout
    calls for (the marker build_neonize.py install and prepare.py write)."""
    marker = bridge_dir(root) / ".venv" / _upstream(root).MARKER
    try:
        return marker.read_text(encoding="utf-8").strip() == expected_build_identity(root)
    except OSError:
        return False


def build(root: Path, out_root: Path, *, run=subprocess.run, base_python: str = sys.executable,
          log=print) -> Path:
    """Freezes the bridge under ``out_root`` and returns the folder that holds
    ``neonize_bridge.exe``. Creates the bridge virtualenv first when missing."""
    root, out_root = Path(root), Path(out_root)
    python = venv_python(root)
    if not python.is_file() or not environment_is_current(root):
        log("  [bridge] the bridge virtualenv is missing or out of date; running "
            "neonize_bridge/prepare.py")
        run([base_python, str(bridge_dir(root) / "prepare.py")], check=True)
    if not python.is_file() or not environment_is_current(root):
        raise BridgeBuildError("the bridge virtualenv is not ready after prepare.py")
    run([str(python), "-m", "pip", "install", "--quiet", "--require-hashes", "-r",
         str(build_lock(root))], check=True)
    if out_root.exists():
        shutil.rmtree(out_root)
    run(freeze_command(python, root, out_root), check=True, cwd=str(bridge_dir(root)))
    result = frozen_dir(out_root)
    if not (result / EXE_NAME).is_file():
        raise BridgeBuildError(f"PyInstaller did not produce {result / EXE_NAME}")
    return result


def stage(frozen: Path, staging: Path) -> int:
    """Copies the frozen bridge into ``<staging>/neonize_bridge``; returns the
    number of files."""
    destination = Path(staging) / BRIDGE_FOLDER
    if destination.exists():
        shutil.rmtree(destination)
    # symlinks as they are: PyInstaller's macOS output links into its Python
    # framework, and a copied target breaks the bundle's code signature
    shutil.copytree(frozen, destination, symlinks=True)
    return sum(1 for item in destination.rglob("*") if item.is_file())
