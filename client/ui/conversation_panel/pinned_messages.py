"""Pinned-message overview, visit-scoped reads and explicit history navigation."""

import logging
import wx
from core.conversation_view import conversation_in_view


class PinnedMessagesMixin:
    def _init_pinned_messages_button(self, sizer):
        self._pinned_messages = []
        self._pinned_visit = None
        self._pinned_request = None
        self._pinned_snapshot_known = False
        self._pinned_writes = set()
        self._pinned_deferred_read = {}
        self._pinned_messages_btn = wx.Button(
            self.conversation_panel,
            label=self.main_window.i18n.t("pinned_messages_title"),
        )
        self._pinned_messages_btn.Bind(wx.EVT_BUTTON, self._on_show_pinned_messages)
        sizer.Add(self._pinned_messages_btn, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 5)

    def _begin_pinned_messages_visit(self, *, reset_history=True):
        # A fresh identity also rejects A -> B -> A responses from the first visit.
        self._pinned_visit = object()
        self._pinned_request = None
        self._pinned_snapshot_known = False
        self._pinned_messages = []
        self._pinned_deferred_read = {}
        if reset_history:
            self._pinned_extra_history_ids = set()
        self._update_pinned_messages_button()

    def _update_pinned_messages_button(self):
        button = getattr(self, "_pinned_messages_btn", None)
        if button is None:
            return
        i18n = self.main_window.i18n
        jid = (self.conversation or {}).get("remoteJid")
        button.Enable(not any(job[0] == jid for job in getattr(self, "_pinned_writes", ())))
        label = (i18n.t("pinned_messages_count").format(count=len(self._pinned_messages))
                 if self._pinned_snapshot_known else i18n.t("pinned_messages_title"))
        if button.GetLabel() != label:
            button.SetLabel(label)
            self.conversation_panel.Layout()

    def _load_pinned_messages(self, *, announce=False, show=False):
        if not self.conversation:
            return
        jid = self.conversation.get("remoteJid", "")
        if any(job[0] == jid for job in getattr(self, "_pinned_writes", ())):
            pending = self._pinned_deferred_read
            self._pinned_deferred_read = {
                "announce": announce or pending.get("announce", False),
                "show": show or pending.get("show", False),
            }
            return
        visit = getattr(self, "_pinned_visit", None)
        request = self._pinned_request = object()

        if jid.endswith("@newsletter"):
            # A channel's pins are server ids of its updates, read with its role
            # (channel_conversation.py).
            def read_channel():
                channel = self._read_channel_state(jid)
                wx.CallAfter(self._finish_channel_pins_read, jid, visit, request, channel,
                             announce, show)

            self.main_window._msg_bg_executor.submit(read_channel)
            return

        def read():
            try:
                messages = self.main_window.get_pinned_messages(jid)
            except Exception:
                # Do not log the exception: HTTP URLs contain credentials.
                logging.warning("[pinned-messages] could not read the linked device's pins")
                messages = None
            wx.CallAfter(self._finish_pinned_messages_read, jid, visit, request,
                         messages, announce, show)

        self.main_window._msg_bg_executor.submit(read)

    def _finish_channel_pins_read(self, jid, visit, request, channel, announce, show):
        if (getattr(self.main_window, "_shutting_down", False)
                or visit is not self._pinned_visit or request is not self._pinned_request
                or (self.conversation or {}).get("remoteJid") != jid):
            return
        self._finish_pinned_messages_read(jid, visit, request,
                                          self._channel_pins_from(jid, channel), announce, show)

    def _finish_pinned_messages_read(self, jid, visit, request, messages, announce, show):
        if (getattr(self.main_window, "_shutting_down", False)
                or visit is not self._pinned_visit or request is not self._pinned_request
                or (self.conversation or {}).get("remoteJid") != jid):
            return
        visible = conversation_in_view(self)
        i18n = self.main_window.i18n
        if messages is None:
            if show and visible:
                self.main_window.output(i18n.t("pinned_messages_failed"))
            return
        self._pinned_messages = messages
        self._pinned_snapshot_known = True
        changed = self._apply_pinned_message_flags()
        self._update_pinned_messages_button()
        if changed:
            self._repaint_or_repopulate(changed)
        if not visible:
            return
        if show:
            self._show_pinned_messages_picker()
        elif announce and messages:
            self.main_window.output(
                i18n.t("pinned_messages_notice").format(count=len(messages)))

    def _apply_pinned_message_flags(self):
        """Reapply the authoritative snapshot after a background history refresh."""
        if not getattr(self, "_pinned_snapshot_known", False) or not self.conversation:
            return []
        pins = {(m.get("key") or {}).get("id") for m in self._pinned_messages}
        records = self.conversation.get("messages", {}).get("messages", {}).get("records", [])
        changed = set()
        materialized = list(records) + list(getattr(self, "_sorted_messages", []))
        for message in materialized:
            mid = (message.get("key") or {}).get("id")
            if not mid:
                continue
            pinned = mid in pins
            if bool(message.get("pinInChat")) != pinned:
                message["pinInChat"] = pinned
                changed.add(mid)
        return sorted(changed)

    def _pinned_message_state_changed(self, message, jid=None):
        self._pinned_messages_state_changed([message], jid)

    def _pinned_messages_state_changed(self, messages, jid=None):
        """Keep optimistic actions and rollback in step with the overview."""
        if (not hasattr(self, "_pinned_messages")
                or (jid is not None and (self.conversation or {}).get("remoteJid") != jid)):
            return
        self._pinned_request = None  # a read predating this action is stale
        pins = {(m.get("key") or {}).get("id"): m for m in self._pinned_messages}
        for message in messages:
            mid = (message.get("key") or {}).get("id")
            if mid and message.get("pinInChat"):
                pins[mid] = message
            else:
                pins.pop(mid, None)
        self._pinned_messages = list(pins.values())
        self._update_pinned_messages_button()

    def _begin_pinned_message_write(self, jid):
        job = (jid, object())
        if not hasattr(self, "_pinned_writes"):
            self._pinned_writes = set()
        self._pinned_writes.add(job)
        self._pinned_request = None
        self._update_pinned_messages_button()
        return job

    def _finish_pinned_message_write(self, job):
        self._pinned_writes.discard(job)
        if getattr(self.main_window, "_shutting_down", False):
            return
        self._update_pinned_messages_button()
        if (getattr(self, "_pinned_visit", None) is not None
                and (self.conversation or {}).get("remoteJid") == job[0]
                and not any(item[0] == job[0] for item in self._pinned_writes)):
            pending = self._pinned_deferred_read
            self._pinned_deferred_read = {}
            self._load_pinned_messages(**pending)

    def _on_show_pinned_messages(self, event):
        self._load_pinned_messages(show=True)

    def _show_pinned_messages_picker(self):
        i18n = self.main_window.i18n
        messages = list(self._pinned_messages)
        if not messages:
            self.main_window.output(i18n.t("pinned_messages_empty"))
            return
        visit = self._pinned_visit
        choices = [self._render_message_line(m).replace("\r", " ").replace("\n", " ")
                   for m in messages]
        dialog = wx.SingleChoiceDialog(
            self, i18n.t("pinned_messages_choose"),
            i18n.t("pinned_messages_title"), choices)
        try:
            result = dialog.ShowModal()
            selected = dialog.GetSelection()
        finally:
            dialog.Destroy()
        if result == wx.ID_OK and visit is self._pinned_visit and 0 <= selected < len(messages):
            self._jump_to_pinned_message(messages[selected])

    def _jump_to_pinned_message(self, message):
        mid = (message.get("key") or {}).get("id")
        if not mid:
            return
        index = self._find_index_by_msg_id(mid)
        if index < 0:
            # Keep existing content/star/media metadata when the message is already resident.
            records = self.conversation.get("messages", {}).get("messages", {}).get("records", [])
            if not any((m.get("key") or {}).get("id") == mid for m in records):
                self._merge_history_into_records([message])
                # A single old pin is not a contiguous page from the local DB.
                # Do not let the history loader count it as a consumed DB row.
                self._pinned_extra_history_ids.add(mid)
            self._pinned_jump_id = mid
            try:
                self.populate_messages(preserve_focus=True)
            finally:
                self._pinned_jump_id = ""
            index = self._find_index_by_msg_id(mid)
        if index >= 0:
            self._focus_message_row(index)
