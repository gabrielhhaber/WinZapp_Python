"""Translating from a message to the end of the chat, and cancelling a run.

Complements the bulk "Translate the selected messages" of translation_backlog.py
(issue #793): the message context menu offers the received text messages from
the focused one down to the newest, through the same confirmed, one-request-at-
a-time run. While a run is going the menu offers to cancel it instead.

Nothing here logs: the texts are the content of private conversations.
"""
import wx

from core.message_translation import MAX_TEXT_CHARS, message_identity, message_text
from ui.conversation_panel.translation_backlog import _MAX_SELECTED
from ui.shortcut_bindings import popup_item_label


def messages_from_here(messages, start, is_separator=None, limit=_MAX_SELECTED):
    """The received text messages from *start* (included) to the end, oldest
    first, at most *limit*. Own messages are already in the person's language."""
    start_id = (start.get("key") or {}).get("id") if isinstance(start, dict) else None
    if not start_id:
        return []
    rows, started = [], False
    for message in messages or ():
        if not isinstance(message, dict) or (is_separator and is_separator(message)):
            continue
        started = started or (message.get("key") or {}).get("id") == start_id
        if not started or (message.get("key") or {}).get("fromMe"):
            continue
        text = message_text(message)
        if text and len(text) <= MAX_TEXT_CHARS and message_identity(message):
            rows.append(message)
    return rows[:limit]


class TranslationToEndMixin:
    def _append_translation_to_end_items(self, menu, msg):
        """The context-menu entry: translate from this message, or cancel."""
        t = self.main_window.i18n.t
        if getattr(self, "_bulk_translation", None) is not None:
            item = menu.Append(wx.ID_ANY, popup_item_label(
                self, t("translation_bulk_cancel_menu"), "messages.ID_TRANSLATE_FROM_HERE"))
            self.Bind(wx.EVT_MENU, lambda event: self._cancel_bulk_translation(), item)
        elif message_text(msg):
            item = menu.Append(wx.ID_ANY, popup_item_label(
                self, t("translate_from_here"), "messages.ID_TRANSLATE_FROM_HERE"))
            self.Bind(wx.EVT_MENU, lambda event: self._on_translate_from_here(msg), item)

    def _on_translate_from_here(self, msg):
        self._start_bulk_translation(
            messages_from_here(self._sorted_messages, msg, self._is_separator))

    def _message_at_selection(self):
        index = self.messages_list.GetFirstSelected()
        rows = self._sorted_messages
        if 0 <= index < len(rows) and not self._is_separator(rows[index]):
            return rows[index]
        return None

    def _on_translate_from_here_shortcut(self, event=None):
        """Alt+Shift+Y: the "from here" menu item for the focused message, or
        the run's cancellation while one is going (as its menu item does)."""
        if getattr(self, "_bulk_translation", None) is not None:
            self._cancel_bulk_translation()
            return
        self._on_translate_from_here(self._message_at_selection())

    def _on_translate_selected_shortcut(self, event=None):
        """Ctrl+Alt+Shift+Y: translate the selected messages, or cancel the run."""
        if getattr(self, "_bulk_translation", None) is not None:
            self._cancel_bulk_translation()
            return
        self._on_mass_translate_messages()

    def _cancel_bulk_translation(self):
        if getattr(self, "_bulk_translation", None) is None:
            return
        self._bulk_translation = None
        self.main_window.output(self.main_window.i18n.t("translation_bulk_cancelled"), interrupt=True)
