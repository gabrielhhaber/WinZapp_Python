"""Accessible controls for sharing desktop audio in an active voice call."""
import logging

import wx

from core.call_desktop_audio import desktop_audio_supported
from core.desktop_audio_sources import list_audio_apps
from core.wasapi_capture import WasapiBackend


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
        visible = desktop_audio_supported() and bool(
            active and not active.get("is_video")
            and audio is not None and audio.running
            and (sharing or self._voice_call_last_announced_state == "ACTIVE")
        )
        if not visible and button.IsShown() and button.HasFocus():
            self.voice_call_window_mute_button.SetFocus()
        button.Show(visible)
        if visible:
            t = self.i18n.t
            button.SetLabel(t("call_desktop_stop") if sharing else t("call_desktop_share"))

    def _choose_call_desktop_audio(self):
        t = self.i18n.t
        try:
            backend = WasapiBackend()
            try:
                apps = list_audio_apps(backend)
            finally:
                backend.close()
        except (OSError, RuntimeError):
            logging.exception("[call] desktop audio app list unavailable")
            apps = []
        dialog = wx.Dialog(self.voice_call_window, title=t("call_desktop_title"))
        layout = wx.BoxSizer(wx.VERTICAL)
        source_label = t("call_desktop_source")
        source = wx.Choice(dialog, name=source_label.replace("&", ""),
                           choices=[t("call_desktop_no_app"), t("call_desktop_system").replace("&", ""),
                                    *[f"{app.name} ({app.pid})" for app in apps]])
        source.SetSelection(0)
        nvda = wx.CheckBox(dialog, label=t("call_desktop_nvda"))
        volume_label = t("call_desktop_volume")
        volume = wx.SpinCtrl(dialog, name=volume_label.replace("&", ""), min=25, max=400, initial=200)
        layout.Add(wx.StaticText(dialog, label=source_label), 0, wx.LEFT | wx.TOP, 10)
        layout.Add(source, 0, wx.EXPAND | wx.ALL, 10)
        layout.Add(nvda, 0, wx.ALL, 10)
        layout.Add(wx.StaticText(dialog, label=volume_label), 0, wx.LEFT | wx.TOP, 10)
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
        t = self.i18n.t
        if audio.stop_desktop_audio():
            self._sync_voice_call_bar()
            self.output(t("call_desktop_stopped"), interrupt=True)
            return
        if call.get("is_video") or self._voice_call_last_announced_state != "ACTIVE":
            return
        choice = self._choose_call_desktop_audio()
        if (not choice or not any(choice[:3]) or self._active_voice_call is not call
                or self._call_audio_session is not audio or call.get("is_video")
                or self._voice_call_last_announced_state != "ACTIVE"
                or getattr(self, "_shutting_down", False)):
            return
        try:
            system, nvda, app, volume = choice
            started = audio.start_desktop_audio(
                system, nvda, lambda worker, ok: wx.CallAfter(self._on_call_desktop_audio, audio, worker, ok),
                app=app, volume=volume,
            )
        except (RuntimeError, OSError):
            logging.exception("[call] desktop audio worker could not start")
            started = False
        if not started:
            self.output(t("call_desktop_failed"), interrupt=True)
        self._sync_voice_call_bar()

    def _on_call_desktop_audio(self, audio, worker, ok):
        if (getattr(self, "_call_audio_session", None) is not audio or audio.desktop_audio is not worker
                or getattr(self, "_shutting_down", False)):
            return
        t = self.i18n.t
        if not ok:
            audio.stop_desktop_audio(expected=worker)
        self._sync_voice_call_bar()
        self.output(t("call_desktop_started") if ok else t("call_desktop_failed"), interrupt=True)
