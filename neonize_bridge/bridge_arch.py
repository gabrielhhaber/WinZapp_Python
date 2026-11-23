"""Tells neonize which DLL this process needs.

neonize picks ``neonize-windows-<arch>.dll`` from ``platform.machine()``. On
Windows, ``platform`` reads ``PROCESSOR_ARCHITEW6432`` before
``PROCESSOR_ARCHITECTURE``, and that variable names the *host* CPU for a process
running under emulation. The bridge ships as x64: on Windows on ARM it runs
emulated, ``platform.machine()`` answers ``ARM64`` and neonize looks for (and
downloads) an ``arm64`` DLL that an x64 process cannot load (WinError 193), so
the bridge died at import and pairing never started (reported 2026-11-23).
What matters is the architecture of this process, which the interpreter's own
build knows (``sysconfig.get_platform()``).
"""
import platform
import sys
import sysconfig

_PROCESS_ARCHITECTURES = {"win-amd64": "AMD64", "win-arm64": "ARM64", "win32": "x86"}


def process_machine(platform_name: str, reported: str) -> str:
    """The machine to report: this process's own architecture on Windows, else what
    the system reported."""
    return _PROCESS_ARCHITECTURES.get(platform_name, reported)


def pin(platform_name=None, platform_module=platform, platform_of=sys) -> bool:
    """Make ``platform.machine()`` answer for this process. True when it changed."""
    if not platform_of.platform.startswith("win"):
        return False
    wanted = process_machine(platform_name or sysconfig.get_platform(), "")
    if not wanted or platform_module.machine().upper() == wanted:
        return False
    platform_module.machine = lambda: wanted
    return True
