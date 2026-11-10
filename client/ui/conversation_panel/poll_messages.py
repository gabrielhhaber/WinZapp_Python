"""PollMessagesMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Attaching a poll (attachment menu → NewPollDialog → a pending row + the send
queue) and opening a received poll (Enter on the row → PollDialog). The poll
rules themselves — validation, option counts, the spoken row — are core/polls.py.
"""

import threading
import time
import uuid

import wx
from ui.conversation_panel.channel_conversation import refuse_in_channel

from core.message_queue import PendingMessage
from core.polls import POLL_MESSAGE_TYPES, apply_vote_result, poll_content
from ui.conversation_panel.typing_row import append_message_row, message_row_count


class PollMessagesMixin:
    """Sending polls and showing a poll's options."""

    def _on_attach_poll(self, event=None):
        if refuse_in_channel(self, "poll"):
            return
        from ui.dialogs.new_poll_dialog import NewPollDialog

        remote_jid = (self.conversation or {}).get("remoteJid", "")
        if not remote_jid:
            return
        dlg = NewPollDialog(self.main_window)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            question, options, selectable = dlg.question, dlg.options, dlg.selectable
        finally:
            dlg.Destroy()

        local_id = str(uuid.uuid4())
        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            "key": {"id": local_id, "fromMe": True, "remoteJid": remote_jid},
            "messageType": "pollCreationMessage",
            "message": {
                "pollCreationMessage": {
                    "name": question,
                    "options": [{"name": opt} for opt in options],
                    "selectableOptionsCount": selectable,
                }
            },
            "messageTimestamp": int(time.time()),
            "pushName": "",
        }
        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        append_message_row(self, self._render_message_line(virtual_msg))
        last = message_row_count(self) - 1   # the row just sent, not the typing row
        if last >= 0:
            self.messages_list.EnsureVisible(last)
        pm = PendingMessage(
            local_id, remote_jid,
            poll={"name": question, "options": options, "selectableCount": selectable},
        )
        self.main_window.message_queue.enqueue(pm)
        self.main_window.mark_conversation_as_read(remote_jid)

        self._register_virtual_msg(virtual_msg)
        self.main_window._schedule_set_chats()

    def _show_poll_dialog(self, msg: dict) -> None:
        """Enter on a poll row: the question and every option with its votes."""
        from ui.dialogs.poll_dialog import PollDialog

        self.refresh_poll_votes(force=True)
        dlg = PollDialog(self.main_window, msg, self._send_poll_vote, self._poll_voter_name)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def _poll_voter_name(self, sender: str) -> str:
        """The name a voter is shown under: the same resolution as the sender
        of a message in this conversation (contact, @lid bridge, push name,
        formatted number), never a raw id. In a private chat the only other
        person is the chat's contact."""
        remote_jid = (self.conversation or {}).get("remoteJid", "")
        in_group = remote_jid.endswith("@g.us")
        return self._sender_label({
            "key": {
                "fromMe": False, "remoteJid": remote_jid,
                "participant": sender if in_group else "",
            },
        })

    # ── Votes ───────────────────────────────────────────────────────────────

    #: Seconds between two vote lookups; the open conversation's own refresh
    #: calls refresh_poll_votes() far more often than votes need asking for.
    _POLL_VOTES_MIN_INTERVAL = 10
    _POLL_VOTES_MAX_POLLS = 10

    def _open_poll_records(self) -> list:
        """The newest confirmed poll records of the open conversation."""
        container = (self.conversation or {}).get("messages")
        inner = container.get("messages") if isinstance(container, dict) else None
        records = inner.get("records") if isinstance(inner, dict) else None
        polls = [
            rec for rec in (records or [])
            if isinstance(rec, dict)
            and rec.get("messageType") in POLL_MESSAGE_TYPES
            and not rec.get("_local_pending")
            and (rec.get("key") or {}).get("id")
        ]
        return polls[-self._POLL_VOTES_MAX_POLLS:]

    def refresh_poll_votes(self, force: bool = False) -> None:
        """Ask WhatsApp Web for the votes of the open conversation's polls, in
        the background; the rows repaint only if a count changed."""
        if getattr(self, "_poll_votes_busy", False):
            return
        if not force and time.monotonic() - getattr(self, "_poll_votes_at", 0.0) < self._POLL_VOTES_MIN_INTERVAL:
            return
        records = self._open_poll_records()
        remote_jid = (self.conversation or {}).get("remoteJid", "")
        if not records or not remote_jid:
            return
        self._poll_votes_busy = True
        self._poll_votes_at = time.monotonic()
        threading.Thread(
            target=self._poll_votes_worker, args=(remote_jid, records), daemon=True
        ).start()

    def _poll_votes_worker(self, remote_jid: str, records: list) -> None:
        try:
            result = self.main_window.fetch_poll_votes(records)
        except Exception:
            result = {}
        wx.CallAfter(self._apply_poll_votes, remote_jid, records, result)

    def _apply_poll_votes(self, remote_jid: str, records: list, result: dict) -> None:
        self._poll_votes_busy = False
        if not result or (self.conversation or {}).get("remoteJid", "") != remote_jid:
            return
        changed = False
        for rec in records:
            data = result.get((rec.get("key") or {}).get("id", ""))
            if data and apply_vote_result(poll_content(rec.get("message") or {}), data):
                changed = True
        if changed:
            self.refresh_messages_if_changed()
            self._reload_inline_poll()

    def _send_poll_vote(self, msg: dict, local_ids: list, on_done) -> None:
        """Cast the user's vote in the background; *on_done(ok)* runs on the
        main thread, after the new counts were asked for."""
        def work():
            ok = bool(self.main_window.send_poll_vote(msg, local_ids))
            wx.CallAfter(self._poll_vote_done, ok, on_done)
        threading.Thread(target=work, daemon=True).start()

    def _poll_vote_done(self, ok: bool, on_done) -> None:
        if ok:
            self.refresh_poll_votes(force=True)
        on_done(ok)
