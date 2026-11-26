"""Optional, pinned NetEq DLL; never search PATH or load a path from HTTP.

Two fixed places, in order: the copy downloaded from Settings > Calls
(core/call_neteq_component.py), loaded only when its SHA-256 is the pinned
one, then the copy inside the Neonize bridge's folder (installer and dev
builds). Either one still has to answer the expected ABI.
"""
import ctypes
import logging
from pathlib import Path

import numpy as np

RATES = (8000, 16000, 32000, 48000)
# The C ABI these bindings speak (winzapp_neteq_abi() in native/call_neteq).
ABI = 1


def bundled_paths():
    """Where the bridge's own build puts the DLL."""
    from core.neonize_bridge_process import bridge_dir
    root = Path(bridge_dir())
    return (root / '_internal/neonize/winzapp_call_neteq.dll',
            root / '.venv/Lib/site-packages/neonize/winzapp_call_neteq.dll')


def bundled_dll():
    """The bridge-folder DLL, or None."""
    return next((path for path in bundled_paths() if path.is_file()), None)


def candidate_paths(downloaded, bundled):
    """The paths to try, the verified download first. `downloaded` is what
    call_neteq_component.verified_dll() answered (None: absent or not the
    pinned file, never loaded)."""
    return ([Path(downloaded)] if downloaded else []) + [p for p in bundled if p.is_file()]


def load_library():
    from core import call_neteq_component
    try:
        downloaded = call_neteq_component.verified_dll()
    except Exception as exc:
        logging.warning('[call_audio] could not check the downloaded NetEq component: %s',
                        type(exc).__name__)
        downloaded = None
    for path in candidate_paths(downloaded, bundled_paths()):
        try:
            return NetEqLibrary(path.resolve())
        except (OSError, AttributeError, ValueError) as exc:
            # A copy with another ABI or that will not load: try the next one,
            # and at the end calls keep the compatible playback.
            logging.warning('[call_audio] NetEq component %s not loaded: %s',
                            path.name, type(exc).__name__)
    return None


class NetEqLibrary:
    def __init__(self, path):
        lib = self.lib = ctypes.CDLL(str(path), winmode=0x1100)
        lib.winzapp_neteq_abi.restype = ctypes.c_uint32
        if lib.winzapp_neteq_abi() != ABI:
            raise ValueError('unsupported NetEq ABI')
        lib.winzapp_neteq_create.argtypes = [ctypes.c_uint32]
        lib.winzapp_neteq_create.restype = ctypes.c_void_p
        lib.winzapp_neteq_insert.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        lib.winzapp_neteq_insert.restype = ctypes.c_int
        lib.winzapp_neteq_render.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        lib.winzapp_neteq_render.restype = ctypes.c_int
        lib.winzapp_neteq_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)]
        lib.winzapp_neteq_stats.restype = ctypes.c_int
        lib.winzapp_neteq_destroy.argtypes = [ctypes.c_void_p]
        lib.winzapp_neteq_destroy.restype = None

    def create(self, rate):
        return NativePlayout(self.lib, rate)


class NativePlayout:
    """Caller serializes insert/render/close with the stream ownership lock."""
    def __init__(self, lib, rate):
        self.lib = lib
        self.handle = lib.winzapp_neteq_create(rate)
        if not self.handle:
            raise ValueError('NetEq initialization failed')

    def insert(self, timestamp, samples):
        pcm = np.ascontiguousarray(samples, dtype=np.float32)
        if self.lib.winzapp_neteq_insert(self.handle, timestamp, pcm.ctypes.data, pcm.size) != 0:
            raise ValueError('NetEq packet rejected')

    def render(self, out):
        if out.dtype != np.float32 or out.ndim != 1:
            return False
        if out.size == 0:
            return True
        if out.strides[0] <= 0:
            return False
        return self.lib.winzapp_neteq_render(self.handle, out.ctypes.data, out.size,
                                            out.strides[0] // out.itemsize) == 0

    def stats(self):
        values = (ctypes.c_uint64 * 4)()
        if self.lib.winzapp_neteq_stats(self.handle, values) != 0:
            return ()
        return tuple(values)

    def close(self):
        if self.handle:
            self.lib.winzapp_neteq_destroy(self.handle)
            self.handle = None
