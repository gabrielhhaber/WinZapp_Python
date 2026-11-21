"""Which sounds an install keeps: the whole pack, or only WhatsApp's own.

The installer (installer/installer.c) offers "only the original WhatsApp
sounds" (issue #769): the files below and everything in alerts/. It then
deletes the other sounds it extracted and leaves INSTALLED_SOUNDS_MARKER in
the install folder, so that an update, which copies a whole release over the
folder, removes the same sounds from the release before copying it
(updater.py). Other files of the pack (default.pack.json, notes) stay.

tests/test_sound_selection.py holds installer.c's copy of the list equal to
this one.
"""
import logging
import os

#: The pack's own sounds that come from WhatsApp, besides alerts/.
WHATSAPP_SOUNDS = (
    "audio_transition_end.ogg",
    "audio_transition_next.ogg",
    "call_ended.ogg",
    "call_incoming.ogg",
    "message_background.ogg",
    "message_current.ogg",
)
WHATSAPP_SOUND_FOLDERS = ("alerts",)
#: In the install folder; holds "whatsapp" when only WhatsApp's sounds were installed.
INSTALLED_SOUNDS_MARKER = "installed_sounds.txt"
_AUDIO = (".ogg", ".wav", ".mp3", ".flac", ".opus", ".m4a")


def is_whatsapp_original(relative: str) -> bool:
    """Whether *relative* (a path inside sounds/default/) is one WhatsApp's
    sounds keep. Anything that is not a sound is kept too."""
    parts = [p for p in relative.replace("\\", "/").split("/") if p]
    if not parts:
        return True
    if not parts[-1].lower().endswith(_AUDIO):
        return True
    if len(parts) > 1:
        return parts[0].lower() in WHATSAPP_SOUND_FOLDERS
    return parts[0].lower() in WHATSAPP_SOUNDS


def keeps_only_whatsapp(install_dir: str) -> bool:
    """Whether the install in *install_dir* chose only WhatsApp's sounds."""
    try:
        with open(os.path.join(install_dir, INSTALLED_SOUNDS_MARKER), encoding="utf-8-sig") as fh:
            return fh.read().strip().lower() == "whatsapp"
    except OSError:
        return False


def prune_to_whatsapp(sounds_dir: str) -> int:
    """Delete from *sounds_dir*/default every sound WhatsApp's set does not
    keep; how many were deleted. A file that cannot be deleted is left (the
    update then installs it, as before)."""
    root = os.path.join(sounds_dir, "default")
    removed = 0
    for folder, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            if is_whatsapp_original(os.path.relpath(path, root)):
                continue
            try:
                os.remove(path)
                removed += 1
            except OSError as exc:
                logging.warning("[update] could not leave out an unselected sound: %s",
                                type(exc).__name__)
    return removed
