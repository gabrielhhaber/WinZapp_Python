"""Transcribing and describing received messages with an online provider, on arrival.

The online counterpart of core/transcription/auto_queue.py: no wx, no
MainWindow — only the rule for which messages qualify. The glue (queue, the
request, the store) is main_window/auto_ai_media.py.

Rules, each one a way this could otherwise surprise or harm:

* **Opt in, twice.** The two switches (`auto_transcribe` for voice messages
  and audio, `auto_describe` for pictures, stickers and videos) are off by
  default, and turning one on asks for the consent a manual action asks for
  (ui/dialogs/ai_settings_page.py). The glue then only uses providers that
  consent covers.
* **Not both transcribers.** Automatic transcription with the local models
  (`transcription.auto_transcribe_received`) and this one would produce two
  texts for the same message; the settings turn the other off when one is
  turned on, and here a message is left to the local one whenever it is on.
* **Only what just arrived**, once, incoming only, never a message that
  already carries a decision, never a view-once one, never a long recording.
"""

import time

from core.transcription import auto_queue
from core.transcription.stored import TRANSCRIPTION_KEY

from . import config as ai_config

#: The kinds this module may act on; a PDF is too long for a message's line.
AUTO_KINDS = ("audio", "image", "sticker", "video")


#: How long the automatic requests stop after every provider refused one
#: for a reason that will repeat (quota, key): one hour.
PAUSE_SECONDS = 3600.0
#: The failures that repeat for every message until the user fixes something.
PAUSING_CATEGORIES = frozenset(("quota", "credits", "authentication", "credentials"))


def day_key(now=None) -> str:
    """The local calendar day, the unit of the daily ceiling."""
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if now is None else now))


def spent_today(config, now=None) -> int:
    usage = config.get("auto_usage") or {}
    return usage.get("count", 0) if usage.get("day") == day_key(now) else 0


def limit_reached(config, now=None) -> bool:
    """Whether today's ceiling has been used up (0 = there is none)."""
    limit = config.get("auto_daily_limit", 0)
    return bool(limit) and spent_today(config, now) >= limit


def charged(raw_block, now=None):
    """`raw_block` (the stored ai_media dict) with one more request counted today.
    Written whole by the caller through AppSettings.update()."""
    value = dict(raw_block) if isinstance(raw_block, dict) else {}
    today = day_key(now)
    usage = value.get("auto_usage")
    count = usage.get("count") if isinstance(usage, dict) and usage.get("day") == today else 0
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = 0
    value["auto_usage"] = {"day": today, "count": count + 1}
    return value


def pauses(error) -> bool:
    """Whether a failed chain means "stop for a while" rather than "this message":
    every provider it tried refused for a reason that repeats."""
    attempts = getattr(error, "attempts", ())
    if attempts:
        return all(category in PAUSING_CATEGORIES for _provider, category in attempts)
    return getattr(error, "category", None) in PAUSING_CATEGORIES


def wanted_kind(msg, config, local_auto_transcription=False, now=None, max_age=None):
    """The kind to process `msg` as on arrival, or None.

    `config` is core.ai_media.config.preferences(); `local_auto_transcription`
    is whether the local automatic transcription is on (auto_queue.enabled()).
    """
    if not isinstance(msg, dict) or not config.get("enabled"):
        return None
    key = msg.get("key") or {}
    if key.get("fromMe") or not key.get("id"):
        return None
    if TRANSCRIPTION_KEY in msg:
        return None
    kind = ai_config.eligible_kind(msg)
    if kind not in AUTO_KINDS or not config["kinds"].get(kind, True):
        return None
    if kind == "audio":
        if not config.get("auto_transcribe") or local_auto_transcription:
            return None
        if auto_queue.audio_seconds(msg) > auto_queue.MAX_AUDIO_SECONDS:
            return None
    elif not config.get("auto_describe"):
        return None
    try:
        arrived = float(msg.get("messageTimestamp") or 0)
    except (TypeError, ValueError):
        arrived = 0.0
    limit = auto_queue.MAX_AGE_SECONDS if max_age is None else max_age
    if arrived and (time.time() if now is None else now) - arrived > limit:
        return None
    info = ((msg.get("message") or {}).get(msg.get("messageType")) or {})
    try:
        size = int(info.get("fileLength", 0)) if isinstance(info, dict) else 0
    except (TypeError, ValueError):
        size = 0
    if size > ai_config.MAX_SOURCE_BYTES[kind]:
        return None
    return kind


def usable_providers(config, kind, saved):
    """The providers to try: on, with a saved key, taking this kind, and that
    the person consented to. Nothing is sent to any other."""
    return [p for p in ai_config.chain(config, kind, lambda provider: provider in saved)
            if p in config["consented"]]
