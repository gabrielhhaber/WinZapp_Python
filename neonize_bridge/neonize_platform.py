"""Which system the neonize library is built for, and the names that follow from it.

build_neonize.py compiles neonize's Go code (with whatsmeow and meowcaller) into a
shared library: a DLL on Windows, a dylib on macOS, a .so on Linux (CI only).
neonize loads it by a name made of the system and the architecture
(``neonize/utils/platform.py``, ``generated_name``): ``neonize-windows-amd64.dll``,
``neonize-darwin-arm64.dylib``, ``neonize-darwin-amd64.dylib``. Everything here is
pure, so the rules are tested on any system.

The Windows bridge ships as x64 only (``bridge_arch.py``): on Windows the target
is always amd64, whatever the host. On macOS it is the architecture of the
Python that runs the build, so an Intel Python under Rosetta builds the Intel
library its own process can load.
"""
import os
import platform
import sys
from typing import NamedTuple

#: neonize's own names for the architectures (``arch_normalizer``)
_ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_SYSTEMS = {"win32": "windows", "darwin": "darwin", "linux": "linux"}
_EXTENSIONS = {"windows": "dll", "darwin": "dylib", "linux": "so"}
#: protoc's release asset suffix for each target
_PROTOC = {("windows", "amd64"): "win64",
           ("darwin", "arm64"): "osx-aarch_64", ("darwin", "amd64"): "osx-x86_64",
           ("linux", "amd64"): "linux-x86_64", ("linux", "arm64"): "linux-aarch_64"}
#: what the C compiler's ``-dumpmachine`` starts with when it builds for the target
_COMPILER_PREFIX = {"amd64": ("x86_64",), "arm64": ("arm64", "aarch64")}


class Target(NamedTuple):
    system: str     # windows, darwin, linux
    arch: str       # amd64, arm64


def target(platform_name=None, machine=None) -> Target:
    """The target this build runs for. ``platform_name`` is ``sys.platform``,
    ``machine`` is ``platform.machine()``. ValueError for a system or an
    architecture the bridge is not built for."""
    platform_name = platform_name or sys.platform
    system = next((name for prefix, name in _SYSTEMS.items()
                   if platform_name.startswith(prefix)), None)
    if system is None:
        raise ValueError(f"the Neonize bridge is not built for {platform_name}")
    if system == "windows":
        return Target(system, "amd64")
    raw = (machine if machine is not None else platform.machine()).lower()
    arch = _ARCHITECTURES.get(raw)
    if arch is None:
        raise ValueError(f"the Neonize bridge is not built for {raw or 'this'} processors")
    return Target(system, arch)


def library_name(build: Target) -> str:
    """The file name neonize loads the compiled library by."""
    return f"neonize-{build.system}-{build.arch}.{_EXTENSIONS[build.system]}"


def asset_name(version: str, build: Target) -> str:
    """The zip build_neonize.py makes (and the "Neonize DLL" workflow publishes)."""
    return f"neonize-{version}-winzapp-{build.system}-{build.arch}.zip"


def protoc_asset_key(build: Target) -> str:
    """Which of the manifest's protoc downloads runs on this target."""
    return _PROTOC[(build.system, build.arch)]


def executable(name: str, build: Target) -> str:
    """A program's file name: ``.exe`` on Windows."""
    return f"{name}.exe" if build.system == "windows" else name


def venv_scripts(venv: str, build: Target) -> str:
    """The folder of a virtualenv's programs."""
    return os.path.join(venv, "Scripts" if build.system == "windows" else "bin")


def venv_python(venv: str, build: Target) -> str:
    """A virtualenv's interpreter."""
    return os.path.join(venv_scripts(venv, build), executable("python", build))


def default_compiler(build: Target) -> str:
    """The C compiler cgo uses when ``CC`` is not set: MinGW gcc on Windows,
    Apple's clang (``cc``) on macOS."""
    return "cc" if build.system == "darwin" else "gcc"


def compiler_problem(build: Target, dumpmachine: str) -> str:
    """Why a C compiler whose ``-dumpmachine`` says ``dumpmachine`` cannot build
    the library for ``build``, or "" when it can."""
    machine = dumpmachine.strip().lower()
    if not machine.startswith(_COMPILER_PREFIX[build.arch]):
        if build.system == "windows":
            return f"the compiler builds for {machine}; the DLL needs a 64-bit (x86_64) MinGW gcc."
        return f"the compiler builds for {machine}, not for {build.arch}."
    if build.system == "darwin" and "apple-darwin" not in machine:
        return f"the compiler builds for {machine}, not for macOS (install the Xcode command line tools)."
    if build.system == "windows" and "mingw" not in machine and "windows" not in machine:
        return f"the compiler builds for {machine}; the DLL needs a 64-bit MinGW gcc."
    return ""
