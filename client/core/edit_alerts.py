"""What an edited message sounds and says (issue #824).

Three sound events, in Settings > Sound Events (core/sound_system.py):

* ``message_edit_saved``: an edit of yours WhatsApp accepted. On by default,
  and by default the "message sent" sound;
* ``message_edited_foreground`` / ``message_edited_background``: someone
  else edited a message, with WinZapp in front or not. Off by default, and by
  default the sounds of a message received in each state.

And two announcements, in Settings > Notifications (``general``), both off by
default: speak "Message from <name> edited: <text>" with the window active
(``announce_edited_messages``), and show a notification for it with the
window in the background (``notify_edited_messages``).

Each conversation can depart from all five in its data window
(ui/dialogs/conversation_edit_alerts.py). ``conversation_edit_alerts`` maps a
normalized JID to what the user changed there; anything left at "default" is
not stored, so the map only holds the exceptions:

    {"<jid>": {"foreground": {"choice": "alert_2", "custom_path": ""},
               "speak_foreground": "off"}}

A sound choice is "default" (the Sound Events entry, on or off as it is
there), "off", "event" (the Sound Events file, even when that entry is off),
"custom" (the file in ``custom_path``) or an alert tone key from the pack
(core/alert_tones.py), like the conversation's notification sound.
"""

from core.remote_deletions import message_timestamp_seconds

SETTING = "conversation_edit_alerts"

SAVED = "saved"
FOREGROUND = "foreground"
BACKGROUND = "background"
SCOPES = (SAVED, FOREGROUND, BACKGROUND)

#: The Sound Events entry each scope plays.
SOUND_EVENTS = {
    SAVED: "message_edit_saved",
    FOREGROUND: "message_edited_foreground",
    BACKGROUND: "message_edited_background",
}

CHOICE_DEFAULT = "default"
CHOICE_OFF = "off"
CHOICE_EVENT = "event"
CHOICE_CUSTOM = "custom"

SPEAK_FOREGROUND = "speak_foreground"
NOTIFY_BACKGROUND = "notify_background"
TOGGLES = (SPEAK_FOREGROUND, NOTIFY_BACKGROUND)
#: The install-wide option (settings["general"]) each toggle defaults to.
GLOBAL_KEYS = {
    SPEAK_FOREGROUND: "announce_edited_messages",
    NOTIFY_BACKGROUND: "notify_edited_messages",
}
TOGGLE_DEFAULT = "default"
TOGGLE_ON = "on"
TOGGLE_OFF = "off"
TOGGLE_CHOICES = (TOGGLE_DEFAULT, TOGGLE_ON, TOGGLE_OFF)

#: WhatsApp accepts an edit up to 20 minutes after the message was sent
#: (core/message_edit.py, isParentWithinEditProcessingWindow); five more cover
#: a clock that runs behind. An "edit" of anything older is a redelivery of a
#: message edited long ago (a reconnection, a resync), never news.
EDIT_PROCESSING_WINDOW_SECONDS = 1200
CLOCK_MARGIN_SECONDS = 300


def _section(settings):
    section = settings.get(SETTING) if isinstance(settings, dict) else None
    return section if isinstance(section, dict) else {}


def _entry(settings, jid) -> dict:
    entry = _section(settings).get(jid)
    return entry if isinstance(entry, dict) else {}


def stored_jid(settings, candidates) -> str:
    """The first of *candidates* (forms of one chat's JID: phone, @lid) with
    something stored, else the first one."""
    candidates = [jid for jid in candidates if jid]
    for jid in candidates:
        if _entry(settings, jid):
            return jid
    return candidates[0] if candidates else ""


def _store(settings, jid, field, value) -> None:
    """Set or (value None) drop one field of *jid*'s entry, and the entry
    itself once it is empty."""
    section = settings.get(SETTING)
    if not isinstance(section, dict):
        section = settings[SETTING] = {}
    entry = dict(section.get(jid) or {})
    if value is None:
        entry.pop(field, None)
    else:
        entry[field] = value
    if entry:
        section[jid] = entry
    else:
        section.pop(jid, None)


def sound_choice(settings, jid, scope) -> tuple:
    """``(choice, custom_path)`` of *scope* for conversation *jid*."""
    value = _entry(settings, jid).get(scope)
    if not isinstance(value, dict):
        return CHOICE_DEFAULT, ""
    choice = value.get("choice")
    custom = value.get("custom_path")
    return (choice if isinstance(choice, str) and choice else CHOICE_DEFAULT,
            custom if isinstance(custom, str) else "")


def set_sound_choice(settings, jid, scope, choice, custom_path="") -> None:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    custom_path = (custom_path or "").strip()
    if choice == CHOICE_DEFAULT and not custom_path:
        _store(settings, jid, scope, None)
    else:
        _store(settings, jid, scope, {"choice": choice, "custom_path": custom_path})


def sound_plan(choice, event_enabled) -> str:
    """What to play for a stored choice: ``"event"`` (the Sound Events file),
    ``"alert"`` (an alert tone or custom file, which falls back to the event's
    file when it cannot be found) or "" (nothing)."""
    if choice == CHOICE_OFF:
        return ""
    if choice == CHOICE_DEFAULT:
        return "event" if event_enabled else ""
    if choice == CHOICE_EVENT:
        return "event"
    return "alert"


def toggle_choice(settings, jid, which) -> str:
    value = _entry(settings, jid).get(which)
    return value if value in TOGGLE_CHOICES else TOGGLE_DEFAULT


def set_toggle_choice(settings, jid, which, choice) -> None:
    if which not in TOGGLES or choice not in TOGGLE_CHOICES:
        raise ValueError(f"unknown toggle {which!r}={choice!r}")
    _store(settings, jid, which, None if choice == TOGGLE_DEFAULT else choice)


def global_toggle(settings, which) -> bool:
    """The Settings > Notifications option behind *which* (off by default)."""
    general = settings.get("general") if isinstance(settings, dict) else None
    value = general.get(GLOBAL_KEYS[which]) if isinstance(general, dict) else None
    return value if isinstance(value, bool) else False


def toggle_on(settings, jid, which) -> bool:
    """Whether *which* is on for conversation *jid*: its own choice, else the
    install-wide option."""
    choice = toggle_choice(settings, jid, which)
    if choice == TOGGLE_ON:
        return True
    if choice == TOGGLE_OFF:
        return False
    return global_toggle(settings, which)


def edit_is_recent(record, now) -> bool:
    """Whether an edit of *record* can have just happened: its message is
    young enough for WhatsApp to still accept an edit. A record with no
    timestamp is given the benefit of the doubt."""
    sent = message_timestamp_seconds(record)
    if not sent:
        return True
    return now - sent <= EDIT_PROCESSING_WINDOW_SECONDS + CLOCK_MARGIN_SECONDS
