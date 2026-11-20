"""The noise reducer's own settings window, opened from the Audio tab.

What used to sit in the tab (the model's state and its download) moved here,
next to the processor the model runs on: the processor, CUDA 12 or CUDA 13 on
Windows (core/cuda_components), Core ML on macOS. The components a CUDA
choice still needs are listed and downloaded here, only those that are
missing: CUDA 12 uses the cuBLAS the local transcription already downloads.
A new processor applies at once: a graphics card build runs in a process of
its own, which restarts (core/cuda_components/noise_worker_host.py).
"""

import logging
import sys
import threading

import wx

from core.cuda_components import store
from core.cuda_components import text as ctext
from core.cuda_components.job import ComponentsInstallJob
from core.cuda_components.model import device_of, offered_devices
from ui.dialogs.noise_model_section import NoiseModelSectionMixin
from ui.dialogs.transcription_progress import TranscriptionProgressDialog


class NoiseReducerDialog(NoiseModelSectionMixin, wx.Dialog):
    def __init__(self, parent, main_window, device=None):
        i18n = main_window.i18n
        super().__init__(parent, title=i18n.t("noise_reducer_title"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.main_window = main_window
        self._devices = offered_devices(sys.platform)
        chosen = device or device_of(main_window.settings)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._build_noise_model_section(self, sizer)
        self._device_label = wx.StaticText(self, label=i18n.t("noise_reducer_device_label"))
        sizer.Add(self._device_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._device_choice = wx.Choice(
            self, choices=[i18n.t(ctext.DEVICE_LABEL_KEYS[d]) for d in self._devices],
            name=i18n.t("noise_reducer_device_label").replace("&", ""))
        self._device_choice.SetSelection(
            self._devices.index(chosen) if chosen in self._devices else 0)
        self._device_choice.Bind(wx.EVT_CHOICE, lambda e: self._refresh_components())
        sizer.Add(self._device_choice, 0, wx.EXPAND | wx.ALL, 8)
        self._components_label = wx.StaticText(
            self, label=i18n.t("noise_reducer_components_label"))
        sizer.Add(self._components_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._components_field = wx.TextCtrl(
            self, style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_NO_VSCROLL, size=(-1, 80))
        sizer.Add(self._components_field, 0, wx.EXPAND | wx.ALL, 8)
        self._components_btn = wx.Button(self, label=i18n.t("noise_reducer_components_download"))
        self._components_btn.Bind(wx.EVT_BUTTON, self._on_download_components)
        sizer.Add(self._components_btn, 0, wx.LEFT | wx.BOTTOM, 8)
        self._remove_components_btn = wx.Button(
            self, label=i18n.t("noise_reducer_components_remove"))
        self._remove_components_btn.Bind(wx.EVT_BUTTON, self._on_remove_components)
        sizer.Add(self._remove_components_btn, 0, wx.LEFT | wx.BOTTOM, 8)
        self._remove_model_btn = wx.Button(self, label=i18n.t("noise_reducer_model_remove"))
        self._remove_model_btn.Bind(wx.EVT_BUTTON, self._on_remove_model)
        sizer.Add(self._remove_model_btn, 0, wx.LEFT | wx.BOTTOM, 8)
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(sizer)
        self._refresh_components()

    @property
    def device(self) -> str:
        index = self._device_choice.GetSelection()
        return self._devices[index] if 0 <= index < len(self._devices) else self._devices[0]

    def _missing(self):
        try:
            return store.missing(self.device)
        except Exception:
            return []

    def _refresh_components(self):
        missing = self._missing()
        self._components_field.SetValue(ctext.components_text(
            self.main_window.i18n, self.device, missing,
            store.bytes_to_download(missing)))
        self._components_btn.Enable(bool(missing))
        try:
            removable = store.removable(self.device)
        except Exception:
            removable = []
        self._remove_components_btn.Enable(bool(removable))
        from core import noise_model
        try:
            self._remove_model_btn.Enable(noise_model.is_installed())
        except Exception:
            self._remove_model_btn.Enable(False)

    def _on_download_components(self, _event):
        i18n = self.main_window.i18n
        missing = self._missing()
        if not missing:
            return
        if wx.MessageBox(
                i18n.t("noise_reducer_components_confirm").format(
                    count=len(missing), size=ctext.size_text(i18n, store.bytes_to_download(missing))),
                i18n.t("transcription_download_confirm_title"),
                wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        def make_job(on_progress, on_finished):
            return ComponentsInstallJob(missing, on_progress=on_progress,
                                        on_finished=on_finished)

        status = i18n.t("noise_reducer_components_progress")
        from update_background import background_downloads_enabled
        if background_downloads_enabled(self.main_window.settings):
            # Settings > General's "download in the background": the main
            # window owns the job and says its outcome; this window stays usable
            # (as the Transcription tab's downloads do).
            self._start_in_background(make_job, status, self._components_btn,
                                      self._components_field)
            return
        dialog = TranscriptionProgressDialog(
            self, i18n, self.main_window.speak_output, make_job, status,
            title=i18n.t("noise_reducer_title"),
        )
        try:
            dialog.run()
            job, result, error = dialog.job, dialog.result, dialog.error
        finally:
            dialog.Destroy()
        from core.transcription import management_text
        self.main_window.speak_output.output(management_text.announcement_text(
            i18n, job.announcement(result, error)))
        self._refresh_components()
        target = self._components_btn if self._components_btn.IsEnabled() else self._components_field
        target.SetFocus()

    def _on_remove_components(self, _event):
        """Remove this processor's components (never the transcription's
        cuBLAS); the noise reducer's process ends first, so none is in use
        (only another program could still hold one). Never while a
        download runs (it would wait on the wx thread for the whole download,
        then delete what it just installed)."""
        i18n = self.main_window.i18n
        if self._background_busy():
            return
        ids = store.removable(self.device)
        if not ids:
            return
        if wx.MessageBox(i18n.t("noise_reducer_components_remove_confirm").format(count=len(ids)),
                         i18n.t("noise_reducer_title"), wx.YES_NO | wx.ICON_QUESTION,
                         self) != wx.YES:
            return
        from core.cuda_components import noise_worker_host
        # host.stop() waits for the process (and for a restart holding its
        # lock): never on the wx thread, a screen reader would hear nothing
        self._remove_components_btn.Disable()

        def work():
            left = None
            try:
                left = remove_components(ids, noise_worker_host.host.stop, store.remove)
            except Exception:
                logging.exception("[noise_reducer] removing the components failed")
            finally:
                wx.CallAfter(self._components_removed, left)

        threading.Thread(target=work, name="noise-components-remove", daemon=True).start()

    def _components_removed(self, left):
        """On the wx thread: what remove_components() found."""
        if not self:
            return
        i18n = self.main_window.i18n
        if left is None:
            self.main_window.speak_output.output(i18n.t("transcription_error_cuda_runtime_busy"))
            self._refresh_components()
            self._focus_after_removal(self._remove_components_btn)
            return
        key = "noise_reducer_components_removed_restart" if left else "noise_reducer_components_removed"
        self.main_window.speak_output.output(i18n.t(key))
        self._refresh_components()
        self._refresh_noise_model_section()
        self._focus_after_removal(self._remove_components_btn)

    def _on_remove_model(self, _event):
        from core import noise_model
        i18n = self.main_window.i18n
        if self._background_busy():
            return
        if wx.MessageBox(i18n.t("noise_reducer_model_remove_confirm"), i18n.t("noise_reducer_title"),
                         wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        noise_model.remove()
        self.main_window.speak_output.output(i18n.t("noise_reducer_model_removed"))
        self._refresh_components()
        self._refresh_noise_model_section()
        self._focus_after_removal(self._remove_model_btn)

    def _focus_after_removal(self, button):
        """Never leave the focus on a button that just switched itself off."""
        if button.IsEnabled():
            button.SetFocus()
        else:
            self._components_field.SetFocus()



def remove_components(ids, stop, remove):
    """End the noise reducer's process, then delete *ids*: what *remove*
    could not delete (it returns it), or None when another window's download
    holds the folder. Blocks, so it runs on a worker thread."""
    stop()
    try:
        return remove(ids)
    except store.ComponentError:
        return None


def reducer_start_wanted(enabled, neural, model_installed) -> bool:
    """Whether the noise reducer's process is worth starting right now:
    the reducer on, the neural engine chosen AND its model there. Without
    the model a start would fail and leave the device in "failed", so a
    model downloaded later in the same window would never retry."""
    return bool(enabled) and bool(neural) and bool(model_installed)


class NoiseReducerButtonMixin:
    """The Audio tab's button opening the noise reducer's window, whose OK
    keeps the processor (general.noise_reduction_device). The engine itself
    (general.noise_reduction_neural) is chosen by the engine combo of
    ui/dialogs/voice_recording_options.py, its only writer."""

    def _build_noise_reducer_button(self, parent, sizer):
        i18n = self.main_window.i18n
        self._noise_reducer_device = None
        self._noise_reducer_page = parent
        self._noise_reducer_btn = wx.Button(parent, label=i18n.t("noise_reducer_button"))
        self._noise_reducer_btn.Bind(wx.EVT_BUTTON, self._on_noise_reducer_button)
        sizer.Add(self._noise_reducer_btn, 0, wx.LEFT | wx.BOTTOM, 8)
        check = getattr(self, "_noise_reduction_check", None)
        if check is not None:
            check.Bind(wx.EVT_CHECKBOX, self._on_noise_reduction_toggled)

    def _on_noise_reduction_toggled(self, event):
        self._sync_noise_reducer_button()
        event.Skip()

    def _sync_noise_reducer_button(self):
        check = getattr(self, "_noise_reduction_check", None)
        reduction_on = check is not None and check.GetValue()
        # the reducer's own window only means something with the reducer on
        button = self._noise_reducer_btn
        if button.IsShown() != bool(reduction_on):
            button.Show(bool(reduction_on))
            self._noise_reducer_page.Layout()

    def _load_noise_reducer_options(self):
        self._sync_noise_reducer_button()

    def _relabel_noise_reducer_button(self):
        i18n = self.main_window.i18n
        self._noise_reducer_btn.SetLabel(i18n.t("noise_reducer_button"))

    def _on_noise_reducer_button(self, _event):
        dialog = NoiseReducerDialog(self, self.main_window, self._noise_reducer_device)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self._noise_reducer_device = dialog.device
        finally:
            dialog.Destroy()
        # The model or selected device may have changed in the dialog.
        self._sync_noise_reducer_button()
        update = getattr(self, "_update_voice_nr_options_state", None)
        if update is not None:
            update()
        self._noise_reducer_btn.SetFocus()

    def _save_noise_reducer_device(self):
        general = self.main_window.settings.setdefault("general", {})
        if self._noise_reducer_device is None:
            return
        changed = general.get("noise_reduction_device") != self._noise_reducer_device
        general["noise_reduction_device"] = self._noise_reducer_device
        # applied at once: a graphics card build's process restarts, said
        # aloud (core/cuda_components/noise_worker_host.py); started now
        # only when a recording would use it (see reducer_start_wanted)
        if changed:
            from core import noise_model
            from core.cuda_components import noise_worker_host
            try:
                installed = noise_model.is_installed()
            except Exception:
                installed = False
            engine_combo = getattr(self, "_voice_nr_engine_combo", None)
            neural = engine_combo.GetSelection() == 0 if engine_combo is not None else True
            noise_worker_host.host.apply(
                self._noise_reducer_device,
                start=reducer_start_wanted(
                    general.get("noise_reduction_enabled"), neural, installed))
