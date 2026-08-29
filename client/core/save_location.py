"""Which folder a "Salvar como..." dialog opens on.

This is only about the folder the Explorer dialog *starts* in. Nothing here
saves anything without asking — every caller still shows the dialog and still
lets the user go wherever they want.

The behaviour was argued over repeatedly without landing anywhere, because both
answers are right for different people: someone filing a run of documents into
one project folder wants the dialog to stay where they left it, and someone who
saves one attachment a week wants Downloads every time. So it became a setting
(Configurações > Arquivos e salvamento) instead of a decision.

The default is REMEMBER_LAST, which is a change: every save dialog used to open
on Downloads unconditionally. Remembering is the behaviour that degrades
gracefully — the first save of a fresh install has no remembered folder and
falls back to Downloads, so a user who never opens the settings gets the old
behaviour once and a useful one afterwards.

Kept as plain functions over a settings dict, with no wx and no MainWindow, so
the resolution order is testable on its own — the four call sites that use it
(save one attachment, save a selection in bulk, save from the media viewer,
save a status) live in three different files.
"""

import logging
import os

from core.utils import get_downloads_folder

# Settings section and keys. One section rather than loose keys under
# "general": the tab exists as a home for file/saving behaviour generally, and
# a section makes it obvious what belongs there.
SECTION = "files"
MODE_KEY = "save_dialog_folder_mode"
CUSTOM_KEY = "save_dialog_custom_folder"
LAST_KEY = "save_dialog_last_folder"

MODE_REMEMBER_LAST = "last"
MODE_DOWNLOADS = "downloads"
MODE_CUSTOM = "custom"

# Order matters: it is the order of the radio buttons in the settings tab, and
# the index stored/read there is an index into this tuple. Appending is safe;
# reordering silently changes what an existing install means.
MODES = (MODE_REMEMBER_LAST, MODE_DOWNLOADS, MODE_CUSTOM)

DEFAULT_MODE = MODE_REMEMBER_LAST


def _section(settings) -> dict:
    if not isinstance(settings, dict):
        return {}
    section = settings.get(SECTION)
    return section if isinstance(section, dict) else {}


def mode_index(mode: str) -> int:
    """Radio-button index for a stored mode, tolerating an unknown value."""
    try:
        return MODES.index(mode)
    except ValueError:
        return MODES.index(DEFAULT_MODE)


def mode_from_index(index) -> str:
    """Stored mode for a radio-button index, tolerating an out-of-range one.

    The bounds check is explicit rather than left to IndexError, because
    Python's negative indexing would otherwise turn -1 into MODES[-1] — the
    LAST mode, "custom folder". And -1 is not a hypothetical: it is
    wx.NOT_FOUND, exactly what RadioBox.GetSelection() returns when nothing is
    selected. Silently reading that as "save everything to the custom folder"
    is the worst available answer.

    isinstance rather than int(), for the same reason in the other direction: a
    float would be truncated into a neighbouring valid mode instead of being
    recognised as nonsense.
    """
    if isinstance(index, bool) or not isinstance(index, int):
        return DEFAULT_MODE
    if 0 <= index < len(MODES):
        return MODES[index]
    return DEFAULT_MODE


def _usable_dir(path) -> str:
    """A path only counts if it is still a directory we can point a dialog at.

    Every remembered or configured folder is one the user could have deleted,
    renamed, or unplugged since — a removable drive is the ordinary case. wx
    handles a missing defaultDir by falling back to somewhere of its own
    choosing, which is worse than choosing deliberately, so each candidate is
    checked before it is offered.
    """
    if not path or not isinstance(path, str):
        return ""
    try:
        return path if os.path.isdir(path) else ""
    except (OSError, ValueError):
        return ""


def resolve_save_dialog_folder(settings) -> str:
    """Folder a Save As dialog should open on. Never empty.

    Falls back to Downloads whenever the configured answer cannot be used:
    custom mode with a folder that no longer exists, remember mode before the
    first save, a settings file with a mode nobody recognises. Downloads itself
    is resolved through get_downloads_folder(), which reads the real Windows
    Downloads location rather than assuming ~/Downloads.
    """
    section = _section(settings)
    mode = section.get(MODE_KEY, DEFAULT_MODE)

    if mode == MODE_CUSTOM:
        chosen = _usable_dir(section.get(CUSTOM_KEY))
        if chosen:
            return chosen
        logging.info(
            "[save_location] custom folder %r is not usable — falling back to "
            "Downloads.", section.get(CUSTOM_KEY),
        )
    elif mode != MODE_DOWNLOADS:
        # REMEMBER_LAST, and anything unrecognised, which is treated as the
        # default rather than as an error: a settings file from a newer build
        # must not leave the dialog with nowhere to open.
        remembered = _usable_dir(section.get(LAST_KEY))
        if remembered:
            return remembered

    return get_downloads_folder()


def remember_save_dialog_folder(settings, saved_path: str) -> bool:
    """Record the folder a file was just saved into. True when it changed.

    Takes the file path the dialog returned, not a directory, because that is
    what every call site has. Recorded regardless of the active mode: the user
    can switch to "última pasta" later and should not find it empty, and the
    value is ignored by the other two modes anyway.

    Returns whether anything changed so callers can skip a settings write —
    saving several files into the same folder is the common case.
    """
    if not isinstance(settings, dict) or not saved_path:
        return False
    try:
        folder = os.path.dirname(os.path.abspath(saved_path))
    except (OSError, ValueError):
        return False
    if not folder:
        return False

    section = settings.get(SECTION)
    if not isinstance(section, dict):
        section = {}
        settings[SECTION] = section
    if section.get(LAST_KEY) == folder:
        return False
    section[LAST_KEY] = folder
    return True
