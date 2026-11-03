"""CI-only real settings-dialog round trips; never run on a user's desktop."""
import copy
from types import SimpleNamespace
import pytest

from core.typing_feedback import MODES, mode
from core.utils import DEFAULT_SETTINGS
from tests.test_settings_checkboxes_roundtrip import make_dialog  # noqa: F401

pytestmark = pytest.mark.wxgui


@pytest.mark.parametrize("spoken", [True, False])
def test_existing_typing_choice_opens_without_turning_on_sound(make_dialog, spoken):
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["speech_content"] = {"announce_typing": spoken}
    dialog = make_dialog(settings)
    assert dialog._announce_typing_check.GetValue() is spoken
    assert dialog._typing_feedback._selected() == ("speech" if spoken else "off")
    assert not dialog._typing_feedback.interval.IsEnabled()


@pytest.mark.parametrize("selected", MODES)
def test_mode_and_interval_survive_apply_and_reopen_in_real_controls(make_dialog, selected):
    dialog = make_dialog(copy.deepcopy(DEFAULT_SETTINGS))
    controls = dialog._typing_feedback
    controls.choice.SetSelection(MODES.index(selected))
    controls._on_mode(SimpleNamespace(Skip=lambda: None))
    controls.interval.SetValue(6)
    assert dialog._apply_values() is True
    saved = copy.deepcopy(dialog.main_window.settings)
    assert mode(saved) == selected
    reopened = make_dialog(saved)
    assert reopened._typing_feedback._selected() == selected
    assert reopened._typing_feedback.interval.GetValue() == 6


def test_existing_checkbox_keeps_control_of_spoken_content(make_dialog):
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["speech_content"]["typing_sound"] = True
    dialog = make_dialog(settings)
    dialog._announce_typing_check.SetValue(False)
    dialog._typing_feedback._on_speech(SimpleNamespace(Skip=lambda: None))
    assert dialog._apply_values() is True
    assert mode(dialog.main_window.settings) == "sound"


@pytest.mark.parametrize("silence", [True, False])
def test_capture_silencing_choice_survives_apply_and_reopen(make_dialog, silence):
    dialog = make_dialog(copy.deepcopy(DEFAULT_SETTINGS))
    dialog._typing_feedback.silence.SetValue(silence)
    assert dialog._apply_values() is True
    reopened = make_dialog(copy.deepcopy(dialog.main_window.settings))
    assert reopened._typing_feedback.silence.GetValue() is silence
