"""Load speech drivers with a process-owned cache for generated COM wrappers."""

import sys
import tempfile


def _prepare_com_cache():
    if sys.platform != "win32":
        return None

    import win32com

    # gencache rebuilds a missing index during win32com.client's import.
    # Existing wrappers then import helpers from that still-incomplete module.
    # Start empty instead, before accessible_output2 imports libloader.com.
    # Each account/process owns its directory: neither stale shared wrappers
    # nor another program's cache cleanup can affect our speech drivers.
    cache = tempfile.TemporaryDirectory(prefix="winzapp-com-")
    win32com.__gen_path__ = cache.name
    win32com.gen_py.__path__ = [cache.name]
    return cache


# Keep the directory alive until interpreter shutdown, including while speech
# objects still need their generated wrappers. No COM objects are created here.
_com_cache = _prepare_com_cache()

from accessible_output2 import outputs  # noqa: E402
