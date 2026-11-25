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

Under that same emulation the Go runtime inside the DLL must not preempt
goroutines asynchronously: Go suspends a thread and rewrites its context to
inject a call, and in the frozen bridge (an executable built with Control Flow
Guard) the x64 emulator kills the process with 0xC0000409
(FAST_FAIL_FATAL_APP_EXIT) on the first call into the DLL. Reproduced on
GitHub's windows-11-arm runner, where ``GODEBUG=asyncpreemptoff=1`` lets the
bridge open its port (reported 2026-11-24).
"""
import os
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


ASYNC_PREEMPT_OFF = "asyncpreemptoff=1"


def without_async_preemption(godebug: str) -> str:
    """``GODEBUG`` with Go's asynchronous preemption off, keeping the other settings
    and any explicit ``asyncpreemptoff`` already there."""
    settings = [item for item in (godebug or "").split(",") if item.strip()]
    if any(item.strip().startswith("asyncpreemptoff=") for item in settings):
        return ",".join(settings)
    return ",".join(settings + [ASYNC_PREEMPT_OFF])


def prepare(platform_name=None, platform_module=platform, platform_of=sys, environ=None) -> bool:
    """Pin the machine and, for a process running emulated, turn Go's asynchronous
    preemption off before the DLL loads (Go reads ``GODEBUG`` at start). True when
    this process runs under emulation."""
    if not pin(platform_name, platform_module, platform_of):
        return False
    environ = os.environ if environ is None else environ
    environ["GODEBUG"] = without_async_preemption(environ.get("GODEBUG", ""))
    return True
