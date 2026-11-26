"""Prepare the pinned Windows NetEq compiler alongside Go and MinGW."""

import os
from pathlib import Path
import runpy
import shutil
import subprocess

# Console entry points can run outside the checkout's working directory.
# Read the builder's pin by absolute path rather than importing an unpackaged
# namespace through the caller's sys.path.
_builder = runpy.run_path(str(Path(__file__).resolve().parents[1]
                             / "neonize_bridge/build_neteq.py"))
RUST, TARGET = _builder["RUST"], _builder["TARGET"]


def _install(package, path, *extra):
    winget = shutil.which("winget", path=path)
    if not winget:
        raise SystemExit(
            f"Install {package} (winget is unavailable), then run "
            "`uv run setup-neonize` again.")
    print(f"Installing {package} for NetEq...", flush=True)
    result = subprocess.run([
        winget, "install", "--id", package, "--exact", "--silent",
        "--accept-source-agreements", "--accept-package-agreements", *extra,
    ], check=False)
    if result.returncode != 0:
        raise SystemExit(
            f"Could not install {package} (exit {result.returncode}). "
            "Install it manually, then run `uv run setup-neonize` again.")


def _installer_dir():
    return (Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Microsoft Visual Studio/Installer")


def _msvc_installation(*, require_cpp=False):
    vswhere = _installer_dir() / "vswhere.exe"
    if not vswhere.is_file():
        return ""
    requires = (["-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"]
                if require_cpp else [])
    # A complete IDE can supply C++, but VCTools is a Build Tools workload.
    # Only repair that product; otherwise install standalone Build Tools.
    product = "*" if require_cpp else "Microsoft.VisualStudio.Product.BuildTools"
    result = subprocess.run([
        str(vswhere), "-products", product, "-latest", *requires,
        "-property", "installationPath",
    ], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _sdk_root():
    from .neonize_setup import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as key:
            return Path(winreg.QueryValueEx(key, "KitsRoot10")[0])
    except OSError:
        return None


def _has_windows_sdk():
    root = _sdk_root()
    if root is None:
        return False
    # Both libraries must come from one SDK version, including custom roots.
    return any((version / "ucrt/x64/ucrt.lib").is_file()
               for version in (root / "Lib").glob("*")
               if (version / "um/x64/kernel32.lib").is_file())


def _has_msvc():
    return bool(_msvc_installation(require_cpp=True)) and _has_windows_sdk()


def _prepare_msvc(path):
    installation = _msvc_installation()
    if installation:
        result = subprocess.run([
            str(_installer_dir() / "setup.exe"), "modify", "--installPath", installation,
            "--quiet", "--norestart", "--add", "Microsoft.VisualStudio.Workload.VCTools",
            "--includeRecommended",
        ], check=False)
        # Visual Studio's success-with-reboot code is not a failed install.
        if result.returncode not in (0, 3010):
            raise SystemExit(f"Visual Studio Installer failed (exit {result.returncode}). "
                             "Add Desktop development with C++ and a Windows SDK, "
                             "then retry `uv run setup-neonize`.")
    else:
        _install("Microsoft.VisualStudio.2022.BuildTools", path, "--override",
                 "--wait --quiet --norestart --add "
                 "Microsoft.VisualStudio.Workload.VCTools --includeRecommended")


def ensure_neteq_tools():
    """Install Rustup, the pinned toolchain and Microsoft's C++ linker.

    Direct build_neonize.py builds still allow the sidecar to be omitted;
    the setup command prepares it explicitly instead of silently losing it.
    """
    from .neonize_setup import _path_from_registry

    path = _path_from_registry()
    if not _has_msvc():
        _prepare_msvc(path)
        if not _has_msvc():
            raise SystemExit(
                "Visual C++ Build Tools are not ready. Add the Desktop development "
                "with C++ workload and a Windows SDK in Visual Studio Installer, then run "
                "`uv run setup-neonize` again.")

    # Rustup can already exist in CARGO_HOME even when this terminal's PATH
    # predates its installation. Respect custom homes and publish the refreshed
    # PATH for prepare.py and its build subprocesses too.
    cargo_bin = Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo"))) / "bin"
    path = os.pathsep.join((str(cargo_bin), path))
    rustup = shutil.which("rustup", path=path)
    if rustup is None:
        _install("Rustlang.Rustup", path)
        path = os.pathsep.join((str(cargo_bin), _path_from_registry()))
        rustup = shutil.which("rustup", path=path)
    if rustup is None:
        raise SystemExit("Rustup is not on PATH. Open a new terminal and run "
                         "`uv run setup-neonize` again.")
    os.environ["PATH"] = path
    toolchain = f"{RUST}-{TARGET}"
    probe = subprocess.run([rustup, "run", toolchain, "cargo", "--version"],
                           capture_output=True, check=False)
    if probe.returncode != 0:
        result = subprocess.run([rustup, "toolchain", "install", toolchain,
                                 "--profile", "minimal"], check=False)
        if result.returncode != 0:
            raise SystemExit(f"Could not install Rust {toolchain}. Run "
                             f"`rustup toolchain install {toolchain} --profile minimal` "
                             "and retry `uv run setup-neonize`.")
