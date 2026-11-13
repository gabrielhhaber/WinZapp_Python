"""Accessible controls for sharing desktop audio in an active voice call."""
import logging
import threading

import wx

from core.call_desktop_audio import DEFAULT_VOLUME, desktop_audio_supported, share_button_visible
from core.desktop_audio_sources import load_audio_apps


def stop_call_desktop_audio(window):
    """Stop the current session's desktop share; True when one was running."""
    stop = getattr(getattr(window, "_call_audio_session", None), "stop_desktop_audio", None)
    return bool(stop()) if stop is not None else False


def call_desktop_share_settings(window):
    """What the current session shares, so a device switch can share it again."""
    worker = getattr(getattr(window, "_call_audio_session", None), "desktop_audio", None)
    return None if worker is None else worker.settings


class DesktopCallAudioMixin:
    def _make_call_desktop_audio_button(self, panel):
        button = wx.Button(panel, label=self.i18n.t("call_desktop_share"))
        button.Bind(wx.EVT_BUTTON, self._toggle_call_desktop_audio)
        button.Hide()
        return button

    def _sync_call_desktop_audio_button(self, active):
        button = self.voice_call_window_desktop_audio_button
        audio = getattr(self, "_call_audio_session", None)
        sharing = audio is not None and getattr(audio, "desktop_audio", None) is not None
        visible = share_button_visible(
            desktop_audio_supported(), active, audio is not None and audio.running,
            sharing, self._voice_call_last_announced_state,
        )
        if not visible and button.IsShown() and button.HasFocus():
            self.voice_call_window_mute_button.SetFocus()
        button.Show(visible)
        if visible:
            button.SetLabel(self.i18n.t("call_desktop_stop") if sharing else self.i18n.t("call_desktop_share"))

    def _call_desktop_audio_allowed(self, call, audio):
        """The call and audio session a share was asked for are still the live ones."""
        return bool(
            call and audio is not None
            and getattr(self, "_active_voice_call", None) is call
            and getattr(self, "_call_audio_session", None) is audio
            and getattr(audio, "running", False)
            and not call.get("is_video")
            and self._voice_call_last_announced_state == "ACTIVE"
            and not getattr(self, "_shutting_down", False)
        )

    def _choose_call_desktop_audio(self, apps):
        dialog = wx.Dialog(self.voice_call_window, title=self.i18n.t("call_desktop_title"))
        layout = wx.BoxSizer(wx.VERTICAL)
        # Each label is created right before its control: Windows names a
        # control after the static text preceding it in creation order, and
        # the label's Alt letter moves focus to that next control.
        layout.Add(wx.StaticText(dialog, label=self.i18n.t("call_desktop_source")), 0, wx.LEFT | wx.TOP, 10)
        source = wx.Choice(dialog, choices=[
            self.i18n.t("call_desktop_no_app"), self.i18n.t("call_desktop_system"),
            *[f"{app.name} ({app.pid})" for app in apps],
        ])
        source.SetSelection(0)
        layout.Add(source, 0, wx.EXPAND | wx.ALL, 10)
        nvda = wx.CheckBox(dialog, label=self.i18n.t("call_desktop_nvda"))
        layout.Add(nvda, 0, wx.ALL, 10)
        layout.Add(wx.StaticText(dialog, label=self.i18n.t("call_desktop_volume")), 0, wx.LEFT | wx.TOP, 10)
        volume = wx.SpinCtrl(dialog, min=25, max=400, initial=DEFAULT_VOLUME)
        layout.Add(volume, 0, wx.ALL, 10)
        layout.Add(dialog.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 10)
        confirm = dialog.FindWindowById(wx.ID_OK)
        confirm.Enable(False)

        def update(_event):
            confirm.Enable(source.GetSelection() > 0 or nvda.GetValue())

        source.Bind(wx.EVT_CHOICE, update)
        nvda.Bind(wx.EVT_CHECKBOX, update)
        dialog.SetSizerAndFit(layout)
        source.SetFocus()
        try:
            if dialog.ShowModal() == wx.ID_OK:
                selection = source.GetSelection()
                return selection == 1, nvda.GetValue(), apps[selection - 2] if selection >= 2 else None, volume.GetValue()
        finally:
            dialog.Destroy()
        return None

    def _toggle_call_desktop_audio(self, _event=None):
        call = getattr(self, "_active_voice_call", None)
        audio = getattr(self, "_call_audio_session", None)
        if not desktop_audio_supported() or not call or audio is None:
            return
        if stop_call_desktop_audio(self):
            self._sync_voice_call_bar()
            self.output(self.i18n.t("call_desktop_stopped"), interrupt=True)
            return
        if not self._call_desktop_audio_allowed(call, audio) or getattr(self, "_call_desktop_apps_loading", False):
            return
        self._call_desktop_apps_loading = True
        threading.Thread(target=self._load_call_desktop_audio_apps, args=(call, audio),
                         name="call-desktop-apps", daemon=True).start()

    def _load_call_desktop_audio_apps(self, call, audio):
        """Worker thread: list the audio applications, then open the picker."""
        apps = []
        try:
            apps = load_audio_apps()
        except Exception:
            logging.exception("[call] desktop audio app list failed")
        finally:
            wx.CallAfter(self._pick_call_desktop_audio, call, audio, apps)

    def _pick_call_desktop_audio(self, call, audio, apps):
        self._call_desktop_apps_loading = False
        if not self._call_desktop_audio_allowed(call, audio):
            return
        choice = self._choose_call_desktop_audio(apps)
        if not choice or not any(choice[:3]) or not self._call_desktop_audio_allowed(call, audio):
            return
        self._start_call_desktop_audio(audio, choice, announce=True)

    def _start_call_desktop_audio(self, audio, settings, *, announce):
        system, nvda, app, volume = settings
        try:
            started = audio.start_desktop_audio(
                system, nvda,
                lambda worker, ok: wx.CallAfter(self._on_call_desktop_audio, audio, worker, ok, announce),
                app=app, volume=volume,
            )
        except (RuntimeError, OSError, ValueError):
            logging.exception("[call] desktop audio worker could not start")
            started = False
        if not started:
            self.output(self.i18n.t("call_desktop_failed"), interrupt=True)
        self._sync_voice_call_bar()

    def _resume_call_desktop_audio(self, call, settings):
        """Share again on the session a device switch opened for the same call.

        Quiet when it works: the user changed a device, not the share.
        """
        audio = getattr(self, "_call_audio_session", None)
        if not self._call_desktop_audio_allowed(call, audio):
            return
        self._start_call_desktop_audio(audio, settings, announce=False)

    def _on_call_desktop_audio(self, audio, worker, ok, announce=True):
        if (getattr(self, "_call_audio_session", None) is not audio or audio.desktop_audio is not worker
                or getattr(self, "_shutting_down", False)):
            return
        if not ok:
            audio.stop_desktop_audio(expected=worker)
        self._sync_voice_call_bar()
        if announce or not ok:
            self.output(self.i18n.t("call_desktop_started") if ok else self.i18n.t("call_desktop_failed"), interrupt=True)
