"""ReadingPositionMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

"Continue where you left off": the message each conversation's list was on
when the user left it, and landing back on it when the conversation is opened
again, if Settings > Interface > Conversations asks for that
(core/reading_position.py). Only the row changes: where keyboard focus goes
on opening is still "focus_on_open".
"""

import logging

from core import reading_position


class ReadingPositionMixin:
    """Remembering and restoring the reading position per conversation."""

    def _reading_positions(self) -> dict:
        """{conversation JID: (message id, records depth)}, for this run."""
        positions = getattr(self, "_reading_position_by_jid", None)
        if positions is None:
            positions = self._reading_position_by_jid = {}
        return positions

    def _remember_reading_position(self) -> None:
        """Note the message the open conversation's list is on, as it is being
        left (closed, or another conversation opened). Always recorded, so
        turning the setting on works from the next open."""
        try:
            conversation = self.conversation
            jid = conversation.get("remoteJid", "") if conversation else ""
            if not jid:
                return
            rows = list(getattr(self, "_sorted_messages", None) or [])
            msg_id = reading_position.resume_message_id(
                rows, self.messages_list.GetFocusedItem())
            if not msg_id:
                return
            records = (((conversation.get("messages") or {}).get("messages") or {})
                       .get("records") or [])
            depth = reading_position.records_depth(records, msg_id)
            self._reading_positions()[jid] = (msg_id, depth)
        except Exception:
            logging.exception("[reading_position] could not remember the position (non-fatal)")

    def _arm_reading_resume(self, jid: str) -> None:
        """Before opening *jid*: what the rebuild should land on, or nothing
        when the setting is off or nothing was remembered."""
        self._pending_reading_resume_id = ""
        self._pending_reading_resume_depth = 0
        if not jid or not reading_position.resumes_last_focused(self.main_window.settings):
            return
        remembered = self._reading_positions().get(jid)
        if remembered:
            self._pending_reading_resume_id, self._pending_reading_resume_depth = remembered

    def _disarm_reading_resume(self) -> None:
        self._pending_reading_resume_id = ""
        self._pending_reading_resume_depth = 0

    def _reading_resume_fetch_limit(self, base_limit: int, unread_count: int) -> int:
        """The database fetch for opening a conversation, widened so it still
        reaches the remembered message."""
        return reading_position.resume_fetch_limit(
            base_limit, getattr(self, "_pending_reading_resume_depth", 0), unread_count)

    def _reading_resume_anchor(self) -> str:
        """The message the rebuild's window must keep in view, or ""."""
        return getattr(self, "_pending_reading_resume_id", "") or ""

    def _take_reading_resume_row(self) -> int:
        """The row the opening rebuild lands on instead of the separator or
        the last message, or -1 (consumed: a later rebuild lands as usual)."""
        msg_id = self._reading_resume_anchor()
        self._disarm_reading_resume()
        if not msg_id:
            return -1
        return self._find_index_by_msg_id(msg_id)

    def _reading_resume_row_for_open_conversation(self) -> int:
        """For the conversation that is already open: the message row the list
        cursor is on, when resuming is on and it is a message; else -1."""
        if not reading_position.resumes_last_focused(self.main_window.settings):
            return -1
        rows = getattr(self, "_sorted_messages", None) or []
        msg_id = reading_position.resume_message_id(
            list(rows), self.messages_list.GetFocusedItem())
        return self._find_index_by_msg_id(msg_id) if msg_id else -1
