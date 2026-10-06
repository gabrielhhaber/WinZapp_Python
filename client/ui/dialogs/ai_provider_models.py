"""Model discovery for the provider window; background completions never move keyboard focus."""
import wx

from core.ai_credentials import CredentialError
from core.ai_media.errors import DescriptionError
from core.ai_media.model_catalog import fetch_models
from core.ai_media.service import RequestToken, submit


class ModelSelectionMixin:
    def _model_pinned(self):
        """False while "Automatic" is on: the model fields then stay locked."""
        automatic = getattr(self, "automatic", None)
        return automatic is None or not automatic.GetValue()

    def _init_model_catalog(self):
        self._model_list_generation = 0
        self._model_list_token = None
        self._model_list_timer = None
        self._model_options = ()

    def _refresh_model_choices(self):
        self.model_choice.Freeze()
        try:
            self.model_choice.SetItems([option.label for option in self._model_options])
            current = self.model.GetValue().strip()
            index = next((i for i, option in enumerate(self._model_options) if option.id == current), wx.NOT_FOUND)
            self.model_choice.SetSelection(index)
            self.model_choice.Enable(bool(self._model_options) and self._model_pinned())
        finally:
            self.model_choice.Thaw()

    def _cancel_model_list(self, *, clear=False):
        self._model_list_generation += 1
        if self._model_list_token:
            self._model_list_token.cancel()
        self._model_list_token = None
        if self._model_list_timer:
            self._model_list_timer.Stop()
        self._model_list_timer = None
        if self._alive:
            self.get_models.Enable(self._model_pinned())
        if clear and self._alive:
            self._model_options = ()
            self._refresh_model_choices()
            self.status.ChangeValue("")

    def _fetch_models(self, event):
        if not self._alive or self._model_list_token is not None:
            return
        self._cancel_model_list(clear=True)
        self._cancel_probe()
        try:
            provider, key = self._provider, self._current_key()
            if not key:
                raise DescriptionError("credentials")
            self._model_list_token = token = RequestToken(seconds=20)
            generation = self._model_list_generation
            self.status.ChangeValue(self._t("ai_models_loading"))
            self.get_models.Enable(False)
            self._model_list_timer = wx.CallLater(20_000, self._model_list_timeout, generation)
            submit(lambda: fetch_models(provider, key, token),
                   lambda result, error: wx.CallAfter(self._models_loaded, generation, provider, result, error))
        except (DescriptionError, CredentialError) as exc:
            self._cancel_model_list()
            self.status.ChangeValue(self._t(str(exc)))
        except Exception:
            self._cancel_model_list()
            self.status.ChangeValue(self._t("ai_error_response"))

    def _models_loaded(self, generation, provider, result, error):
        if not self._alive or generation != self._model_list_generation or provider != self._provider:
            return
        try:
            self._model_list_token.check()
        except DescriptionError as exc:
            error = str(exc)
        self._cancel_model_list()
        self._model_options = tuple(result or ()) if not error else ()
        self._refresh_model_choices()
        message = error or ("ai_models_ready" if self._model_options else "ai_models_empty")
        text = self._t(message)
        self.status.ChangeValue(text)
        self.main_window.output(text)

    def _model_list_timeout(self, generation):
        if self._alive and generation == self._model_list_generation and self._model_list_token:
            self._cancel_model_list()
            text = self._t("ai_error_timeout")
            self.status.ChangeValue(text)
            self.main_window.output(text)

    def _select_model(self, event):
        index = self.model_choice.GetSelection()
        if 0 <= index < len(self._model_options):
            self._cancel_probe()
            self.model.ChangeValue(self._model_options[index].id)
        event.Skip()

    def _manual_model_changed(self, event):
        self._cancel_probe()
        self._refresh_model_choices()
        event.Skip()
