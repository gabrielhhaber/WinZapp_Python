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
from typing import Optional

#: The pack's own sounds that come from WhatsApp, besides alerts/.
WHATSAPP_SOUNDS = (
    "audio_transition_end.ogg",
    "audio_transition_next.ogg",
    "call_ended.ogg",
    "call_incoming.ogg",
    "message_background.ogg",
    "message_current.ogg",
    "message_foreground.ogg",
    "message_sent.ogg",
    "voicemsg_discard.ogg",
    "voicemsg_pauserecording.ogg",
    "voicemsg_send.ogg",
    "voicemsg_startrecording.ogg",
)
WHATSAPP_SOUND_FOLDERS = ("alerts",)
#: In the install folder; holds "whatsapp" when only WhatsApp's sounds were installed.
INSTALLED_SOUNDS_MARKER = "installed_sounds.txt"
#: The two installation-wide choices (what the marker file says).
MODE_ALL = "all"
MODE_WHATSAPP = "whatsapp"
#: What the marker holds when the choice is made again to be the whole pack.
_MARKER_ALL = "full"
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


def current_mode(install_dir: str) -> str:
    """MODE_WHATSAPP or MODE_ALL for the install in *install_dir*."""
    return MODE_WHATSAPP if keeps_only_whatsapp(install_dir) else MODE_ALL


# The choice made in the update dialog (Additional settings). It lives only in
# memory: it applies to the install that dialog started, from any of its paths
# (now, in the background, on exit), and a restart before that install simply
# keeps the current selection.
_pending_mode: Optional[str] = None


def set_pending_mode(mode: Optional[str]) -> None:
    """Remember the update's chosen mode; None (or anything else) clears it."""
    global _pending_mode
    _pending_mode = mode if mode in (MODE_ALL, MODE_WHATSAPP) else None


def pending_mode() -> Optional[str]:
    return _pending_mode


def extras_to_remove(sounds_dir: str) -> list:
    """Paths of every sound in *sounds_dir*/default that WhatsApp's set does
    not keep, without deleting them."""
    root = os.path.join(sounds_dir, "default")
    found = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            if not is_whatsapp_original(os.path.relpath(path, root)):
                found.append(path)
    return found


def stage_selection(source_dir: str, install_dir: str, mode: Optional[str] = None) -> list:
    """Make the release in *source_dir* install *mode* (default: the pending
    choice, else the installed one). Returns the installed sounds the copy
    must delete, which is non-empty only when switching to WhatsApp's own.

    Switching to WhatsApp's own prunes the new files, writes the marker into
    the release (the copy carries it over) and lists the installed extras.
    Switching to all lets the release bring every sound back, and its marker
    replaces the old one. Never raises: a failure keeps the current sounds."""
    chosen = mode or _pending_mode or current_mode(install_dir)
    installed = current_mode(install_dir)
    try:
        if chosen == MODE_WHATSAPP:
            prune_to_whatsapp(os.path.join(source_dir, "sounds"))
            if installed != MODE_WHATSAPP:
                _write_marker(source_dir, MODE_WHATSAPP)
                return extras_to_remove(os.path.join(install_dir, "sounds"))
        elif installed == MODE_WHATSAPP:
            _write_marker(source_dir, _MARKER_ALL)
    except OSError as exc:
        logging.warning("[update] could not stage the sound selection: %s", type(exc).__name__)
    return []


def _write_marker(directory: str, mode: str) -> None:
    value = MODE_WHATSAPP if mode == MODE_WHATSAPP else _MARKER_ALL
    with open(os.path.join(directory, INSTALLED_SOUNDS_MARKER), "w", encoding="utf-8") as fh:
        fh.write(value)


def plain_label(text: str) -> str:
    """*text* without wx mnemonics (``&x`` -> ``x``, ``&&`` -> ``&``), for a
    label that is only read: a mnemonic there would take an Alt letter."""
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")
