"""The local model servers (Ollama, LM Studio) on the AI settings page.

A mixin of ``AISettingsPage`` (ai_settings_page.py): the page keeps the order,
the on/off switch and the model of every provider; a local one adds an address
and has no key, so its row and its window are these. Nothing here touches the
credential store: a local server holds no key.
"""
import wx

from core.ai_media import config as ai_config
from core.ai_media.config import PROVIDERS
from .ai_local_provider_dialog import AILocalProviderDialog


#: The sentence of each ``ai_config.local_status``.
STATE_KEYS = {"ready": "ai_local_state_ready", "remote": "ai_local_state_remote",
              "cloud": "ai_local_state_cloud", "no_model": "ai_local_state_no_model",
              "bad_url": "ai_local_state_bad_url"}


def local_row(t, name, enabled, status):
    """Item text of a local provider: its name, whether it is on, and why it
    would or would not be used (the same three parts as an online row)."""
    return ", ".join((name, t("ai_provider_state_enabled" if enabled else "ai_provider_state_disabled"),
                      t(STATE_KEYS[status])))


def changed_addresses(saved, staged):
    """The local providers whose address differs from the saved one: what was
    consented to for the old address does not carry over to the new."""
    return {p for p, url in staged.items() if saved.get(p, "") != url}


def changed_cloud_models(saved, staged):
    """The local providers whose model moved to or from a cloud one (Ollama's
    ``-cloud`` / ``:cloud`` ids): the consent given before does not cover the
    other side, like a changed address."""
    return {p for p, model in staged.items()
            if ai_config.is_local(p) and ai_config.is_cloud_model(saved.get(p, "")) != ai_config.is_cloud_model(model)}


class AILocalSettingsMixin:
    def _local_config(self):
        """The staged state of the local providers as ``config`` functions read it."""
        return {"models": self._models, "local_urls": self._local_urls}

    def _local_row(self, provider):
        return local_row(self._t, PROVIDERS[provider].name, provider not in self._disabled,
                         ai_config.local_status(self._local_config(), provider))

    def _configure_local(self, index, provider):
        state = {"url": self._local_urls.get(provider, ""), "model": self._models.get(provider, ""),
                 "enabled": provider not in self._disabled}
        dialog = AILocalProviderDialog(self.GetTopLevelParent(), self.main_window, provider, state)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            draft = dialog.values()
        finally:
            dialog.Destroy()
        self._local_urls[provider] = draft["url"]
        self._models[provider] = draft["model"]
        (self._disabled.discard if draft["enabled"] else self._disabled.add)(provider)
        self._refresh_list(index)
        self._on_change()
