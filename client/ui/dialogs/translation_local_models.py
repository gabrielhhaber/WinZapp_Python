"""The offline model section of the message translation settings.

Mixed into ``TranslationSettingsDialog``: the model list (each item says its
size and whether it is on this computer), Download and Remove, and where the
model runs. The download and the removal run behind the transcription's
progress dialog (throttled bar, three spoken figures, Cancel), on the same
worker, so neither is written a second time; the sentences are
``core/text_translation/local_text``.
"""

import logging

import wx

from core.text_translation import local_catalog, local_engine, local_store
from core.text_translation.local_text import (
    download_confirmation, model_label, outcome_text, remove_confirmation,
)
from ui.dialogs.transcription_progress import TranscriptionProgressDialog


def _state(model):
    try:
        return local_store.state(model)
    except Exception as exc:
        logging.warning("[translation] could not read %s: %s", model.id, type(exc).__name__)
        return local_store.STATE_ABSENT


class TranslationLocalModelsMixin:
    def _build_local_section(self, config):
        t = self._t
        self._local_ids = tuple(model.id for model in local_catalog.MODELS)
        self.local_model = self._field("translation_local_model", wx.Choice,
                                       choices=self._local_labels())
        wanted = config.model if config.model in self._local_ids else local_catalog.DEFAULT_MODEL_ID
        self.local_model.SetSelection(self._local_ids.index(wanted))
        self.local_download = self._button("translation_local_download", self._on_local_download)
        self.local_remove = self._button("translation_local_remove", self._on_local_remove)
        self.local_device = self._field("translation_local_device", wx.Choice, choices=[
            t("transcription_device_preference_cpu"), t("transcription_device_preference_cuda"),
        ])
        self.local_device.SetSelection(local_engine.DEVICES.index(config.device)
                                       if config.device in local_engine.DEVICES else 0)
        self.local_model.Bind(wx.EVT_CHOICE, self._on_local_model)
        self._refresh_local()

    def _show_local_section(self, shown):
        for control in (self.local_model, self.local_download, self.local_remove,
                        self.local_device):
            self._show(control, shown)

    def _local_model(self):
        return local_catalog.get_model(self._local_ids[max(self.local_model.GetSelection(), 0)])

    def _local_device(self):
        return local_engine.DEVICES[max(self.local_device.GetSelection(), 0)]

    def _local_supports(self, code):
        return local_engine.supports(self._local_model(), code)

    def _local_labels(self):
        return [model_label(self._i18n, model, _state(model)) for model in local_catalog.MODELS]

    def _refresh_local(self):
        self.local_model.Freeze()
        try:
            for index, label in enumerate(self._local_labels()):
                self.local_model.SetString(index, label)
        finally:
            self.local_model.Thaw()
        state = _state(self._local_model())
        self.local_download.Enable(state != local_store.STATE_INSTALLED)
        self.local_remove.Enable(state != local_store.STATE_ABSENT)

    def _on_local_model(self, event):
        self._refresh_local()
        self._fill_languages()
        self.status.ChangeValue("")

    def _on_local_download(self, event):
        model = self._local_model()
        if wx.MessageBox(download_confirmation(self._i18n, model),
                         self._t("transcription_download_confirm_title"),
                         wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        self._run_local_job(local_store.ACTION_DOWNLOAD, model,
                            self._t("translation_local_downloading").format(name=model.name))

    def _on_local_remove(self, event):
        model = self._local_model()
        if wx.MessageBox(remove_confirmation(self._i18n, model),
                         self._t("transcription_remove_confirm_title"),
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
            return
        self._run_local_job(local_store.ACTION_REMOVE, model,
                            self._t("translation_local_removing").format(name=model.name))

    def _run_local_job(self, action, model, status):
        self._cancel()

        def make_job(on_progress, on_finished):
            return local_store.TranslationModelJob(action, model, on_progress=on_progress,
                                                   on_finished=on_finished)

        dialog = TranscriptionProgressDialog(self, self._i18n, self._speak_output, make_job, status,
                                             title=self.GetTitle())
        try:
            dialog.run()
            result, error = dialog.result, dialog.error
        finally:
            dialog.Destroy()
        sentence = outcome_text(self._i18n, action, model, result, error)
        self.status.ChangeValue(sentence)
        self._speak_output.output(sentence)
        self._refresh_local()
        # Never leave the focus on a button that just switched itself off.
        for control in (self.local_download, self.local_remove, self.local_model):
            if control.IsEnabled():
                control.SetFocus()
                break
