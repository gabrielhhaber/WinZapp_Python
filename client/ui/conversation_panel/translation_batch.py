"""Translate the messages already in the open chat (issue #793).

Automatic translation reaches only messages that arrive after it is turned on.
The message menu adds "Translate from here to the end" and "Translate the
selected messages": received text messages, one request each through
"Translate message"'s provider, model and display mode
(translation_actions._submit_translation). With "replace" or "append" each
translation lands in its own line as it arrives; with "separate window" they
are shown together at the end. At most BATCH_LIMIT messages per batch.
"""
from collections import deque
from dataclasses import replace

import wx

from core.ai_media.errors import DescriptionError
from core.ai_media.service import RequestToken
from core.message_copy_format import format_copied_message
from core.message_translation import DISPLAY_SEPARATE, message_identity, message_text, translation_model_ready
from core.translation_batch import batch_candidates, says_progress
from ui.dialogs.translation_settings import LABELS

_BUSY_RETRY_MS = 1000


class TranslationBatchMixin:
    # ── Menu items ───────────────────────────────────────────────────────

    def _append_translation_batch_items(self, menu, msg):
        """Message-menu items: from here to the end, the selection, or Cancel
        while a batch runs. Offered only while translation is set up."""
        t = self.main_window.i18n.t
        self._drop_stale_translation_batch()
        if getattr(self, "_translation_batch", None) is not None:
            item = menu.Append(wx.ID_ANY, t("translation_batch_cancel_menu"))
            self.Bind(wx.EVT_MENU, lambda event: self._cancel_translation_batch(), item)
            return
        if not self._translation_settings().provider:
            return
        if batch_candidates(self._translation_batch_from(msg))[0]:
            item = menu.Append(wx.ID_ANY, t("translation_batch_from_here_menu"))
            self.Bind(wx.EVT_MENU, lambda event: self._on_translate_batch(
                self._translation_batch_from(msg)), item)
        if self.selected_messages and batch_candidates(self._translation_batch_selected())[0]:
            item = menu.Append(wx.ID_ANY, t("translation_batch_selected_menu"))
            self.Bind(wx.EVT_MENU, lambda event: self._on_translate_batch(
                self._translation_batch_selected()), item)

    def _translation_batch_from(self, msg):
        """Messages from ``msg`` to the newest one, in list order."""
        key = (msg.get("key") or {}).get("id")
        rows = self._sorted_messages
        start = next((n for n, row in enumerate(rows)
                      if not self._is_separator(row) and (row.get("key") or {}).get("id") == key), None)
        return [] if start is None else [row for row in rows[start:] if not self._is_separator(row)]

    def _translation_batch_selected(self):
        return [row for row in self._sorted_messages
                if not self._is_separator(row) and (row.get("key") or {}).get("id") in self.selected_messages]

    # ── Running a batch ──────────────────────────────────────────────────

    def _on_translate_batch(self, messages):
        mw = self.main_window
        t = mw.i18n.t
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
        rows, left_out = batch_candidates(messages)
        self._drop_stale_translation_batch()
        if not rows:
            mw.output(t("translation_batch_nothing"))
            return
        if getattr(self, "_translation_batch", None) is not None:
            mw.output(t("ai_error_busy"))
            return
        provider = LABELS.get(config.provider, config.provider)
        # A locked chat's text leaves WhatsApp only with fresh consent, as
        # "Translate message" asks (translation_locked_confirm).
        if mw.is_chat_locked(jid):
            answer = wx.MessageBox(
                t("translation_locked_confirm").format(provider=provider), t("translation_message"),
                wx.YES_NO | wx.NO_DEFAULT, self)
            if (answer != wx.YES or self._translation_chat_hidden(jid)
                    or (self.conversation or {}).get("remoteJid") != jid):
                return
        self._translation_batch = {
            "jid": jid, "config": config, "pending": deque(rows), "total": len(rows),
            "done": 0, "shown": [], "token": None, "retry": None,
        }
        spoken = t("translation_batch_sending").format(provider=provider, count=len(rows))
        if left_out:
            spoken += " " + t("translation_batch_left_out").format(left_out=left_out)
        mw.output(spoken)
        self._translation_batch_next()

    def _translation_batch_current(self, state):
        """Whether ``state`` is still the running batch and its chat, vault and
        settings are as they were when it started."""
        jid = state["jid"]
        return (getattr(self, "_translation_batch", None) is state
                and not getattr(self.main_window, "_shutting_down", False)
                and (self.conversation or {}).get("remoteJid") == jid
                and not self._translation_chat_hidden(jid)
                and self._translation_settings() == state["config"])

    def _translation_batch_next(self):
        state = getattr(self, "_translation_batch", None)
        if state is None:
            return
        state["retry"] = None
        if not self._translation_batch_current(state):
            self._translation_batch = None
            return
        config = state["config"]
        results = getattr(self, "_translation_results", {})
        while state["pending"]:
            message = state["pending"][0]
            text = message_text(message)
            key = message_identity(message)
            known = results.get(key)
            # Display is not part of the match: a translation stays valid when
            # only how it is shown changed ("Translate message" does the same).
            if not (known and replace(known[0], display=config.display) == config and known[1] == text):
                break
            # Already translated with these settings: no second request.
            state["pending"].popleft()
            self._translation_batch_landed(state, message, key, text, known[2])
        if not state["pending"]:
            self._finish_translation_batch(state)
            return
        message = state["pending"][0]
        text = message_text(message)
        key = message_identity(message)
        token = state["token"] = RequestToken()

        def complete(answer, error):
            if getattr(self, "_translation_batch", None) is not state or state["token"] is not token:
                return
            state["token"] = None
            if not self._translation_batch_current(state):
                self._translation_batch = None
                return
            if error:
                self._translation_batch = None
                self.main_window.output(self.main_window.i18n.t("translation_batch_failed").format(
                    error=self.main_window.i18n.t(error), done=state["done"]))
                return
            state["pending"].popleft()
            current = next((row for row in self._sorted_messages if message_identity(row) == key), message)
            if message_text(current) == text:
                self._translation_batch_landed(state, current, key, text, answer)
            self._translation_batch_next()

        try:
            self._submit_translation(config, text, token, complete)
        except DescriptionError as error:
            state["token"] = None
            if error.category == "busy":
                # Another AI request holds both slots: this message waits.
                state["retry"] = wx.CallLater(_BUSY_RETRY_MS, self._translation_batch_next)
            else:
                self._translation_batch = None
                t = self.main_window.i18n.t
                self.main_window.output(t("translation_batch_failed").format(error=t(error.key), done=state["done"]))

    def _translation_batch_landed(self, state, message, key, text, answer):
        state["done"] += 1
        self._present_translation(state["jid"], key, state["config"], text, answer, message=message)
        if state["config"].display == DISPLAY_SEPARATE:
            state["shown"].append(format_copied_message(
                self._extract_timestamp(message), self._sender_label(message),
                self._translation_for_reading(message, answer), self.main_window.i18n.t("datetime_fmt")))
        if says_progress(state["done"], state["total"]):
            self.main_window.output(self.main_window.i18n.t("translation_batch_progress").format(
                done=state["done"], total=state["total"]))

    def _finish_translation_batch(self, state):
        self._translation_batch = None
        t = self.main_window.i18n.t
        if state["shown"]:
            self._show_translation("\n\n".join(state["shown"]), original="", same=False, jid=state["jid"])
        else:
            self.main_window.output(t("translation_batch_done").format(count=state["done"]))

    def _drop_stale_translation_batch(self):
        """A batch whose chat was left, vault closed or settings changed ends
        now, not when its request in flight comes back (up to a minute)."""
        state = getattr(self, "_translation_batch", None)
        if state is not None and not self._translation_batch_current(state):
            self._cancel_translation_batch(quiet=True)

    def _cancel_translation_batch(self, *, locked_only=False, quiet=False):
        """Stop the running batch. Translations already shown stay."""
        state = getattr(self, "_translation_batch", None)
        if state is None or (locked_only and not self.main_window.is_chat_locked(state["jid"])):
            return
        self._translation_batch = None
        if state["token"] is not None:
            state["token"].cancel()
        if state["retry"] is not None:
            state["retry"].Stop()
        if not quiet:
            self.main_window.output(self.main_window.i18n.t("translation_batch_cancelled").format(
                done=state["done"]))
