"""Stage a decrypted media file under its real name for an external viewer.

The "Open" button hands a file to the program Windows associates with it, and
that program shows the file's name in its title bar and recent-files list. A
``tempfile.NamedTemporaryFile`` name (``tmpab12cd34.pdf``) told the user
nothing, so the plaintext goes into a private folder of its own, under the
name Save As would propose. The folder keeps two opens of same-named files
from colliding and is swept by ``core.transcription.temp_sweep`` (PREFIX).
"""

from __future__ import annotations

import os
import re
import tempfile

#: Prefix of the per-open folder; listed in temp_sweep.PREFIXES.
PREFIX = "winzapp-open-"

_MAX_NAME = 150
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_file_name(name: str, fallback: str = "file") -> str:
    """`name` made valid as a Windows file name, keeping its extension."""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = _INVALID.sub("_", name).strip(" .")
    root, ext = os.path.splitext(name)
    if len(name) > _MAX_NAME:
        ext = ext[:20]
        root = root[: _MAX_NAME - len(ext)]
        name = root + ext
    if not root:
        return fallback
    if root.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name


def write_named_temp_file(content: bytes, display_name: str, fallback: str = "file") -> str:
    """Write `content` to a new private folder as `display_name`; return the path."""
    folder = tempfile.mkdtemp(prefix=PREFIX)
    path = os.path.join(folder, safe_file_name(display_name, fallback))
    with open(path, "wb") as fh:
        fh.write(content)
    return path
