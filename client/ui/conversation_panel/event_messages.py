"""EventMessagesMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

WhatsApp events in a conversation: creating one (attachment menu -> EventDialog),
opening one (Enter on its row -> EventDetailsDialog: what, when, where, who goes,
and the answer, edit and cancel buttons), and keeping the answers on its row
(asked of the engine in the background, like a poll's votes, and again when an
``event-changed`` socket event says one arrived). The rules are core/events.py.
"""

import logging
import threading
import time

import wx

from core import events as ev
from core.group_invitation import connection_identity
from ui.conversation_panel.channel_conversation import refuse_in_channel


class EventMessagesMixin:
    """Creating, opening and answering WhatsApp events."""

    #: Seconds between two background reads of the open conversation's answers.
    _EVENT_RESPONSES_MIN_INTERVAL = 10
    _EVENT_RESPONSES_MAX = 10

    # ── Creating ────────────────────────────────────────────────────────────

    def _on_attach_event(self, event=None):
        if refuse_in_channel(self, "event"):
            return
        remote_jid = (self.conversation or {}).get("remoteJid", "")
        if not remote_jid:
            return
        from ui.dialogs.event_dialog import EventDialog
        dlg = EventDialog(self.main_window)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            fields = dlg.fields
        finally:
            dlg.Destroy()
        self._run_event_action(lambda c: ev.create(c, remote_jid, fields),
                               "event_created", "event_create_failed")

    # ── Opening ─────────────────────────────────────────────────────────────

    def _show_event_dialog(self, msg: dict) -> None:
        """Enter on an event row."""
        from ui.dialogs.event_dialog import EventDetailsDialog
        self.refresh_event_responses(force=True)
        dlg = EventDetailsDialog(self.main_window, msg, self._event_person_name)
        try:
            answer = dlg.ShowModal()
            choice = dlg.choice
        finally:
            dlg.Destroy()
        if answer != wx.ID_OK or not choice:
            return
        kind, value = choice
        if kind == "respond":
            self._send_event_response(msg, value)
        elif kind == "edit":
            self._edit_event(msg)
        elif kind == "cancel":
            self._cancel_event(msg)

    def _event_person_name(self, sender: str) -> str:
        """Who answered, under the same name a sender is shown with here."""
        remote_jid = (self.conversation or {}).get("remoteJid", "")
        return self._sender_label({"key": {
            "fromMe": False, "remoteJid": remote_jid,
            "participant": sender if remote_jid.endswith("@g.us") else ""}})

    def _event_message_id(self, msg: dict) -> str:
        key = msg.get("key") or {}
        return self.main_window._serialize_msg_id(key.get("remoteJid", ""), key, msg) or ""

    def _send_event_response(self, msg: dict, response: int) -> None:
        message_id = self._event_message_id(msg)
        if not message_id:
            return
        self._run_event_action(lambda c: ev.respond(c, message_id, response),
                               "event_response_sent", "event_response_failed",
                               refresh=True)

    def _edit_event(self, msg: dict) -> None:
        from ui.dialogs.event_dialog import EventDialog
        message_id = self._event_message_id(msg)
        if not message_id:
            return
        dlg = EventDialog(self.main_window, ev.event_content(msg.get("message") or {}))
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            fields = dlg.fields
        finally:
            dlg.Destroy()
        self._run_event_action(lambda c: ev.edit(c, message_id, fields),
                               "event_edited", "event_edit_failed")

    def _cancel_event(self, msg: dict) -> None:
        i18n = self.main_window.i18n
        message_id = self._event_message_id(msg)
        if not message_id:
            return
        if wx.MessageBox(i18n.t("event_cancel_confirm"), i18n.t("event_label"),
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
            return
        self._run_event_action(lambda c: ev.cancel(c, message_id),
                               "event_canceled_done", "event_cancel_failed")

    def _run_event_action(self, work, done_key, failed_key, refresh=False) -> None:
        """Run one request off the wx thread and say how it went."""
        mw = self.main_window
        connection = connection_identity(mw)

        def run():
            reason = ""
            try:
                work(connection)
                ok = True
            except Exception as exc:
                logging.warning("[events] %s failed: %s", done_key, exc)
                ok, reason = False, str(exc)
            wx.CallAfter(finished, ok, reason)

        def finished(ok, reason=""):
            # the Neonize engine cannot make a call link (neonize_bridge/events.py)
            key = done_key if ok else (
                "event_call_unsupported" if reason == "event_call_unsupported" else failed_key)
            mw.output(mw.i18n.t(key), interrupt=True)
            if ok and refresh:
                self.refresh_event_responses(force=True)

        threading.Thread(target=run, name="event-action", daemon=True).start()

    # ── Answers ─────────────────────────────────────────────────────────────

    def _open_event_records(self) -> list:
        container = (self.conversation or {}).get("messages")
        inner = container.get("messages") if isinstance(container, dict) else None
        records = inner.get("records") if isinstance(inner, dict) else None
        found = [rec for rec in (records or []) if isinstance(rec, dict)
                 and rec.get("messageType") == ev.EVENT_MESSAGE_TYPE
                 and not rec.get("_local_pending") and (rec.get("key") or {}).get("id")]
        return found[-self._EVENT_RESPONSES_MAX:]

    def refresh_event_responses(self, force: bool = False) -> None:
        """Ask the engine for the answers to the open conversation's events, in
        the background; the rows repaint only if what they say changed."""
        if getattr(self, "_event_responses_busy", False):
            return
        if (not force and time.monotonic() - getattr(self, "_event_responses_at", 0.0)
                < self._EVENT_RESPONSES_MIN_INTERVAL):
            return
        records = self._open_event_records()
        remote_jid = (self.conversation or {}).get("remoteJid", "")
        if not records or not remote_jid:
            return
        ids = {}
        for rec in records:
            serialized = self._event_message_id(rec)
            if serialized:
                ids[serialized] = rec["key"]["id"]
        if not ids:
            return
        self._event_responses_busy = True
        self._event_responses_at = time.monotonic()
        connection = connection_identity(self.main_window)

        def run():
            try:
                data = ev.read_responses(connection, list(ids))
            except Exception as exc:
                logging.info("[events] answers not read: %s", exc)
                data = {}
            by_id = {ids[k]: v for k, v in data.items() if k in ids}
            wx.CallAfter(self._apply_event_responses, remote_jid, records, by_id)

        threading.Thread(target=run, name="event-responses", daemon=True).start()

    def _apply_event_responses(self, remote_jid: str, records: list, by_id: dict) -> None:
        self._event_responses_busy = False
        if not by_id or (self.conversation or {}).get("remoteJid", "") != remote_jid:
            return
        changed = False
        for rec in records:
            data = by_id.get(rec["key"]["id"])
            if data and ev.apply_responses(ev.event_content(rec.get("message") or {}), data):
                changed = True
        if changed:
            self.refresh_messages_if_changed()

    def on_event_changed(self, message_id: str, kind: str) -> None:
        """An answer arrived, or the event was edited or canceled (socket
        ``event-changed``; main thread). Only the open conversation's events
        matter: the others are read when their conversation opens."""
        record = next((rec for rec in self._open_event_records()
                       if rec["key"]["id"] == message_id), None)
        if record is None:
            return
        if kind == "edited":
            self._reread_event(record)
        self.refresh_event_responses(force=True)

    def _reread_event(self, record: dict) -> None:
        """An edit or a cancel: read the event message again and keep its new
        fields (its answers stay), then repaint and save."""
        message_id = self._event_message_id(record)
        remote_jid = (self.conversation or {}).get("remoteJid", "")
        if not message_id or not remote_jid:
            return
        connection = connection_identity(self.main_window)

        def run():
            try:
                fresh = ev.read_event(connection, message_id)
            except Exception as exc:
                logging.info("[events] edited event not read: %s", exc)
                fresh = {}
            wx.CallAfter(apply, fresh)

        def apply(fresh):
            if ev.apply_edit(ev.event_content(record.get("message") or {}), fresh):
                self.main_window._schedule_save(dirty_jid=remote_jid)
                if (self.conversation or {}).get("remoteJid", "") == remote_jid:
                    self.refresh_messages_if_changed()

        threading.Thread(target=run, name="event-edited", daemon=True).start()
