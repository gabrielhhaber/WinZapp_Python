"""ConversationInfoMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import wx
from core.contact_presence import cached
from ui.conversation_panel.text_helpers import _fmt_last_seen
from core.utils import (
    format_number,
    is_phone_like,
)


class ConversationInfoMixin:
    """Conversation data dialog, profile fetch and the presence note.
    """

    # ── Ctrl+Shift+D / Ctrl+Shift+P dispatch ────────────────────────────────

    def _on_ctrl_shift_d(self, event):
        """Discard voice recording if active; otherwise show conversation data."""
        if self._is_recording:
            self._discard_voice_message(event)
        elif self.conversation is not None:
            self._show_conversation_data()

    def _on_ctrl_shift_p(self, event):
        """Pause/resume recording when active; otherwise pin/unpin the
        currently focused message. Both share this one accelerator — they
        are mutually exclusive contexts (pause/resume only ever does
        anything while actively recording audio), so there is no real
        conflict in practice."""
        if self._is_recording:
            self._toggle_pause_recording(event)
            return
        index = self.messages_list.GetFirstSelected()
        if index < 0:
            index = self.messages_list.GetFocusedItem()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        self._on_menu_pin_message(msg)

    # ── Conversation / group data ────────────────────────────────────────────

    def _show_conversation_data(self, event=None, chat=None):
        target = chat if chat is not None else self.conversation
        if target is None:
            return
        from ui.dialogs.conversation_data_dialog import ConversationDataDialog
        dlg = ConversationDataDialog(self.main_window, target)
        dlg.ShowModal()
        dlg.Destroy()

    def _fetch_and_update_profile(self, conversation: dict, visit=None):
        """
        Background: fetch contact profile / group info and update the
        conversation-data button note with a last-seen or group-size string.

        For private chats the note comes from the _presence_cache (populated
        by presence.update WebSocket events) rather than from fetchProfile,
        because the WPPConnect API's fetchProfile response does not include
        lastSeen or online fields.
        """
        jid      = conversation.get("remoteJid", "")
        mw       = self.main_window
        i18n     = mw.i18n
        
        if visit is None:
            visit = getattr(self, "_contact_presence_visit", 0)
        if not jid.endswith("@g.us"):
            # No captured note, blocking last-seen fetch, or mapping query.
            # The active-contact worker owns those requests and renders live data.
            def refresh():
                if visit == getattr(self, "_contact_presence_visit", 0):
                    mw._refresh_open_contact_presence_note()
            wx.CallAfter(refresh)
            return

        note = (
            mw._resolve_contact_name(conversation)
            or mw.find_name_through_messages(conversation)
            or conversation.get("name", "")
            or conversation.get("pushName", "")
            or format_number(jid)
        )
        try:
            if jid.endswith("@g.us"):
                data = mw.get_group_info_recent(jid)
                # "size" may be absent in some WPPConnect API builds; fall back to
                # counting the participants list which is always present.
                participants = data.get("participants", [])
                size = data.get("size") or len(participants)
                group_name = (
                    mw._resolve_contact_name(conversation)
                    or mw.find_name_through_messages(conversation)
                    or conversation.get("name", "")
                    or conversation.get("pushName", "")
                    or format_number(jid)
                )
                note = f"{group_name}, {i18n.t('group_size').format(count=size)}"
        except Exception:
            pass

        def _update():
            if (visit == getattr(self, "_contact_presence_visit", 0)
                    and self.conversation is not None
                    and self.conversation.get("remoteJid") == jid):
                try:
                    display_note = note
                    if not jid.endswith("@g.us") and is_phone_like(display_note):
                        display_note = f"{i18n.t('phone_label')}: {display_note}"
                    self._conv_data_btn.SetNote(display_note)
                    self.conversation_panel.Layout()
                except Exception:
                    pass

        wx.CallAfter(_update)

    def _refresh_presence_note(self, canonical_jid: str):
        """
        Called on the main thread by on_presence_update when a presence.update
        arrives for the currently open conversation.  Updates the button note
        immediately without going through the background-fetch path.
        """
        if self.conversation is None:
            return
        mw    = self.main_window
        i18n  = mw.i18n
        jid = self.conversation.get("remoteJid", "")
        if jid.endswith("@g.us"):
            return
        presence = cached(mw, jid)
        lkp = presence.get("lastKnownPresence", "")
        last_seen = presence.get("lastSeen")
        # Default note stays as the contact name
        note = (
            mw._resolve_contact_name(self.conversation)
            or mw.find_name_through_messages(self.conversation)
            or self.conversation.get("name", "")
            or self.conversation.get("pushName", "")
            or format_number(jid)
        )

        if lkp in ("available", "composing", "recording"):
            note = i18n.t("online_status")
        elif last_seen:
            ls_str = _fmt_last_seen(last_seen, i18n)
            if ls_str:
                note = ls_str

        try:
            display_note = note
            if not jid.endswith("@g.us") and is_phone_like(display_note):
                display_note = f"{i18n.t('phone_label')}: {display_note}"
            if self._conv_data_btn.GetNote() != display_note:
                self._conv_data_btn.SetNote(display_note)
                self.conversation_panel.Layout()
        except Exception:
            pass
