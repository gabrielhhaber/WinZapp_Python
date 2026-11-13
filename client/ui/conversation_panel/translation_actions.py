"""Keyboard, chat opt-in and live message translation for ConversationsPanel."""
from collections import OrderedDict, deque
from dataclasses import replace
from sqlite3 import Error as SQLiteError
import time

import wx

from app_paths import global_dir
from app_settings import AppSettings
from core.ai_credentials import CredentialError, CredentialStore, store_for_translation
from core.ai_media.auto import day_key
from core.ai_media.errors import DescriptionError
from core.ai_media.service import RequestToken, submit
from core.message_translation import (
    CUSTOM, DISPLAY_SEPARATE, MAX_TEXT_CHARS, PAUSE_SECONDS, TranslationSettings,
    automatic_candidate, chat_allows_automatic, credential_id, message_identity, message_text,
    pauses_automatic, preferences, reserve_automatic_request, translate, translated_body,
    translation_model_ready,
)
from core.utils import link_preview_text
from ui.dialogs.translation_settings import LABELS
from ui.shortcut_bindings import popup_item_label

_OPT_IN = "automatic_text_translation_chats"
_RESULT_LIMIT = 128
_QUEUE_LIMIT = 50
_BUSY_RETRY_MS = 1000


class TranslationActionsMixin:
    def _translation_app(self):
        return getattr(self.main_window, "_app_settings", None) or AppSettings(global_dir())

    def _translation_settings(self):
        """The saved translation settings, read without waiting on the lock.

        Another thread or account process holds the settings lock for a few
        milliseconds while it writes; the last settings read stand in for
        this one then. Falling back to empty settings said "not configured"
        and threw away translations that had already been paid for."""
        app = self._translation_app()
        try:
            config = preferences(app, snapshot=True)
        except (OSError, RuntimeError):
            last = getattr(self, "_translation_last_settings", None)
            if last is not None:
                return last
            try:
                config = preferences(app)
            except (OSError, RuntimeError):
                return TranslationSettings()
        self._translation_last_settings = config
        return config

    def _translation_chats(self):
        if not hasattr(self, "_translation_opt_in"):
            saved = self.main_window.db.get_metadata_json(_OPT_IN, {})
            self.__dict__.setdefault("_translation_opt_in", saved if isinstance(saved, dict) else {})
        return self._translation_opt_in

    def _translation_chat_key(self, jid):
        mw = self.main_window
        jid = mw._normalize_jid(jid)
        return getattr(mw, "_lid_to_phone", {}).get(jid) or jid

    def _translation_chat_hidden(self, jid):
        """A locked chat while the vault is closed: its text stays here. The
        rule of the AI media action (ai_actions._on_ai_action)."""
        mw = self.main_window
        return mw.is_chat_locked(jid) and not getattr(mw, "_chat_lock_unlocked", False)

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

    def _translation_menu_label(self):
        return popup_item_label(self, self.main_window.i18n.t("translation_message"),
                                "message_list.translate")

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
        mw.output(t("translation_auto_chat_enabled" if not enabled else "translation_auto_chat_disabled"))
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
        t = mw.i18n.t
        text = message_text(message)
        if not text:
            return
        config = self._translation_settings()
        if not config.provider:
            mw.output(t("translation_not_configured"))
            return
        if not translation_model_ready(config):
            mw.output(t("translation_test_required"))
            return
        jid = (self.conversation or {}).get("remoteJid", "")
        if not jid or self._translation_chat_hidden(jid):
            return
        key = message_identity(message)
        if not key:
            return
        result = getattr(self, "_translation_results", {}).get(key)
        if result and result[1] == text and replace(result[0], display=config.display) == config:
            self._present_translation(jid, key, config, text, result[2], message=message, manual=True)
            return
        if getattr(self, "_manual_translation", None) is not None:
            mw.output(t("ai_error_busy"))
            return
        provider = LABELS.get(config.provider, config.provider)
        # A locked chat's text leaves WhatsApp only with fresh consent, asked
        # each time like the AI media window does (ai_locked_consent).
        if mw.is_chat_locked(jid):
            answer = wx.MessageBox(
                t("translation_locked_confirm").format(provider=provider), t("translation_message"),
                wx.YES_NO | wx.NO_DEFAULT, self)
            # The vault's timer fires inside the box's modal loop too: a Yes
            # given to a chat that closed meanwhile sends nothing.
            if (answer != wx.YES or self._translation_chat_hidden(jid)
                    or (self.conversation or {}).get("remoteJid") != jid):
                return
        token = self._manual_translation = RequestToken()

        def complete(answer, error):
            self._manual_translation = None
            if (getattr(mw, "_shutting_down", False)
                    or (self.conversation or {}).get("remoteJid") != jid
                    or self._translation_chat_hidden(jid)
                    or self._translation_settings() != config):
                return
            current = next((row for row in self._sorted_messages if message_identity(row) == key), None)
            if current is None or message_text(current) != text:
                return
            if error:
                mw.output(t(error))
            else:
                self._present_translation(jid, key, config, text, answer, message=current, manual=True)

        try:
            self._submit_translation(config, text, token, complete)
        except DescriptionError as error:
            complete(None, error.key)
        else:
            mw.output(t("translation_sending").format(provider=provider))

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

    def resume_automatic_translation(self):
        """The AI settings were applied: what paused automatic translation
        (a key, a quota, the provider) may have been fixed there."""
        self._translation_paused_until = 0.0

    def drop_locked_translations(self):
        """The vault closed: no translation of a locked chat stays in memory
        or on screen."""
        mw = self.main_window
        results = getattr(self, "_translation_results", None)
        if results:
            for key in [key for key in results if mw.is_chat_locked(key[0])]:
                del results[key]
        pending = getattr(self, "_translation_queue", None)
        if pending:
            kept = [entry for entry in pending if not mw.is_chat_locked(entry[0])]
            pending.clear()
            pending.extend(kept)
        dialog = getattr(self, "_translation_dialog", None)
        if dialog is not None and mw.is_chat_locked(dialog[1]):
            dialog[0].EndModal(wx.ID_CANCEL)

    def _translation_for_reading(self, message, answer):
        """The answer as the row reads it: @mentions as names, never digits."""
        return self._resolve_mentions_in_text(answer, self._message_mentioned_jids(message))

    def _translated_row_body(self, msg, body):
        """The row text of *msg* with its cached translation, if any."""
        results = getattr(self, "_translation_results", None)
        if not results:
            return body
        framed = None
        if msg.get("messageType") == "extendedTextMessage":
            ext = (msg.get("message") or {}).get("extendedTextMessage") or {}
            framed = lambda text: link_preview_text(ext, text, self.main_window)  # noqa: E731
        return translated_body(
            body, results, msg, self.main_window.i18n.t, getattr(self, "_translation_display", None),
            shown=lambda text: self._translation_for_reading(msg, text), framed=framed)

    def _present_translation(self, jid, key, config, original, answer, *, message, manual=False):
        self._set_translation_view(config)
        results = self.__dict__.setdefault("_translation_results", OrderedDict())
        results[key] = (config, original, answer)
        results.move_to_end(key)
        if len(results) > _RESULT_LIMIT:
            results.popitem(last=False)
        shown = self._translation_for_reading(message, answer)
        if config.display == DISPLAY_SEPARATE:
            if manual:
                self._show_translation(shown, original=original, same=answer.strip() == original,
                                       jid=jid)
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
            self.main_window.output(self.main_window.i18n.t("translation_result").format(text=shown))

    def _show_translation(self, answer, *, original, same, jid):
        t = self.main_window.i18n.t
        translated_text = answer
        if same:
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
        self._translation_dialog = (dialog, jid)
        try:
            dialog.ShowModal()
        finally:
            self._translation_dialog = None
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

    def _automatic_translation_paused(self):
        return time.monotonic() < getattr(self, "_translation_paused_until", 0.0)

    def _maybe_auto_translate(self, jid, message):
        if getattr(self.main_window, "_shutting_down", False) or self._automatic_translation_paused():
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

    def _automatic_translation_failed(self, error, config):
        """Say once why automatic translation stopped: today's limit, or a
        provider refusal that would repeat for every message (paused for an
        hour, like automatic AI media)."""
        mw = self.main_window
        t = mw.i18n.t
        if str(error) == "ai_error_limit":
            today = day_key()
            if getattr(self, "_translation_limit_said_on", None) != today:
                self._translation_limit_said_on = today
                mw.output(t("translation_auto_limit_reached").format(limit=config.daily_limit))
        elif pauses_automatic(error):
            self._translation_paused_until = time.monotonic() + PAUSE_SECONDS
            self._translation_queue.clear()
            mw.output(t("translation_auto_paused"))

    def _next_translation_entry(self):
        """The next queued message still worth translating, or None. Stale
        entries are skipped here, in one go: deferring each skip let a new
        message start a second request beside the queue's own."""
        pending = self._translation_queue
        while pending:
            entry = jid, _key, text, config, message = pending.popleft()
            if (config == self._translation_settings()
                    and automatic_candidate(message, config) and message_text(message) == text
                    and self._automatic_translation_may_be_enabled(jid, config)):
                return entry
        return None

    def _drain_translation_queue(self):
        self._translation_retry = None
        pending = self._translation_queue
        if getattr(self.main_window, "_shutting_down", False) or self._automatic_translation_paused():
            pending.clear()
            return
        entry = self._next_translation_entry()
        if entry is None:
            return
        jid, key, text, config, message = entry
        self._translation_running = entry

        def complete(answer, error):
            self._translation_running = None
            if error:
                self._automatic_translation_failed(error, config)
            elif (message_text(message) == text and config == self._translation_settings()
                    and self._automatic_translation_enabled(jid, config)):
                self._present_translation(jid, key, config, text, answer, message=message)
            self._drain_translation_queue()

        try:
            self._submit_translation(config, text, RequestToken(), complete, automatic=(jid, message))
        except DescriptionError as error:
            self._translation_running = None
            if error.category == "busy":
                # Another AI request holds both slots; this message waits.
                pending.appendleft(entry)
                self._translation_retry = wx.CallLater(_BUSY_RETRY_MS, self._drain_translation_queue)
