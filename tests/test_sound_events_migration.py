"""Regression test: a sound event disabled before the soundpack system
existed silently went back to "enabled" after the app updated.

Sound.play() (core/sound_system.py) only ever reads the nested shape
settings["sound_events"][pack_id][event_key] — that's the shape the
soundpack restructuring introduced. An install whose settings.json still had
the old flat shape (settings["sound_events"][event_key], from before
soundpacks existed) never got migrated, so every lookup under a real pack_id
came back empty and "enabled" silently defaulted back to True — e.g. the
startup sound played again even though Settings still showed it as
disabled, because nothing there was reading the field it still lived under.

MainWindow._migrate_settings() now detects and rewrites the old flat shape
into {DEFAULT_PACK_ID: {...}} on load. Exercised as a plain function bound
to a stub, per the project's convention for MainWindow (a wx.Frame) — see
tests/test_reported_bugfixes.py.
"""

from main import MainWindow
from core.sound_system import DEFAULT_PACK_ID
from core.utils import (VOICE_MEDIA_TYPE_MIGRATION_FLAG,
                        VOICE_MESSAGE_MODE_MIGRATION_FLAG)


class _MainWindowStub:
    _migrate_settings = MainWindow._migrate_settings

    def __init__(self, settings):
        self.settings = settings
        # _migrate_settings() runs every one-shot migration, and each of them
        # writes its own flag the first time — which would save the settings
        # here even when the sound-events migration, the only one this file is
        # about, did nothing. Mark the others as already done so save_calls
        # keeps answering the question these tests are asking.
        self.settings.setdefault("general", {}).update({
            VOICE_MEDIA_TYPE_MIGRATION_FLAG: True,
            VOICE_MESSAGE_MODE_MIGRATION_FLAG: True,
        })
        self.save_calls = 0

    def save_settings(self):
        self.save_calls += 1


def test_flat_sound_events_migrated_to_nested_shape():
    mw = _MainWindowStub({
        "sound_events": {
            "startup": {"enabled": False, "path": ""},
            "error": {"enabled": True, "path": ""},
        }
    })

    mw._migrate_settings()

    assert mw.settings["sound_events"] == {
        DEFAULT_PACK_ID: {
            "startup": {"enabled": False, "path": ""},
            "error": {"enabled": True, "path": ""},
        }
    }
    assert mw.save_calls == 1


def test_already_nested_sound_events_left_untouched():
    nested = {
        DEFAULT_PACK_ID: {"startup": {"enabled": False, "path": ""}},
        "custom_pack": {"startup": {"enabled": True, "path": ""}},
    }
    mw = _MainWindowStub({"sound_events": nested})

    mw._migrate_settings()

    assert mw.settings["sound_events"] == nested
    assert mw.save_calls == 0


def test_missing_sound_events_is_a_no_op():
    mw = _MainWindowStub({})

    mw._migrate_settings()

    assert "sound_events" not in mw.settings
    assert mw.save_calls == 0
