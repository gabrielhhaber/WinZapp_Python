"""Settings round trips on plain controls: no wx.App, dialog or playback."""
from types import SimpleNamespace
import copy
import pytest

from core.typing_feedback import MODES, mode
from ui.dialogs.typing_feedback_controls import TypingFeedbackControls
from ui.dialogs.sound_event_files import SoundEventFiles


class Control:
    def __init__(self, value=None):
        self.value = value
        self.enabled = True

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = value

    def GetSelection(self):
        return self.value

    def SetSelection(self, value):
        self.value = value

    def Enable(self, value):
        self.enabled = value


def controls():
    c = TypingFeedbackControls.__new__(TypingFeedbackControls)
    c.choice = Control()
    c.interval = Control()
    c.interval_label = Control()
    c.silence = Control(True)
    c.dialog = SimpleNamespace(_announce_typing_check=Control(True), _mark_dirty=lambda: None)
    return c


@pytest.mark.parametrize("selected", MODES)
def test_mode_selection_updates_the_original_checkbox_and_applies(selected):
    c = controls()
    c.load({})
    c.choice.SetSelection(MODES.index(selected))
    c._on_mode(SimpleNamespace(Skip=lambda: None))
    settings = {"speech_content": {"announce_typing": c.dialog._announce_typing_check.GetValue()}}
    c.apply(settings)
    assert mode(settings) == selected
    assert c.interval.enabled is (selected in ("sound", "both"))
    c.load(settings)
    assert MODES[c.choice.GetSelection()] == selected


@pytest.mark.parametrize("audible", [True, False])
@pytest.mark.parametrize("spoken", [True, False])
def test_original_checkbox_still_controls_speech_without_changing_sound(audible, spoken):
    c = controls()
    c.load({"speech_content": {"typing_sound": audible}})
    c.dialog._announce_typing_check.SetValue(spoken)
    c._on_speech(SimpleNamespace(Skip=lambda: None))
    settings = {"speech_content": {"announce_typing": spoken}}
    c.apply(settings)
    assert settings["speech_content"]["announce_typing"] is spoken
    assert settings["speech_content"]["typing_sound"] is audible


def test_opening_and_cancelling_settings_never_writes_defaults_or_changes_choices():
    c = controls()
    settings = {"speech_content": {"announce_typing": False}}
    original = copy.deepcopy(settings)
    c.load(settings)
    c.choice.SetSelection(MODES.index("both"))
    c._on_mode(SimpleNamespace(Skip=lambda: None))
    assert settings == original


def test_preview_uses_current_working_path_and_falls_back_when_removed(tmp_path):
    custom = tmp_path / "custom.wav"
    bundled = tmp_path / "typing.wav"
    custom.write_bytes(b"custom")
    bundled.write_bytes(b"default")
    p = SoundEventFiles.__new__(SoundEventFiles)
    from core.sound_system import SOUND_EVENTS
    p.dialog = SimpleNamespace(
        _sound_events_list=Control(next(i for i, (key, _) in enumerate(SOUND_EVENTS) if key == "typing")),
        _sound_event_path_field=Control(str(custom)), _current_pack_id="other",
        main_window=SimpleNamespace(_sound_packs={"other": {"dir": str(tmp_path), "events": {}}},
                                    _default_sound_pack={"dir": str(tmp_path), "events": {"typing": "typing.wav"}}))
    assert p.resolve() == str(custom)
    custom.unlink()
    assert p.resolve() == str(bundled)


def test_reset_clears_override_without_mutating_account_settings():
    p = SoundEventFiles.__new__(SoundEventFiles)
    changed = []
    field = Control("custom.wav")
    p.dialog = SimpleNamespace(_sound_event_path_field=field,
                               _update_sound_event_path_display=lambda: changed.append(field.GetValue()),
                               _mark_dirty=lambda: changed.append("dirty"))
    p._reset(None)
    assert changed == ["", "dirty"]


def test_new_control_access_keys_do_not_conflict_with_the_existing_sounds_page():
    from tests.test_settings_mnemonics import TOGETHER
    from tests.mnemonics import load_strings, mnemonic
    from tests.locales import registered_locale_codes
    new = ("typing_feedback_label", "typing_feedback_interval", "sound_event_browse",
           "sound_event_reset", "sound_event_preview", "typing_feedback_silence_capture")
    for locale in registered_locale_codes():
        strings = load_strings(locale)
        keys = list(dict.fromkeys(TOGETHER["page self._sound_events_page"])) + list(new)
        letters = [mnemonic(strings[key]) for key in keys]
        assert None not in letters, locale
        assert len(letters) == len(set(letters)), locale
        assert mnemonic(strings["sound_event_preview_stop"]) == mnemonic(strings["sound_event_preview"])
