"""Camera selection for video calls, following the pattern already shipped for
call audio devices (settings["call_audio_devices"], open_call_audio_settings()).

Camera enumeration is extracted out of CameraCapture.start()'s device-listing
subprocess call into core.call_video.list_camera_devices() so the new settings
dialog can reuse it without a second copy of the same subprocess invocation
(covered directly in tests/test_call_video.py). This file covers the settings
side: open_call_audio_settings()'s include_audio/include_camera flags picking
which rows the dialog shows, and apply() writing only the settings section(s)
that were actually shown.

open_call_audio_settings() ends in dialog.ShowModal(), which blocks on a real
modal event loop — so, same approach as other dialog-opening methods tested
in this repo (e.g. the connection_dial.ShowModal() tests in
test_unattended_qr_halt.py / test_another_number_linked_wipe.py), ShowModal()
itself is monkeypatched: the fake finds the Apply button among the dialog's
real children and fires its bound handler directly, then returns without
ever pumping a real modal loop. MainWindow is a wx.Frame and cannot be
instantiated without a running wx.App, so open_call_audio_settings() is
exercised as a plain function bound onto a stub, the same approach as
tests/test_incoming_call_answer_without_video.py — but building the real
wx.Dialog it constructs still needs a real wx.App, hence the `wxgui` marker
and `hidden_frame()` for the parent.
"""

import pytest
import wx

from main import MainWindow
from tests.conftest import hidden_frame


class _I18n:
    def t(self, key):
        return {
            "voice_call_settings_title": "Call settings",
            "voice_call_video_settings_title": "Call video settings",
            "voice_call_recording_devices": "Recording devices",
            "voice_call_playback_devices": "Playback devices",
            "voice_call_camera_devices": "Camera",
            "audio_device_default": "System default",
            "cancel": "Cancel",
            "apply": "Apply",
            "ok": "OK",
        }.get(key, key)


class _CallSettingsMainWindow:
    open_call_audio_settings = MainWindow.open_call_audio_settings

    def __init__(self):
        self.settings = {}
        self.i18n = _I18n()
        self._call_audio_session = None
        self._call_camera_capture = None
        self.save_calls = 0
        self.audio_restarts = 0
        self.camera_stops = 0
        self.camera_starts = 0

    def _find_api_ffmpeg(self):
        return "ffmpeg.exe"

    def save_settings(self):
        self.save_calls += 1

    def _restart_active_voice_call_audio(self):
        self.audio_restarts += 1

    def _stop_call_camera(self):
        self.camera_stops += 1
        self._call_camera_capture = None

    def _start_call_camera(self):
        self.camera_starts += 1

    def _resume_call_camera(self):
        # apply() reopens the camera through the same guarded path as the
        # video-on button (see _call_camera_resuming).
        self._start_call_camera()
        self._call_camera_resuming = False


def _click_apply_and_close(dialog):
    """Fire the dialog's own Apply handler without pumping a real modal loop."""
    apply_button = next(
        child for child in dialog.GetChildren()
        if isinstance(child, wx.Button) and child.GetId() == wx.ID_APPLY
    )
    apply_button.GetEventHandler().ProcessEvent(
        wx.CommandEvent(wx.wxEVT_COMMAND_BUTTON_CLICKED, apply_button.GetId())
    )
    return wx.ID_OK


@pytest.fixture(autouse=True)
def _stub_device_enumeration(monkeypatch):
    monkeypatch.setattr("main.enumerate_input_devices", lambda: [(0, "Mic A")])
    monkeypatch.setattr(
        "sounddevice.query_devices",
        lambda: [{"name": "Speaker A", "max_output_channels": 2}],
    )
    monkeypatch.setattr(
        "core.call_video.list_camera_devices", lambda ffmpeg: ["Cam A", "Cam B"]
    )
    monkeypatch.setattr(
        "main.threading.Thread",
        lambda target=None, args=(), kwargs=None, daemon=None: _InlineThread(target),
    )


class _InlineThread:
    def __init__(self, target):
        self._target = target

    def start(self):
        self._target()


@pytest.mark.wxgui
def test_audio_only_dialog_shows_no_camera_row_and_keeps_default_title(wx_app):
    frame = hidden_frame()
    stub = _CallSettingsMainWindow()
    captured = {}

    def fake_show_modal(self):
        captured["title"] = self.GetTitle()
        captured["combos"] = [c for c in self.GetChildren() if isinstance(c, wx.ComboBox)]
        return _click_apply_and_close(self)

    orig_show_modal = wx.Dialog.ShowModal
    wx.Dialog.ShowModal = fake_show_modal
    try:
        stub.open_call_audio_settings(parent=frame)
    finally:
        wx.Dialog.ShowModal = orig_show_modal
        frame.Destroy()

    assert captured["title"] == "Call settings"
    assert len(captured["combos"]) == 2  # mic + speaker, no camera
    assert "call_video_devices" not in stub.settings
    assert stub.camera_starts == 0
    assert stub.camera_stops == 0


@pytest.mark.wxgui
def test_camera_only_dialog_shows_one_combo_and_the_video_title(wx_app):
    frame = hidden_frame()
    stub = _CallSettingsMainWindow()
    captured = {}

    def fake_show_modal(self):
        captured["title"] = self.GetTitle()
        combos = [c for c in self.GetChildren() if isinstance(c, wx.ComboBox)]
        captured["combos"] = combos
        combos[0].SetStringSelection("Cam B")
        return _click_apply_and_close(self)

    orig_show_modal = wx.Dialog.ShowModal
    wx.Dialog.ShowModal = fake_show_modal
    try:
        stub.open_call_audio_settings(parent=frame, include_audio=False, include_camera=True)
    finally:
        wx.Dialog.ShowModal = orig_show_modal
        frame.Destroy()

    assert captured["title"] == "Call video settings"
    assert len(captured["combos"]) == 1
    # Only the camera section was written; the audio section is untouched.
    assert "call_audio_devices" not in stub.settings
    assert stub.settings["call_video_devices"]["camera_name"] == "Cam B"
    assert stub.save_calls == 1
    assert stub.audio_restarts == 0


@pytest.mark.wxgui
def test_camera_apply_restarts_a_running_capture_live(wx_app):
    frame = hidden_frame()
    stub = _CallSettingsMainWindow()
    stub._call_camera_capture = object()  # a call's camera is already running

    def fake_show_modal(self):
        return _click_apply_and_close(self)

    orig_show_modal = wx.Dialog.ShowModal
    wx.Dialog.ShowModal = fake_show_modal
    try:
        stub.open_call_audio_settings(parent=frame, include_audio=False, include_camera=True)
    finally:
        wx.Dialog.ShowModal = orig_show_modal
        frame.Destroy()

    assert stub.camera_stops == 1
    assert stub.camera_starts == 1


@pytest.mark.wxgui
def test_audio_only_apply_never_touches_a_running_camera(wx_app):
    frame = hidden_frame()
    stub = _CallSettingsMainWindow()
    stub._call_camera_capture = object()

    def fake_show_modal(self):
        return _click_apply_and_close(self)

    orig_show_modal = wx.Dialog.ShowModal
    wx.Dialog.ShowModal = fake_show_modal
    try:
        stub.open_call_audio_settings(parent=frame)
    finally:
        wx.Dialog.ShowModal = orig_show_modal
        frame.Destroy()

    assert stub.camera_stops == 0
    assert stub.camera_starts == 0
