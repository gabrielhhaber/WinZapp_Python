"""Settings: Apply keeps the focus on a visible button, and the two automatic
transcriptions cannot be saved on together.

Reported: Apply "jumped to the Shortcuts tab". Apply hides itself once
nothing is left to apply, and hiding the focused button left Windows to pick
where the focus went. And: both automatic transcriptions on made the local
and the online one write two texts for the same message; the conversation's
data window now offers one of them (or none), inherited from these options.
"""
from types import SimpleNamespace

import wx

from ui.dialogs.settings_dialog import SettingsDialog
from ui.dialogs.transcription_options import TranscriptionOptionsMixin


class _Button:
    def __init__(self):
        self.focused = False
        self.shown = True

    def SetFocus(self):
        self.focused = True

    def Hide(self):
        self.shown = False


def _apply_stub():
    stub = SimpleNamespace(
        _apply_values=lambda: True, _update_live_snapshot_fields=lambda: None,
        _refresh_dialog_labels=lambda: None, _maybe_warn_restart_required=lambda: None,
        _apply_btn=_Button(), _ok_btn=_Button(),
        _stop_unit_loading=lambda: None, _resume_unit_loading=lambda: None,
        _notebook=SimpleNamespace(GetPageCount=lambda: 0), Layout=lambda: None)
    return stub


def test_apply_hands_the_focus_to_ok_before_hiding_itself(monkeypatch):
    stub = _apply_stub()
    monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: stub._apply_btn))
    SettingsDialog._on_apply(stub, None)
    assert stub._ok_btn.focused and not stub._apply_btn.shown


def test_apply_leaves_the_focus_alone_when_it_is_elsewhere(monkeypatch):
    stub = _apply_stub()
    monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: object()))
    SettingsDialog._on_apply(stub, None)
    assert not stub._ok_btn.focused and not stub._apply_btn.shown


def _transcription_stub(local, online):
    shown = []
    stub = SimpleNamespace(
        _transcription_auto_check=SimpleNamespace(GetValue=lambda: local),
        _ai_page=SimpleNamespace(auto_transcribe=SimpleNamespace(GetValue=lambda: online)),
        _transcription_page="page",
        _show_page=lambda page, control: shown.append((page, control)),
        main_window=SimpleNamespace(app_name="WinZapp",
                                    i18n=SimpleNamespace(t=lambda key: key)))
    return stub, shown


def test_both_automatic_transcriptions_on_are_refused(monkeypatch):
    boxes = []
    monkeypatch.setattr(wx, "MessageBox", lambda *args, **kwargs: boxes.append(args[0]))
    stub, shown = _transcription_stub(True, True)
    assert TranscriptionOptionsMixin._validate_automatic_transcription(stub) is False
    assert boxes == ["transcription_auto_both_on"]
    assert shown == [("page", stub._transcription_auto_check)]


def test_one_or_none_on_is_saved(monkeypatch):
    monkeypatch.setattr(wx, "MessageBox", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))
    for local, online in ((True, False), (False, True), (False, False)):
        stub, shown = _transcription_stub(local, online)
        assert TranscriptionOptionsMixin._validate_automatic_transcription(stub) is True
        assert shown == []


def test_the_check_runs_before_anything_is_saved():
    import inspect
    source = inspect.getsource(SettingsDialog._apply_values)
    check = source.index("self._validate_automatic_transcription()")
    assert check < source.index("self._ai_page.apply()")
