"""SettingsDialog must be buildable against a bare stand-in main window.

Every wxgui suite that exercises this dialog (`test_settings_checkboxes_
roundtrip`, `test_settings_dialog_roundtrip`, `test_settings_files_saving_tab`,
`test_settings_dialog_apply_button`, `test_settings_dialog_spell_check_windows_
state`) stands a plain `wx.Frame` in for `MainWindow`. So a control wired
straight to one of `MainWindow`'s own methods — `Bind(evt, self.main_window.
some_method)` — raises `AttributeError` inside `__init__` and leaves a
part-built dialog behind.

That failure does not stay local. Once enough half-constructed dialogs leak,
wx answers `Failed to create dialog. Incorrect DLGTEMPLATE?` to every
subsequent one, so a single bad binding reported **155 failures and 14 errors**
across eight files, with the one informative traceback a thousand log lines
above the noise. Bind to a method on the dialog and look the opener up when the
button is actually pressed.

Checked statically because the alternative is constructing the dialog, and that
opens a real window on whoever is running the suite — which on this project is
usually a blind developer with a screen reader attached. See CLAUDE.md on why
the dialog tests are opt-in.
"""

import pathlib
import re

SOURCE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "client" / "ui" / "dialogs" / "settings_dialog.py"
).read_text(encoding="utf-8")


def test_no_control_is_bound_directly_to_a_main_window_method():
    offenders = re.findall(
        r"\.Bind\(\s*[^,]+,\s*self\.main_window\.([A-Za-z_][A-Za-z0-9_]*)",
        SOURCE,
    )
    assert not offenders, (
        "These controls are bound straight to MainWindow methods, so building "
        "the dialog against the plain wx.Frame the wxgui suites use raises "
        f"AttributeError: {sorted(set(offenders))}. Bind to a method on the "
        "dialog that looks the handler up with getattr() when the event fires."
    )


def test_the_call_audio_button_forwards_lazily():
    """The concrete case this file was written for."""
    assert "self._on_call_audio_settings" in SOURCE
    forwarder = SOURCE[SOURCE.index("def _on_call_audio_settings"):]
    forwarder = forwarder[: forwarder.index("\n    def ", 1)]
    assert 'getattr(self.main_window, "open_call_audio_settings", None)' in forwarder
    # A missing opener returns rather than raising: the button doing nothing is
    # survivable, the dialog failing to open is not.
    assert "if opener is None:" in forwarder
    assert "return" in forwarder
