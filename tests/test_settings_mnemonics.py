"""Every control of the settings dialog and of the windows opened from it has
an Alt+letter, and no two controls on screen together share one, in every
locale.

Reported: most buttons and checkboxes of the settings and of their windows had
no Alt key at all, and the Transcription tab had more controls than letters
(its code said so: "the tab has no free letter left"). Its options now live in
windows of their own (ui/dialogs/transcription_windows.py).

Which controls share a window is read from the sources by
tests/settings_controls.py, never written out here: a control added later is
checked the day it is added. A settings page is on screen together with the
dialog's own controls (search field, OK, Cancel, Apply); each window opened
from the dialog has its own OK.
"""

import pytest

from tests.locales import registered_locale_codes
from tests.mnemonics import load_strings, mnemonic
from tests.settings_controls import DIALOG_CONTROLS, DIALOG_EXTRA_KEYS, collect

WINDOWS = collect()


def _reachable_together():
    """{window: [keys]} with each page holding the dialog's own controls too."""
    dialog = [k for k, _ctor, _where in WINDOWS.get(DIALOG_CONTROLS, [])] + list(DIALOG_EXTRA_KEYS)
    together = {}
    for name, items in WINDOWS.items():
        keys = [k for k, _ctor, _where in items]
        if name.startswith("page "):
            keys += dialog
        together[name] = list(dict.fromkeys(keys))
    return together


TOGETHER = _reachable_together()


@pytest.fixture(params=registered_locale_codes())
def strings(request):
    return load_strings(request.param)


def test_the_windows_are_found():
    """The reading of the sources still sees the dialog it is about."""
    assert DIALOG_CONTROLS in WINDOWS
    pages = [name for name in WINDOWS if name.startswith("page ")]
    assert len(pages) >= 10
    for window in ("self._shortcuts_dialog", "self._transcription_advanced_dialog",
                   "self._transcription_cuda_dialog", "self._transcription_whisper_dialog",
                   "self._whatsapp_privacy_dialog", "AIProviderDialog"):
        assert f"window {window}" in WINDOWS, window


def test_a_builder_used_twice_puts_its_controls_in_both_windows():
    """_build_transcription_action_row makes the model's buttons on the tab
    and the CUDA libraries' in their own window."""
    tab = [k for k, _c, _w in WINDOWS["page self._transcription_holder"]]
    cuda = [k for k, _c, _w in WINDOWS["window self._transcription_cuda_dialog"]]
    assert "transcription_model_download_btn" in tab and "transcription_model_download_btn" not in cuda
    assert "transcription_cuda_install_btn" in cuda and "transcription_cuda_install_btn" not in tab


@pytest.mark.parametrize("window", sorted(TOGETHER))
def test_every_control_has_an_alt_key(strings, window):
    missing = [k for k in TOGETHER[window] if mnemonic(strings[k]) is None]
    assert not missing, f"{window}: {missing}"


@pytest.mark.parametrize("window", sorted(TOGETHER))
def test_no_two_controls_of_a_window_share_a_letter(strings, window):
    letters = {}
    for key in TOGETHER[window]:
        letter = mnemonic(strings[key])
        if letter is None:
            continue
        assert letter not in letters, f"{window}: {key} and {letters[letter]} both on {letter!r}"
        letters[letter] = key


def test_a_mnemonic_never_lands_inside_a_placeholder(strings):
    import re
    for keys in TOGETHER.values():
        for key in keys:
            for placeholder in re.findall(r"\{[^}]*\}", strings[key]):
                assert "&" not in placeholder, key


def test_the_online_ai_windows_and_their_buttons_are_checked():
    """The kinds of media (ai_kinds_options.py) and the automatic options
    (ai_auto_options.py) live in windows of their own; a label the collector
    cannot read (a bare ``t(key)`` alias did that) leaves its letter unchecked."""
    automatic = {key for key, _ctor, _where in WINDOWS["window AISettingsPage:self.auto_dialog"]}
    assert {"ai_auto_transcribe", "ai_auto_describe", "ai_auto_scope", "ai_auto_max_age",
            "ai_auto_daily_limit", "ok"} <= automatic
    kinds = {key for key, _ctor, _where in WINDOWS["window AISettingsPage:self.kinds_dialog"]}
    assert {"ai_describe_images_label", "ai_describe_stickers_label", "ai_describe_videos_label",
            "ai_transcribe_audio_label", "ai_pdf_accessible_label", "ok"} <= kinds
    page = {key for key, _ctor, _where in WINDOWS["page self._ai_holder"]}
    assert {"ai_kinds_button", "ai_auto_button", "translation_settings"} <= page
    assert not {"ai_describe_images_label", "ai_pdf_accessible_label"} & page
