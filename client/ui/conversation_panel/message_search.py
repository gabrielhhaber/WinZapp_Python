"""MessageSearchMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

from ui.shortcut_bindings import command_key_event
import wx
from core.call_log import is_call_log
from core.utils import normalize_for_search


class MessageSearchMixin:
    """Searching inside the open conversation (Ctrl+Shift+F).
    """

    # ── Ctrl+Shift+F: search in conversation ───────────────────────────────

    def _on_accel_open_search(self, event):
        self._on_open_search(event)

    def _on_open_search(self, event):
        self._search_panel.Show()
        self._search_open_btn.Hide()
        self.conversation_panel.Layout()
        self._search_field.SetFocus()

    def _on_close_search(self, event):
        self._search_panel.Hide()
        self._search_open_btn.Show()
        self._search_results = []
        self._search_result_idx = -1
        self._search_field.SetValue("")
        self.conversation_panel.Layout()
        self.messages_list.SetFocus()

    def _message_search_text(self, msg) -> str:
        """The part of a message row that searching should actually look at.

        Not the rendered row: _render_message_line() also appends delivery
        status, the timestamp, "Editada"/"Encaminhada", and the reaction
        summary. Searching that string made every decoration a false match —
        reported live for "reproduz", which hit every played voice message
        through its "Reproduzida" status, but "editada", "encaminhada", a
        date, or a reaction label would all have done the same.

        What stays is what a user would call content: who wrote it, the text
        or media description itself, and — when the message is a reply — who
        was quoted and what the quote says. That last part is deliberate: a
        reply can quote a message that scrolled out of the loaded history, so
        the quote is sometimes the only copy of those words in the list.
        """
        parts = []
        if not self._is_system_event(msg) or is_call_log(msg):
            parts.append(self._sender_label(msg))
        parts.append(self._get_message_content(msg) or "")
        ctx = self._get_context_info(msg)
        if ctx:
            # The bare name, never the "respondendo a {name}" phrasing around
            # it — that wording is decoration and would match on its own.
            parts.append(self._get_quoted_sender(ctx, msg) or "")
            parts.append(self._get_quoted_preview(ctx.get("quotedMessage") or {}) or "")
        return " ".join(p for p in parts if p)

    def _on_search_text_changed(self, event):
        query = self._search_field.GetValue()
        if not query.strip():
            self._search_results = []
            self._search_result_idx = -1
            return
        # Read the setting per search, not once at startup: changing it in
        # Settings takes effect on the very next keystroke.
        fold = self.main_window._search_normalization_mode()
        qlow = normalize_for_search(query, fold)
        # Store message IDs, not raw row indices: _sorted_messages can be
        # mutated (a new message arrives, more history is paginated in, a
        # message is deleted) between when the query runs and when the user
        # actually jumps to a result, which silently sent "next result" to
        # whatever unrelated row now sits at that same index. Messages with
        # no id (essentially never, in practice) are skipped rather than
        # matched by an ambiguous empty key.
        self._search_results = [
            msg.get("key", {}).get("id", "")
            for msg in self._sorted_messages
            if not self._is_separator(msg)
            and msg.get("key", {}).get("id")
            and qlow in normalize_for_search(self._message_search_text(msg), fold)
        ]
        self._search_result_idx = -1

    def _on_search_key_down(self, event):
        event = command_key_event(self, 'search', event)
        key   = event.GetKeyCode()
        shift = event.ShiftDown()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if shift:
                self._on_search_prev(None)
            else:
                self._on_search_next(None)
        else:
            event.Skip()

    def _on_search_next(self, event):
        i18n = self.main_window.i18n
        if not self._search_results:
            self.main_window.output(i18n.t("search_no_results"), interrupt=True)
            return
        self._search_result_idx = (self._search_result_idx + 1) % len(self._search_results)
        self._jump_to_search_result()

    def _on_search_prev(self, event):
        i18n = self.main_window.i18n
        if not self._search_results:
            self.main_window.output(i18n.t("search_no_results"), interrupt=True)
            return
        self._search_result_idx = (self._search_result_idx - 1) % len(self._search_results)
        self._jump_to_search_result()

    def _jump_to_search_result(self):
        i18n = self.main_window.i18n
        idx = -1
        # Resolve the stored message ID to its CURRENT row. Drop any result
        # whose message is no longer present (paginated out, deleted) instead
        # of silently focusing whatever unrelated row now sits at a stale
        # index.
        while self._search_results:
            if not (0 <= self._search_result_idx < len(self._search_results)):
                self._search_result_idx = 0
            msg_id = self._search_results[self._search_result_idx]
            idx = next(
                (i for i, m in enumerate(self._sorted_messages)
                 if m.get("key", {}).get("id") == msg_id),
                -1,
            )
            if idx >= 0:
                break
            del self._search_results[self._search_result_idx]
            idx = -1

        if idx < 0:
            self.main_window.output(i18n.t("search_no_results"), interrupt=True)
            return

        total = len(self._search_results)
        self.messages_list.Focus(idx)
        self.messages_list.Select(idx, True)
        self.messages_list.EnsureVisible(idx)
        ann = i18n.t("search_result").format(
            current=self._search_result_idx + 1,
            total=total,
        )
        self.main_window.output(ann, interrupt=True)
