"""Tests for backfilling absent settings defaults, and its ordering.

The backfill fills in keys a settings.json predating a feature does not have.
It shipped as a closure inside MainWindow.load_settings() that ran BEFORE
_migrate_settings(), which broke the one migration that exists:

    if "ui" in self.settings and "user_interface" not in self.settings:
        self.settings["user_interface"] = self.settings.pop("ui")

Backfill first creates "user_interface", so the condition is False, the legacy
"ui" block is orphaned, and every UI preference an older install had set
(page_up_down_step, self_reference_mode, show_yesterday_label, ...) silently
reverts to defaults — and the same backfill then writes that to disk.
"""

import inspect

from core.utils import backfill_missing_defaults, migrate_call_exclusive_mode_split
from main import MainWindow


class TestItFillsOnlyWhatIsAbsent:
    def test_a_missing_top_level_section_is_added(self):
        settings = {"general": {"a": 1}}
        assert backfill_missing_defaults(settings, {"general": {"a": 1}, "ui": {}}) is True
        assert settings["ui"] == {}

    def test_a_missing_nested_key_is_added(self):
        settings = {"general": {"a": 1}}
        backfill_missing_defaults(settings, {"general": {"a": 9, "b": 2}})
        assert settings["general"] == {"a": 1, "b": 2}

    def test_an_existing_value_is_never_overwritten(self):
        settings = {"general": {"alpha_updates_enabled": True}}
        backfill_missing_defaults(settings, {"general": {"alpha_updates_enabled": False}})
        assert settings["general"]["alpha_updates_enabled"] is True

    def test_nothing_missing_reports_no_change(self):
        settings = {"general": {"a": 1}}
        assert backfill_missing_defaults(settings, {"general": {"a": 1}}) is False

    def test_the_default_is_deep_copied(self):
        """A shared nested dict would make one account's edit leak into the
        template, and from there into every other account backfilled after."""
        defaults = {"ui": {"nested": {"x": 1}}}
        settings = {}
        backfill_missing_defaults(settings, defaults)
        settings["ui"]["nested"]["x"] = 999
        assert defaults["ui"]["nested"]["x"] == 1


class TestItRunsAfterTheMigration:
    def test_backfill_does_not_pre_empt_the_ui_rename(self):
        """The failure in one assertion, at the level the bug lived: a legacy
        settings dict must still be migratable after the backfill has seen
        it — i.e. backfill must not be what creates user_interface."""
        legacy = {"ui": {"page_up_down_step": 7}}
        defaults = {"user_interface": {"page_up_down_step": 1}}

        # Order as shipped in load_settings(): migrate, then backfill.
        migrated = dict(legacy)
        if "ui" in migrated and "user_interface" not in migrated:
            migrated["user_interface"] = migrated.pop("ui")
        backfill_missing_defaults(migrated, defaults)

        assert migrated["user_interface"]["page_up_down_step"] == 7, (
            "the user's own value must survive the rename"
        )
        assert "ui" not in migrated

    def test_load_settings_calls_them_in_that_order(self):
        src = inspect.getsource(MainWindow.load_settings)
        assert "_migrate_settings" in src and "backfill_missing_defaults" in src
        assert src.index("self._migrate_settings()") < src.index(
            "backfill_missing_defaults("
        ), "backfilling before migrating orphans the legacy 'ui' section"


class TestItPersistsThroughTheLock:
    def test_it_saves_via_save_settings_not_a_raw_dump(self):
        """save_settings() is what takes self._save_lock; WebSocket handlers
        and the debounce timer write this same file concurrently."""
        src = inspect.getsource(MainWindow.load_settings)
        backfill_at = src.index("backfill_missing_defaults(")
        tail = src[backfill_at:]
        assert "self.save_settings()" in tail
        # Scoped to the backfill's own tail on purpose: the corrupted-settings
        # recovery earlier in this method legitimately writes the freshly
        # seeded fallback with a plain dump, before _save_lock is meaningful.
        assert "json.dump(self.settings" not in tail


# ── call_audio_devices.exclusive_mode -> exclusive_input + exclusive_output ──
#
# One checkbox governed both directions. Holding the MICROPHONE exclusively
# takes a device nothing else is using mid-call; holding the SPEAKER
# exclusively silences every other application on it -- the screen reader
# included, for the whole call, which for WinZapp's users means losing the
# call window's own spoken controls. Only the output one now carries a warning.

def test_a_legacy_exclusive_choice_reaches_the_microphone_but_never_the_speaker():
    """The old checkbox never did anything: exclusive mode could not engage at
    all until this release made WASAPI reachable. Carrying it onto the SPEAKER
    would turn a dead flag into a live one in the same update -- so a user who
    ticked it months ago would answer their first call and lose the screen
    reader for the whole of it, never having seen the warning, which only
    fires when the box is ticked in Settings."""
    settings = {"call_audio_devices": {"exclusive_mode": True}}

    assert migrate_call_exclusive_mode_split(settings) is True

    section = settings["call_audio_devices"]
    assert section["exclusive_input"] is True
    assert section["exclusive_output"] is False
    # The old key is gone, so a later build cannot read a stale value that no
    # longer governs anything.
    assert "exclusive_mode" not in section


def test_the_migration_is_one_shot_and_does_not_undo_a_later_choice():
    """Without the flag, a user who unticks the speaker box would find it back
    on at the next launch -- and that is exactly the user who cares."""
    settings = {"call_audio_devices": {"exclusive_mode": True}}
    migrate_call_exclusive_mode_split(settings)

    settings["call_audio_devices"]["exclusive_output"] = False
    assert migrate_call_exclusive_mode_split(settings) is False
    assert settings["call_audio_devices"]["exclusive_output"] is False


def test_an_install_without_the_legacy_key_is_left_to_the_backfill():
    """A missing old key is not invented here: backfill_missing_defaults()
    puts both new keys in at their False defaults straight after."""
    settings = {"call_audio_devices": {"input_device_name": "Mic"}}

    assert migrate_call_exclusive_mode_split(settings) is True

    section = settings["call_audio_devices"]
    assert "exclusive_input" not in section
    assert "exclusive_output" not in section


def test_the_migration_runs_before_the_backfill_that_would_invent_the_value():
    """Same ordering rule the other migrations rely on: run after the backfill
    and it operates on a value it just made up."""
    source = inspect.getsource(MainWindow._migrate_settings)
    assert "migrate_call_exclusive_mode_split" in source
