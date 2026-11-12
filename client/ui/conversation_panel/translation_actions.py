"""Keyboard, chat opt-in and live message translation for ConversationsPanel."""
from collections import OrderedDict, deque
from dataclasses import replace
from sqlite3 import Error as SQLiteError

import wx

from app_paths import global_dir
from app_settings import AppSettings
from core.ai_credentials import CredentialError, CredentialStore, store_for_translation
from core.ai_media.errors import DescriptionError
from core.ai_media.service import RequestToken, submit
from core.message_translation import (
    CUSTOM, DISPLAY_SEPARATE, MAX_TEXT_CHARS, TranslationSettings, automatic_candidate,
    chat_allows_automatic,
    credential_id, message_identity, message_text, preferences, reserve_automatic_request, translate,
    translation_model_ready,
)
from ui.dialogs.translation_settings import LABELS

_OPT_IN = "automatic_text_translation_chats"
_RESULT_LIMIT = 128
_QUEUE_LIMIT = 50


class TranslationActionsMixin:
    def _translation_app(self):
        return getattr(self.main_window, "_app_settings", None) or AppSettings(global_dir())

    def _translation_settings(self):
        try:
            return preferences(self._translation_app(), snapshot=True)
        except (OSError, RuntimeError):
            pass
        return TranslationSettings()

    def _translation_chats(self):
        if not hasattr(self, "_translation_opt_in"):
            saved = self.main_window.db.get_metadata_json(_OPT_IN, {})
            self.__dict__.setdefault("_translation_opt_in", saved if isinstance(saved, dict) else {})
        return self._translation_opt_in

    def _translation_chat_key(self, jid):
        mw = self.main_window
        jid = mw._normalize_jid(jid)
        return getattr(mw, "_lid_to_phone", {}).get(jid) or jid

    def _automatic_translation_enabled(self, jid, config):
        if not config.automatic or not config.provider or self.main_window.is_chat_locked(jid):
            return False
        try:
            saved = self._translation_chats()
            key = self._translation_chat_key(jid)
            lid = getattr(self.main_window, "_phone_to_lid", {}).get(key)
            return chat_allows_automatic(config, saved, (key, jid, lid))
        except (DescriptionError, OSError, RuntimeError, SQLiteError):
            return False

    def _automatic_translation_may_be_enabled(self, jid, config):
        # The request worker loads the account's chat choices on a cache miss,
        # before it is allowed to send anything. Keep SQLite off live UI admission.
        return (config.automatic and not self.main_window.is_chat_locked(jid)
                and (not hasattr(self, "_translation_opt_in")
                     or self._automatic_translation_enabled(jid, config)))

    def _append_translation_chat_item(self, menu, jid):
        config = self._translation_settings()
        if not config.automatic or config.provider not in LABELS or self.main_window.is_chat_locked(jid):
            return
        enabled = self._automatic_translation_enabled(jid, config)
        label = "translation_auto_off" if enabled else "translation_auto_on"
        item = menu.Append(wx.ID_ANY, self.main_window.i18n.t(label))
        self.Bind(wx.EVT_MENU, lambda event: self._toggle_translation_chat(jid, config), item)

    def _toggle_translation_chat(self, jid, config):
        mw = self.main_window
        t = mw.i18n.t
        key = self._translation_chat_key(jid)
        if mw.is_chat_locked(jid):
            return
        saved = dict(self._translation_chats())
        enabled = self._automatic_translation_enabled(jid, config)
        if not enabled:
            prompt = t("translation_auto_confirm").format(provider=LABELS[config.provider])
            if wx.MessageBox(prompt, t("translation_auto_on"), wx.YES_NO | wx.NO_DEFAULT, self) != wx.YES:
                return
        for address in (key, jid, getattr(mw, "_phone_to_lid", {}).get(key)):
            saved.pop(address, None)
        saved[key] = {"provider": credential_id(config), "enabled": not enabled}
        mw.db.set_metadata_json(_OPT_IN, saved)
        self._translation_opt_in = saved
        mw.output(t("translation_auto_on" if not enabled else "translation_auto_off"))
        if not enabled:
            self._translate_focused_recent(jid)

    def _translate_focused_recent(self, jid=None):
        active = (self.conversation or {}).get("remoteJid")
        if not active or (jid and self._translation_chat_key(jid) != self._translation_chat_key(active)):
            return
        index = self.messages_list.GetFocusedItem()
        if 0 <= index < len(self._sorted_messages):
            self._maybe_auto_translate(active, self._sorted_messages[index])

    def _on_translate_message(self, message):
        mw = self.main_window
        text = message_text(message)
        if not text:
            return
        config = self._translation_settings()
        if not config.provider:
            mw.output(mw.i18n.t("translation_not_configured"))
            return
        if not translation_model_ready(config):
            mw.output(mw.i18n.t("translation_test_required"))
            return
        jid = (self.conversation or {}).get("remoteJid", "")
        if not jid or mw.is_chat_locked(jid):
            return
        key = message_identity(message)
        if not key:
            return
        result = getattr(self, "_translation_results", {}).get(key)
        if result and result[1] == text and replace(result[0], display=config.display) == config:
            self._present_translation(jid, key, config, text, result[2], manual=True)
            return
        if getattr(self, "_manual_translation", None) is not None:
            mw.output(mw.i18n.t("ai_error_busy"))
            return
        token = self._manual_translation = RequestToken()

        def complete(answer, error):
            self._manual_translation = None
            if (getattr(mw, "_shutting_down", False)
                    or (self.conversation or {}).get("remoteJid") != jid or mw.is_chat_locked(jid)
                    or self._translation_settings() != config):
                return
            current = next((row for row in self._sorted_messages if message_identity(row) == key), None)
            if current is None or message_text(current) != text:
                return
            if error:
                mw.output(mw.i18n.t(error))
            else:
                self._present_translation(jid, key, config, text, answer, manual=True)

        try:
            self._submit_translation(config, text, token, complete)
        except DescriptionError as error:
            complete(None, error.key)
        else:
            provider = LABELS.get(config.provider, config.provider)
            mw.output(mw.i18n.t("translation_sending").format(provider=provider))

    def _submit_translation(self, config, text, token, complete, *, automatic=None):
        def work():
            if automatic is not None:
                jid, message = automatic
                token.check()
                if (self._translation_settings() != config
                        or not automatic_candidate(message, config)
                        or message_text(message) != text
                        or not self._automatic_translation_enabled(jid, config)):
                    raise DescriptionError("cancelled")
            try:
                provider = credential_id(config)
                key = store_for_translation(CredentialStore(global_dir()), provider).get(provider)
            except CredentialError:
                raise DescriptionError("credentials") from None
            if automatic is not None and (key or config.provider in CUSTOM):
                reserve_automatic_request(self._translation_app(), config)
                if (message_text(message) != text or not self._automatic_translation_enabled(jid, config)):
                    raise DescriptionError("cancelled")
            return translate(config, key, text, token)

        submit(work, lambda answer, error: wx.CallAfter(complete, answer, error))

    def _set_translation_view(self, config):
        previous = getattr(self, "_translation_view_config", None)
        if previous is not None and (
                previous.provider, previous.base_url, previous.model, previous.language
        ) != (config.provider, config.base_url, config.model, config.language):
            getattr(self, "_translation_results", {}).clear()
        self._translation_view_config = config
        self._translation_display = config.display

    def _present_translation(self, jid, key, config, original, answer, *, manual=False):
        self._set_translation_view(config)
        results = self.__dict__.setdefault("_translation_results", OrderedDict())
        results[key] = (config, original, answer)
        results.move_to_end(key)
        if len(results) > _RESULT_LIMIT:
            results.popitem(last=False)
        if config.display == DISPLAY_SEPARATE:
            if manual:
                self._show_translation(answer, original=original)
            return
        active = (self.conversation or {}).get("remoteJid", "")
        if self._translation_chat_key(active) != self._translation_chat_key(jid):
            return
        if answer.strip() == original:
            if manual:
                self.main_window.output(self.main_window.i18n.t("translation_same_language"))
            return
        self.messages_list.Freeze()
        try:
            self._refresh_message_rows_by_ids({key[1]})
        finally:
            self.messages_list.Thaw()
        self._update_read_more_button(self.messages_list.GetFocusedItem())
        if manual:
            self.main_window.output(self.main_window.i18n.t("translation_result").format(text=answer))

    def _show_translation(self, answer, *, original):
        t = self.main_window.i18n.t
        translated_text = answer
        if answer.strip() == original:
            answer = t("translation_same_language") + "\n\n" + answer
        title = t("translation_message")
        dialog = wx.Dialog(self, title=title, size=(540, 320))
        text = wx.TextCtrl(dialog, value=answer, name=title, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(text, 1, wx.EXPAND | wx.ALL, 8)
        copy_button = wx.Button(dialog, label=t("transcription_result_copy"))
        copy_button.Bind(wx.EVT_BUTTON, lambda event: self._copy_translation(translated_text))
        sizer.Add(copy_button, 0, wx.ALL, 8)
        sizer.Add(dialog.CreateSeparatedButtonSizer(wx.OK), 0, wx.EXPAND | wx.ALL, 8)
        dialog.SetSizer(sizer)
        text.SetFocus()
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _copy_translation(self, text):
        copied = False
        if wx.TheClipboard.Open():
            try:
                copied = wx.TheClipboard.SetData(wx.TextDataObject(text))
            finally:
                wx.TheClipboard.Close()
        if copied:
            self.main_window.output(self.main_window.i18n.t("msg_copied"))

    def _maybe_auto_translate(self, jid, message):
        if getattr(self.main_window, "_shutting_down", False):
            return
        text = message_text(message)
        key = message_identity(message)
        if not text or len(text) > MAX_TEXT_CHARS or not key:
            return
        config = self._translation_settings()
        if (not translation_model_ready(config) or not automatic_candidate(message, config)
                or not self._automatic_translation_may_be_enabled(jid, config)):
            return
        state = self.__dict__
        pending = state.setdefault("_translation_queue", deque(maxlen=_QUEUE_LIMIT))
        known = state.setdefault("_translation_results", OrderedDict())
        entry = (jid, key, text, config, message)
        existing = known.get(key)
        if (existing and existing[:2] == (config, text)) or entry in pending or entry == state.get("_translation_running"):
            return
        pending.append(entry)
        if not state.get("_translation_running") and not state.get("_translation_retry"):
            self._drain_translation_queue()

    def _drain_translation_queue(self):
        self._translation_retry = None
        pending = self._translation_queue
        if not pending or getattr(self.main_window, "_shutting_down", False):
            pending.clear()
            return
        entry = jid, key, text, config, message = pending.popleft()
        if (config != self._translation_settings()
                or not automatic_candidate(message, config) or message_text(message) != text
                or not self._automatic_translation_may_be_enabled(jid, config)):
            wx.CallAfter(self._drain_translation_queue)
            return
        self._translation_running = entry

        def complete(answer, error):
            self._translation_running = None
            if (not error and message_text(message) == text and config == self._translation_settings()
                    and self._automatic_translation_enabled(jid, config)):
                self._present_translation(jid, key, config, text, answer)
            self._drain_translation_queue()

        try:
            self._submit_translation(config, text, RequestToken(), complete, automatic=(jid, message))
        except DescriptionError as error:
            self._translation_running = None
            if error.category == "busy":
                pending.appendleft(entry)
                self._translation_retry = wx.CallLater(500, self._drain_translation_queue)
