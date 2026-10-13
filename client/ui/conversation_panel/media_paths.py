"""Where a message's media lives on disk: cache paths, saved paths,
revealing a file in Explorer and probing a media file's duration.

Moved verbatim out of ui/conversations.py, which re-exports every name.
"""

import logging
import os
import subprocess
import wave
from app_paths import data_path


# Message types that carry a file "Save as" can actually write to disk.
# Everything else — text, stickers, locations, contacts, system events — has
# no payload to save: _resolve_media_filename() falls back to "<id>.bin" for
# those, so the save dialog used to open on a plain text message offering a
# .bin that no download could ever produce, and saving it just errored.
# The context menu was already gating on this set; the Ctrl+Shift+S
# accelerator and the toolbar button reached _on_action_save_as() without
# passing anywhere near it, which is how the two disagreed.
_SAVEABLE_MESSAGE_TYPES = frozenset({
    "documentMessage", "imageMessage", "videoMessage", "audioMessage",
})


def any_phone_media(messages) -> bool:
    """True when one of *messages* is a photo, video, audio or document.

    Deleting such a message can also remove its file from the storage of the
    phone, and WhatsApp Web shows its "Delete file from your phone" box for
    exactly these: a media message that is not a sticker (read from the code
    of its delete dialog, 2026-10)."""
    return any(
        isinstance(msg, dict) and msg.get("messageType", "") in _SAVEABLE_MESSAGE_TYPES
        for msg in messages
    )


def local_media_cache_paths(voice_dir: str, media_dir: str, msg_id: str) -> list:
    """The locally pre-cached copies a sent message can own, by message id.

    voice_messages/<id>.msv is written by the voice recorder before the send is
    even enqueued, media/<id>.wzmedia by _pre_cache_sent_media() — both under
    the local UUID first, then renamed to the real WhatsApp id so Open/Save As
    and voice playback find them instead of re-downloading a file already on
    disk (see _mark_message_sent).  Module-level so the cancelled-but-delivered
    path can reach the same two names without a panel instance.

    They are also the only two names a *received* message's media can be cached
    under (handle_audio_message writes the first, handle_media_message the
    second), so the Media tab's "baixada / nao baixada" scan asks here rather
    than keeping its own idea of where a file might be.
    """
    return [
        os.path.join(voice_dir, f"{msg_id}.msv"),
        os.path.join(media_dir, f"{msg_id}.wzmedia"),
    ]


def media_cache_id(msg_id: str) -> str:
    """The id a message's cached media file is actually named after.

    Some ids arrive in WhatsApp's composite `false_<jid>_<id>` form, and the
    file on disk is named after the last component only. Playback, Save As and
    the download button all reduced it with this same three-line rule, each
    keeping its own copy.
    """
    if "_" in msg_id:
        parts = msg_id.split("_")
        return parts[2] if len(parts) > 2 else parts[-1]
    return msg_id


def cached_media_path(msg_type: str, msg_id: str) -> str:
    """The one file this message's media is cached at, if it is cached at all.

    **Voice notes and audio files do not live where the other media do.**
    `handle_audio_message()` writes `voice_messages/<id>.msv`;
    `handle_media_message()` writes `media/<id>.wzmedia`. Anything that answers
    "where is this message's file" by hardcoding the second one is wrong for
    every audio message — and wrong in the worst way, because the file IS on
    disk: the caller concludes it is missing, downloads it (into the .msv path
    it is not looking at), re-checks the .wzmedia path, finds nothing, and
    tells the user the media could not be downloaded and the link may have
    expired. Reported against Ctrl+C on a voice message that played perfectly
    a second earlier.

    This is the same rule `local_media_cache_paths()` states for the Media
    tab's downloaded/not-downloaded scan, in the form a caller wanting ONE
    path needs. CLAUDE.md's warning applies to both: a second copy of this
    answer is how one part of the app starts disagreeing with whatever wrote
    the file.
    """
    cache_id = media_cache_id(msg_id)
    if msg_type == "audioMessage":
        return data_path("voice_messages", f"{cache_id}.msv")
    return data_path("media", f"{cache_id}.wzmedia")


def saved_media_path(msg: dict) -> str:
    """Return the existing user-visible copy created through Save As.

    WinZapp's encrypted .wzmedia/.msv cache is an implementation detail, not a
    file users asked to reveal.  Issue #94's three entry points therefore stay
    unavailable until Save As has completed successfully, and become
    unavailable again if that chosen copy is deleted or moved.
    """
    if not isinstance(msg, dict) or msg.get("messageType", "") not in _SAVEABLE_MESSAGE_TYPES:
        return ""
    saved = msg.get("_saved_media_path")
    return os.path.abspath(saved) if saved and os.path.isfile(saved) else ""


