from core.foreground_sound import (
    SCOPE_ACTIVE, SCOPE_OTHER, foreground_sound_choice, play_foreground_message_sound,
)


class _Sound:
    def __init__(self):
        self.plays = 0

    def play(self):
        self.plays += 1


class _Window:
    def __init__(self, settings):
        self.settings = settings
        self.message_current_sound = _Sound()
        self.message_foreground_sound = _Sound()
        self.background_calls = []

    def play_background_notification_sound(self, remote_jid, respect_quiet_hours=True):
        self.background_calls.append((remote_jid, respect_quiet_hours))


def test_choice_defaults_to_dedicated_for_missing_or_invalid_values():
    assert foreground_sound_choice({}, SCOPE_ACTIVE) == "dedicated"
    assert foreground_sound_choice({"foreground_sounds": "x"}, SCOPE_OTHER) == "dedicated"
    assert foreground_sound_choice({"foreground_sounds": {"active": "bogus"}}, SCOPE_ACTIVE) == "dedicated"


def test_dedicated_plays_the_matching_foreground_sound():
    window = _Window({})
    play_foreground_message_sound(window, SCOPE_ACTIVE, "a@s.whatsapp.net")
    play_foreground_message_sound(window, SCOPE_OTHER, "b@s.whatsapp.net")
    assert (window.message_current_sound.plays, window.message_foreground_sound.plays) == (1, 1)
    assert window.background_calls == []


def test_notification_choice_uses_background_sound_ignoring_quiet_hours():
    window = _Window({"foreground_sounds": {"active": "notification", "other": "dedicated"}})
    play_foreground_message_sound(window, SCOPE_ACTIVE, "a@s.whatsapp.net")
    play_foreground_message_sound(window, SCOPE_OTHER, "b@s.whatsapp.net")
    assert window.background_calls == [("a@s.whatsapp.net", False)]
    assert window.message_current_sound.plays == 0
    assert window.message_foreground_sound.plays == 1
