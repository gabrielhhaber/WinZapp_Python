"""The machine's own architecture, not the process's.

WinZapp is a 64-bit x86 program; on a Windows ARM64 machine it runs under
emulation, where Python's platform.machine() answers "AMD64" like anywhere
else. whisper.cpp runs as a program of its own (whisper-cli, whisper-server),
so on ARM64 it can be the native ARM64 build: faster than an emulated x64
one, and the only one with an NVIDIA CUDA build there (CUDA 13,
whisper_cpp_builds). IsWow64Process2 is what tells the machine apart.
"""

import functools
import sys

#: IMAGE_FILE_MACHINE_ARM64
_ARM64 = 0xAA64


def is_arm64(native_machine) -> bool:
    return native_machine == _ARM64


@functools.lru_cache(maxsize=1)
def native_is_arm64() -> bool:
    """Whether this Windows machine is ARM64 (False anywhere else, and when
    Windows cannot say: an x64 build then runs as it always did)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = getattr(kernel32, "IsWow64Process2", None)
        if fn is None:          # before Windows 10 1709: no ARM64 Windows either
            return False
        fn.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT),
                       ctypes.POINTER(wintypes.USHORT))
        fn.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        process, native = wintypes.USHORT(), wintypes.USHORT()
        if not fn(kernel32.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
            return False
        return is_arm64(native.value)
    except Exception:
        return False
