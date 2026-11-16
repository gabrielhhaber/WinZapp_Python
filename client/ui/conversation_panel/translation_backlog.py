"""Translating what a chat already holds, for ConversationsPanel.

Automatic translation only looks at what arrives. Two ways reach the messages
that were there before (issue #793), like the bulk forms of transcription:

* turning automatic translation on for a chat translates its latest received
  messages at once, whatever their age (the queue of translation_actions does
  the work and its daily limit still counts);
* "Translate selected messages", in the bulk actions, translates the selected
  text messages one after another and says the count at the end.

Nothing here logs: the texts are the content of private conversations.
"""
from dataclasses import replace

import wx

from core.ai_media.errors import DescriptionError
from core.ai_media.service import RequestToken
from core.message_translation import (
    DISPLAY_SEPARATE, MAX_TEXT_CHARS, backlog_messages, message_identity, message_text, translation_model_ready,
)
from ui.dialogs.translation_settings import LABELS

_MAX_SELECTED = 50
_REMEMBERED_KEYS = 500


def selected_texts(messages, selected_ids, is_separator=None, limit=_MAX_SELECTED):
    """The selected messages that hold text to translate, oldest first."""
    wanted = set(selected_ids or ())
    rows = []
    for message in messages or ():
        if not isinstance(message, dict) or (is_separator and is_separator(message)):
            continue
        text = message_text(message)
        if ((message.get("key") or {}).get("id") in wanted and text
                and len(text) <= MAX_TEXT_CHARS and message_identity(message)):
            rows.append(message)
    return rows[:limit]


class TranslationBacklogMixin:
    def _translate_chat_backlog(self, jid):
        """Automatic translation was just turned on for *jid*: translate its
        latest received messages, however old."""
        active = (self.conversation or {}).get("remoteJid")
        if not active or self._translation_chat_key(jid) != self._translation_chat_key(active):
            return
        config = self._translation_settings()
        if not translation_model_ready(config):
            return
        rows = backlog_messages(self._sorted_messages, config, is_separator=self._is_separator)
        keys = self.__dict__.setdefault("_translation_backlog_keys", set())
        if len(keys) > _REMEMBERED_KEYS:
            keys.clear()
        keys.update(message_identity(row) for row in rows)
        for row in rows:
            self._maybe_auto_translate(active, row)

    def _on_mass_translate_messages(self, event=None):
        self._start_bulk_translation(
            selected_texts(self._sorted_messages, self.selected_messages, self._is_separator))

    def _start_bulk_translation(self, rows):
        """Check the settings, confirm, then translate *rows* one after another
        (the run's steps below). Shared by the selection and "from here"."""
        mw = self.main_window
        t = mw.i18n.t
        config = self._translation_settings()
        if not config.provider:
            mw.output(t("translation_not_configured"), interrupt=True)
            return
        if not translation_model_ready(config):
            mw.output(t("translation_test_required"), interrupt=True)
            return
        jid = (self.conversation or {}).get("remoteJid", "")
        if not jid or self._translation_chat_hidden(jid):
            return
        if getattr(self, "_bulk_translation", None) is not None or getattr(
                self, "_manual_translation", None) is not None:
            mw.output(t("ai_error_busy"), interrupt=True)
            return
        if not rows:
            mw.output(t("translation_bulk_none"), interrupt=True)
            return
        provider = LABELS.get(config.provider, config.provider)
        prompt = t("translation_bulk_confirm").format(count=len(rows), provider=provider)
        if wx.MessageBox(prompt, t("translation_message"), wx.YES_NO | wx.NO_DEFAULT, self) != wx.YES:
            return
        # The vault's timer fires inside the box's modal loop too.
        if self._translation_chat_hidden(jid) or (self.conversation or {}).get("remoteJid") != jid:
            return
        run = self._bulk_translation = {"jid": jid, "rows": rows, "done": 0, "config": config, "texts": []}
        mw.output(t("translation_sending").format(provider=provider))
        self._bulk_translation_step(run)

    def _bulk_translation_finish(self, run):
        if getattr(self, "_bulk_translation", None) is run:
            self._bulk_translation = None
        self.main_window.output(
            self.main_window.i18n.t("translation_bulk_done").format(count=run["done"]))
        # A separate window is this display's way of reading: one for the run.
        if run["config"].display == DISPLAY_SEPARATE and run["texts"]:
            self._show_translation("\n\n".join(run["texts"]), original="", same=False,
                                   jid=run["jid"])

    def _bulk_translation_step(self, run):
        if getattr(self, "_bulk_translation", None) is not run:
            return
        mw = self.main_window
        jid = run["jid"]
        if (getattr(mw, "_shutting_down", False) or self._translation_chat_hidden(jid)
                or (self.conversation or {}).get("remoteJid") != jid
                or self._translation_settings() != run["config"]):
            self._bulk_translation = None
            return
        if not run["rows"]:
            self._bulk_translation_finish(run)
            return
        message = run["rows"].pop(0)
        text, key, config = message_text(message), message_identity(message), run["config"]
        cached = getattr(self, "_translation_results", {}).get(key)
        if cached and cached[1] == text and replace(cached[0], display=config.display) == config:
            run["done"] += 1
            run["texts"].append(self._translation_for_reading(message, cached[2]))
            self._bulk_translation_step(run)
            return

        def complete(answer, error):
            if getattr(self, "_bulk_translation", None) is not run:
                return
            if error:
                # The next message would fail the same way: stop and say why.
                self._bulk_translation = None
                mw.output(mw.i18n.t(error))
                return
            self._present_translation(jid, key, config, text, answer, message=message)
            run["done"] += 1
            run["texts"].append(self._translation_for_reading(message, answer))
            self._bulk_translation_step(run)

        try:
            self._submit_translation(config, text, RequestToken(), complete)
        except DescriptionError as error:
            complete(None, error.key)