def reveal_file_in_folder(filepath: str) -> bool:
    """Open File Explorer with *filepath* selected, if it still exists."""
    if not filepath or not os.path.isfile(filepath):
        return False
    path = os.path.abspath(filepath)
    subprocess.Popen(["explorer.exe", "/select,", path])
    return True


def promote_local_media_cache(voice_dir: str, media_dir: str,
                              local_id: str, real_id: str) -> None:
    """Rename a message's cached copies from its local UUID to its real id."""
    if not local_id or not real_id or local_id == real_id:
        return
    old_paths = local_media_cache_paths(voice_dir, media_dir, local_id)
    new_paths = local_media_cache_paths(voice_dir, media_dir, real_id)
    for old, new in zip(old_paths, new_paths):
        try:
            if os.path.isfile(old) and not os.path.isfile(new):
                os.rename(old, new)
        except Exception as exc:
            logging.warning("[conversations] could not promote %s: %s", old, exc)


def discard_local_media_cache(voice_dir: str, media_dir: str, local_id: str) -> None:
    """Delete a message's cached copies — the message itself is gone for good.

    Without this a cancelled-then-revoked voice message leaves its .msv behind
    under a local UUID nothing will ever look up again.
    """
    if not local_id:
        return
    for path in local_media_cache_paths(voice_dir, media_dir, local_id):
        try:
            if os.path.isfile(path):
                os.unlink(path)
        except Exception as exc:
            logging.warning("[conversations] could not discard %s: %s", path, exc)


def probe_media_duration(path: str):
    """Best-effort length in whole seconds of a media file, or None if unknown.

    Supports .mp3, .ogg, .wav, .m4a, .flac, .opus, .aac etc. — and .mp4, since
    BASS opens the container directly through the bass_aac plugin the app
    already loads at startup (that is how core/video_player.py plays a video's
    audio track), which is what lets a video with no stated duration be
    measured from the file. Uses sound_lib / BASS when available, or stdlib
    wave and header fallback parsers.

    A module-level function rather than only a ConversationsPanel method
    because MainWindow probes downloaded video from the media-download path,
    where there is no panel in reach.
    """
    if not path or not os.path.isfile(path):
        return None

    # 1. Try BASS / sound_lib stream length (supports all audio formats: mp3, ogg, wav, m4a, flac, opus, aac)
    #    A file that reads as shorter than a second returns 0, not None: the
    #    caller has to tell "measured, and it really is that short" apart from
    #    "could not measure" — see core.utils.video_seconds(). A length of
    #    exactly 0.0 is the second case (nothing decodable), so it falls
    #    through to the parsers below.
    #    decode=True matters, not just cosmetics: it's the exact stream mode
    #    every actual playback path in the app already opens the file with
    #    (VideoPlayer._start_audio, ConversationsPanel._play_audio's
    #    _open_stream) — BASS can report a slightly different get_length()
    #    for a plain (decode=False) stream vs. a decoded one on the same AAC/
    #    MP4 file, which is why a probed duration used to drift a second or
    #    two from what the player itself later showed for the same file.
    try:
        from sound_lib import stream
        s = stream.FileStream(file=path, decode=True)
        length_bytes = s.get_length()
        length_secs = s.bytes_to_seconds(length_bytes)
        s.free()
        if length_secs and 0 < length_secs < 86400:
            return int(length_secs)
    except Exception:
        pass

    # 2. Try stdlib wave module for .wav files
    if path.lower().endswith(".wav"):
        try:
            import wave
            with wave.open(path, "rb") as wf:
                frames = wf.getnframes()
                rate   = wf.getframerate()
                if frames > 0 and rate > 0:
                    sec = int(frames / rate)
                    if sec < 86400:
                        return sec
        except Exception:
            pass

    # 3. Fallback lightweight header parser for MP3 / OGG
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".mp3":
            size = os.path.getsize(path)
            if size > 0:
                # Estimate based on standard 128kbps (16000 bytes/sec)
                sec = max(1, int(size / 16000))
                if 0 < sec < 86400:
                    return sec
        elif ext in (".ogg", ".opus"):
            size = os.path.getsize(path)
            if size > 0:
                sec = max(1, int(size / 6000))
                if 0 < sec < 86400:
                    return sec
    except Exception:
        pass

    return None
