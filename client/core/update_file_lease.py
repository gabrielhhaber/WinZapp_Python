"""Lifetime locks for staged update packages and their installer guardian.

The Windows reader denies writes/deletion but permits a second reader, so
the installer can take over before the application releases its handle.
Probes never create a file and treat an unreadable lock conservatively.
"""
import errno
import os
import threading

_packages = {}
_lock = threading.RLock()


def _open_windows(path, shared):
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    access, share, disposition = (0x80000000, 1, 4) if shared else (0xC0000000, 0, 3)
    handle = k.CreateFileW(path, access, share, None, disposition, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return lambda: k.CloseHandle(handle)


def _open_posix(path, shared):
    import fcntl
    stream = open(path, "a+b" if shared else "r+b")
    try:
        fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BaseException:
        stream.close()
        raise
    return stream.close


def open_lease(path, shared=True):
    return (_open_windows if os.name == "nt" else _open_posix)(path, shared)


def lease_busy(path):
    """Whether a lifetime owner exists or its absence cannot be proved."""
    try:
        close = open_lease(path, shared=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if getattr(exc, "winerror", None) in (2, 3) or exc.errno == errno.ENOENT:
            return False
        return True
    close()
    return False


def package_lock_path(root):
    root = os.path.normpath(root)
    if not os.path.basename(root).startswith("winzapp_ext_"):
        raise ValueError("not an update package root")
    return root + ".lease"


def hold_package(root):
    path = package_lock_path(root)
    with _lock:
        if path not in _packages:
            _packages[path] = open_lease(path)


def release_package(root):
    path = package_lock_path(root)
    with _lock:
        close = _packages.pop(path, None)
        if close:
            close()
    try:
        os.remove(path)
    except OSError:
        pass  # The guardian may still own its shared reader.


def package_busy(root):
    path = package_lock_path(root)
    with _lock:
        if path in _packages:
            return True
    return lease_busy(path)
