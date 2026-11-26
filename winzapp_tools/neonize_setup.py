"""Install the native tools needed to prepare the Neonize bridge on Windows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

if sys.platform == "win32":
    import winreg


_WINGET_PACKAGES = {
    "go": "GoLang.Go",
    "gcc": "BrechtSanders.WinLibs.POSIX.UCRT",
}


def _path_from_registry() -> str:
    """Include newly installed user and machine PATH entries in this process."""
    values = [os.environ.get("PATH", "")]
    for root, subkey in (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if kind == winreg.REG_EXPAND_SZ:
            value = winreg.ExpandEnvironmentStrings(value)
        values.append(value)
    return os.pathsep.join(dict.fromkeys(
        part for value in values for part in value.split(os.pathsep) if part))


def _has_tool(name: str, path: str) -> bool:
    executable = shutil.which(name, path=path)
    if not executable:
        return False
    if name == "gcc":
        result = subprocess.run([executable, "-dumpmachine"], capture_output=True,
                                text=True, check=False)
        return result.returncode == 0 and "x86_64" in result.stdout.lower()
    return subprocess.run([executable, "version"], capture_output=True,
                          check=False).returncode == 0


def ensure_build_tools() -> None:
    """Install Go and 64-bit MinGW with winget when they are not available."""
    if sys.platform != "win32":
        raise SystemExit("The Neonize bridge setup is supported on Windows only.")

    path = _path_from_registry()
    missing = [name for name in _WINGET_PACKAGES if not _has_tool(name, path)]
    if not missing:
        os.environ["PATH"] = path
        from .neonize_neteq_setup import ensure_neteq_tools
        ensure_neteq_tools()
        return

    winget = shutil.which("winget", path=path)
    if not winget:
        raise SystemExit(
            "winget is required to install the Neonize build tools. Install App Installer "
            "from Microsoft Store, then run `uv run setup-neonize` again.")

    for name in missing:
        package = _WINGET_PACKAGES[name]
        print(f"Installing {name} ({package}) with winget...", flush=True)
        result = subprocess.run([
            winget, "install", "--id", package, "--exact", "--silent",
            "--accept-source-agreements", "--accept-package-agreements",
        ], check=False)
        if result.returncode != 0:
            raise SystemExit(
                f"winget could not install {package} (exit {result.returncode}). "
                "Install it manually, then run `uv run setup-neonize` again.")
        path = _path_from_registry()

    still_missing = [name for name in _WINGET_PACKAGES if not _has_tool(name, path)]
    if still_missing:
        raise SystemExit(
            "Installed the build tools, but they are not on PATH yet: "
            + ", ".join(still_missing)
            + ". Open a new terminal and run `uv run setup-neonize` again.")
    os.environ["PATH"] = path
    from .neonize_neteq_setup import ensure_neteq_tools
    ensure_neteq_tools()
