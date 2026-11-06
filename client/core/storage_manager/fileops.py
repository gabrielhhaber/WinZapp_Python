"""Windows operations on a pinned file identity, not a re-resolved path.

API contracts: Microsoft Learn, CreateFileW, SetFileInformationByHandle and
FILE_RENAME_INFO. Native APIs are loaded only for an explicit file operation.
No content is read. No fallback to a path-based delete or copy is permitted.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os
import stat

from .types import canonical, fingerprint, inside, unsafe


class ChangedFile(OSError):
    pass


@contextmanager
def pinned_directory(path):
    """Hold ancestry and this directory while a transaction creates journals."""
    if os.name != "nt":
        raise NotImplementedError("storage file actions require the Windows provider")
    kernel = _kernel()
    with ExitStack() as stack:
        _pin_parents(stack, kernel, path)
        stack.enter_context(_opened(kernel, path, directory=True))
        yield


def _kernel():
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.CloseHandle.argtypes, k.CloseHandle.restype = [w.HANDLE], w.BOOL
    k.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
    k.SetFileInformationByHandle.restype = w.BOOL
    k.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    k.GetFinalPathNameByHandleW.restype = w.DWORD
    k.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
    k.GetFileInformationByHandleEx.restype = w.BOOL
    return k


def _final_path(kernel, handle):
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    count = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not count or count >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return canonical(value)


@contextmanager
def _opened(kernel, path, *, directory=False):
    import ctypes
    import msvcrt
    # No DELETE sharing pins the directory/path. A file also denies writers;
    # an existing writer makes CreateFile fail rather than racing a cleanup.
    access = 0x80 if directory else 0x80 | 0x10000
    sharing = 1 | 2 if directory else 1
    flags = 0x00200000 | (0x02000000 if directory else 0)
    handle = kernel.CreateFileW(path, access, sharing, None, 3, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        info = os.fstat(descriptor)
        attributes = (ctypes.c_ulong * 2)()
        if not kernel.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        from .types import BLOCKED_ATTRIBUTES
        if attributes[0] & BLOCKED_ATTRIBUTES or unsafe(info) or _final_path(kernel, handle) != canonical(path):
            raise ChangedFile("path changed or is a reparse point")
        if directory != stat.S_ISDIR(info.st_mode):
            raise ChangedFile("wrong file kind")
        yield handle, info
    finally:
        if descriptor is None:
            kernel.CloseHandle(handle)
        else:
            os.close(descriptor)  # owns the native handle after open_osfhandle


def _pin_parents(stack, kernel, path):
    parts, current = [], canonical(os.path.dirname(path))
    while True:
        parts.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for parent in reversed(parts):
        stack.enter_context(_opened(kernel, parent, directory=True))


class PinnedFile:
    def __init__(self, kernel, handle, info):
        self.kernel, self.handle, self.info = kernel, handle, info

    def remove(self):
        import ctypes
        value = ctypes.c_ubyte(1)
        if not self.kernel.SetFileInformationByHandle(self.handle, 4, ctypes.byref(value), 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def move(self, destination):
        import ctypes
        from ctypes import wintypes as w

        class Rename(ctypes.Structure):
            _fields_ = [("Flags", w.DWORD), ("RootDirectory", w.HANDLE),
                        ("FileNameLength", w.DWORD), ("FileName", w.WCHAR * 1)]

        encoded = os.path.abspath(destination).encode("utf-16-le")
        offset = Rename.FileName.offset
        buffer = ctypes.create_string_buffer(max(ctypes.sizeof(Rename), offset + len(encoded) + 2))
        value = Rename.from_buffer(buffer)
        value.Flags = 0  # Do not replace a file created after the preview.
        value.RootDirectory = None
        value.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
        with ExitStack() as stack:
            _pin_parents(stack, self.kernel, destination)
            if not self.kernel.SetFileInformationByHandle(self.handle, 3, buffer, len(buffer)):
                raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def pinned(entry):
    """Only expose handle operations after scope, ancestry and identity checks."""
    if len(entry.identity) != 5 or not entry.identity[1]:
        raise ChangedFile("stable file identity unavailable")
    if os.name != "nt":
        raise NotImplementedError("storage file actions require the Windows provider")
    if not inside(entry.path, entry.root.path) or canonical(entry.path) == canonical(entry.root.path):
        raise ChangedFile("entry leaves its root")
    kernel = _kernel()
    with ExitStack() as stack:
        _pin_parents(stack, kernel, entry.path)
        handle, info = stack.enter_context(_opened(kernel, entry.path))
        if fingerprint(info) != entry.identity:
            raise ChangedFile("preview identity changed")
        yield PinnedFile(kernel, handle, info)
