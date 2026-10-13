"""Local transcription of status updates: what differs from a message's.

An audio or video status is transcribed through the very same run as a voice
message (message_run.MessageTranscription); this module holds the few rules
that are the status tab's own.

* **Where the text lives.** Under the same top-level ``_transcription`` key
  (stored.TRANSCRIPTION_KEY), in the status's own record, which is what the
  ``status_updates`` table stores encrypted. Nothing else is needed for it to
  go away with the status: the periodic prune of expired statuses deletes the
  row, and the text with it.
* **Its own switches**, mirroring the conversation's: transcribe on arrival
  (``transcription.auto_transcribe_statuses``, off), keep the result
  (``transcription.status_cache_enabled``, on) and Enter/Space transcribe
  (``user_interface.enter_transcribes_statuses``, off). A model's own cache
  choice (``cache_by_model``) is the same one the conversation reads: it is
  about the model, not about where the audio came from.
* **The media is never on disk.** A status is downloaded when it is opened and
  kept in memory, so a run gets its own private folder (TEMP_PREFIX, swept at
  startup like the other transcription temporaries) holding the Fernet-
  encrypted download for as long as the run lasts.
* **Two kinds of copy.** MainWindow._status_updates holds the cached records;
  the Status tab shows its own list, built from the API's answer first, so the
  record on screen is often not the cached one. Storing writes every copy, and
  carry_over() puts the text back on the list each time it is rebuilt.

No wx here.
"""

from core.transcription import auto_queue, message_audio, stored
from core.transcription.preferences import (
    SECTION,
    SETTING_AUTO_TRANSCRIBE_STATUSES,
    SETTING_STATUS_CACHE_ENABLED,
)

STATUS_JID = "status@broadcast"

UI_SECTION = "user_interface"
UI_SETTING_ENTER_TRANSCRIBES = "enter_transcribes_statuses"

#: The folder of one run's download. Listed in temp_sweep.PREFIXES.
TEMP_PREFIX = "winzapp-status-"

KEY = stored.TRANSCRIPTION_KEY


def _section(settings, name) -> dict:
    section = settings.get(name) if isinstance(settings, dict) else None
    return section if isinstance(section, dict) else {}


def auto_enabled(settings) -> bool:
    """Whether statuses are transcribed as they arrive. Only a real True."""
    return _section(settings, SECTION).get(SETTING_AUTO_TRANSCRIBE_STATUSES) is True


def enter_transcribes(settings) -> bool:
    """Whether Enter and Space transcribe an audio or video status into its
    line (Alt+Shift+N then opens or plays it) instead of opening it."""
    return _section(settings, UI_SECTION).get(UI_SETTING_ENTER_TRANSCRIBES) is True


def cache_enabled_for(settings, model_id) -> bool:
    """Whether a status result of this model is kept with the status.

    The model's own choice when the user made one (the same per-model map the
    conversation reads), else the statuses' general switch, on by default.
    Tolerant like stored.cache_enabled_for(): only a real False turns it off.
    """
    section = _section(settings, SECTION)
    own = section.get(stored.CACHE_BY_MODEL_KEY)
    if model_id and isinstance(own, dict) and isinstance(own.get(model_id), bool):
        return own[model_id]
    return section.get(SETTING_STATUS_CACHE_ENABLED) is not False


def may_cache(settings, value) -> bool:
    """cache_enabled_for() for the newest result `value` holds; a value that
    names no model (a tombstone) is not a result and is always allowed."""
    if not isinstance(value, dict) or not value.get("model_id"):
        return True
    return cache_enabled_for(settings, value.get("model_id"))


def status_id(status) -> str:
    key = status.get("key") if isinstance(status, dict) else None
    return (key or {}).get("id", "") or ""


def participant_of(status) -> str:
    """Who posted it, as the record says: the bucket it is stored under."""
    if not isinstance(status, dict):
        return ""
    key = status.get("key") or {}
    return key.get("participant") or status.get("participant") or ""


def is_transcribable(status) -> bool:
    """An audio or video status (never a GIF), by the conversation's rule."""
    return isinstance(status, dict) and message_audio.is_transcribable(status)


def auto_eligible(status, now=None) -> bool:
    """Whether the automatic setting should transcribe this status: someone
    else's, audio or video, just arrived, not too long, nothing decided yet."""
    return auto_queue.eligible(status, now)


def records_with_id(collections, msg_id) -> list:
    """Every dict carrying this status id among lists of status dicts."""
    found = []
    if not msg_id:
        return found
    for records in collections:
        for record in records or ():
            if isinstance(record, dict) and status_id(record) == msg_id:
                found.append(record)
    return found


def set_on(copies, value) -> int:
    """Put `value` on every copy; how many took it."""
    count = 0
    for copy in copies:
        if isinstance(copy, dict):
            copy[KEY] = value
            count += 1
    return count


def carry_over(shown, cached) -> int:
    """Give the statuses about to be shown the decision the cache holds.

    `shown` is the list the tab builds, with the API's copy of a status ahead
    of the cached one; `cached` is every record of MainWindow._status_updates.
    The later decision wins, as everywhere a transcription meets a stale copy
    (stored.newer_decision()). Returns how many copies changed.
    """
    by_id = {}
    for record in cached or ():
        if isinstance(record, dict) and KEY in record:
            by_id.setdefault(status_id(record), record[KEY])
    changed = 0
    for status in shown or ():
        if not isinstance(status, dict):
            continue
        known = by_id.get(status_id(status))
        if known is None:
            continue
        winner = stored.newer_decision(known, status.get(KEY))
        if winner is not status.get(KEY):
            status[KEY] = winner
            changed += 1
    return changed


def holds_text(status) -> bool:
    """Whether a saved transcription (of any model) is on this status."""
    value = status.get(KEY) if isinstance(status, dict) else None
    return bool(stored.results_of(value))


def inline_text(status, settings) -> str:
    """The text the status's line of the list carries, "" when none is wanted
    there (only an inline record, of the model selected now)."""
    if not isinstance(status, dict):
        return ""
    return stored.inline_row_text(status, stored.selection_from_settings(settings))
