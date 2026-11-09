"""Which sound a message plays while the WinZapp window is in the foreground.

Two independent choices: the conversation that is open ("active") and every
other conversation ("other"). Each is either the dedicated foreground sound
(``message_current`` / ``message_foreground``) or the same notification sound
the message would play in the background (per-conversation, per-type or the
``message_background`` Sound Events entry).
"""

SCOPE_ACTIVE = "active"
SCOPE_OTHER = "other"
SCOPES = (SCOPE_ACTIVE, SCOPE_OTHER)

CHOICE_DEDICATED = "dedicated"
CHOICE_NOTIFICATION = "notification"
CHOICES = (CHOICE_DEDICATED, CHOICE_NOTIFICATION)

SETTINGS_KEY = "foreground_sounds"


def foreground_sound_choice(settings: dict, scope: str) -> str:
    """The saved choice for *scope*; anything unknown means the dedicated sound."""
    saved = (settings or {}).get(SETTINGS_KEY)
    choice = saved.get(scope) if isinstance(saved, dict) else None
    return choice if choice in CHOICES else CHOICE_DEDICATED


def play_foreground_message_sound(window, scope: str, remote_jid: str) -> None:
    """Play the sound chosen for *scope* on *window* (a MainWindow or stub)."""
    if foreground_sound_choice(window.settings, scope) == CHOICE_NOTIFICATION:
        # If the user disabled the message_background sound event, this is
        # silent on purpose: it is the same sound the background would play.
        # The window is in front, so Focus Assist / fullscreen detection (made
        # for toasts) must not silence it.
        window.play_background_notification_sound(
            remote_jid, respect_quiet_hours=False)
        return
    sound = (window.message_current_sound if scope == SCOPE_ACTIVE
             else window.message_foreground_sound)
    sound.play()
