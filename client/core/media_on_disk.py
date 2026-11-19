"""Whether a message's media is already stored, as the downloads store it.

handle_media_message() writes data/media/<id>.wzmedia and handle_audio_message()
data/voice_messages/<id>.msv, the id being the key's id or, for a serialized
one, its third part. The media sweep (sync_media_for_all_chats) counted every
candidate, the ones already on disk included, and said "Baixando mídias: 723 de
728 processadas (66 baixadas)" when 659 of them had been downloaded in earlier
sessions and the 69 still missing had all come down (a user's log, 2026-11-09:
69 requests to WPPConnect, all answered, 45 files in media/ and 24 in
voice_messages/ in that minute). Leaving the stored ones out of the count makes
it "66 de 69": what is left to download.

Tested in test_media_on_disk.py.
"""
import os

from app_paths import data_path


def stored_id(raw_id) -> str:
    """The id a downloaded file is named after."""
    raw_id = str(raw_id or "")
    if "_" in raw_id:
        parts = raw_id.split("_")
        return parts[2] if len(parts) > 2 else parts[-1]
    return raw_id


def stored_path(msg, message_type, path=data_path) -> str:
    """Where the download of *msg* (of *message_type*) is written, "" without an id."""
    media_id = stored_id((msg.get("key") or {}).get("id"))
    if not media_id:
        return ""
    if message_type == "audioMessage":
        return path("voice_messages", f"{media_id}.msv")
    return path("media", f"{media_id}.wzmedia")


def already_stored(msg, message_type, path=data_path, isfile=os.path.isfile) -> bool:
    """False too when the data folder cannot be resolved (no active account):
    the download checks the file again before fetching."""
    try:
        file = stored_path(msg, message_type, path)
    except RuntimeError:
        return False
    return bool(file) and isfile(file)
