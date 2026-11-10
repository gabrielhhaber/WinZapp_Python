"""Explicit participant views for the active call and the Calls tab."""
from core.call_matching import is_placeholder_call_id
from core.call_participants import active_participants_available
from core.call_roster import remember, user_of


class CallParticipantsMixin:
    def _show_active_call_participants(self, _event=None):
        active = getattr(self, "_active_voice_call", None) or {}
        if not active_participants_available(active, getattr(self, "_voice_call_last_announced_state", "")):
            return
        self.show_call_participants(active.get("call_id"), active.get("peer_jid"),
                                    parent=getattr(self, "voice_call_window", None))

    def show_call_participants(self, call_id, chat_jid, *, parent=None):
        if not call_id or is_placeholder_call_id(call_id) or getattr(self, "_shutting_down", False):
            return
        allowed = lambda: not getattr(self, "is_chat_locked", lambda _jid: False)(chat_jid)
        if not allowed():
            # The words the dialog itself uses for a call it may not show;
            # they do not reveal that the chat is locked.
            self.output(self.i18n.t("call_participants_ended"), interrupt=True)
            return
        from ui.dialogs.call_participants import CallParticipantsDialog
        dialog = CallParticipantsDialog(parent or self, self, call_id,
                                        self.chat_display_name(chat_jid), allowed,
                                        invite_chat=self._call_invite_chat(call_id, chat_jid))
        self._call_participants_dialog = dialog     # for on_call_participants
        try:
            dialog.ShowModal()
        finally:
            self._call_participants_dialog = None
            dialog._dispose_participants()
            dialog.Destroy()

    def on_call_participants(self, call_id, connected):
        """A group call's connected people changed: say who joined or left
        (Settings > Calls, both on by default) when it is the call held here,
        and refresh its participants dialog if one is open."""
        rosters = self.__dict__.setdefault("_call_rosters", {})
        mine = {user_of(j) for j in (getattr(self, "my_jid", ""), getattr(self, "my_lid", ""))} - {""}
        joined, left = remember(rosters, call_id, connected, mine)
        active = getattr(self, "_active_voice_call", None) or {}
        here = (active.get("call_id") == call_id and active_participants_available(
            active, getattr(self, "_voice_call_last_announced_state", "")))
        if here:
            calls = (getattr(self, "settings", {}) or {}).get("calls", {})
            name = lambda jid: self.chat_display_name({"remoteJid": jid, "name": ""})
            if calls.get("announce_participant_joined", True):
                for jid in joined:
                    self.output(self.i18n.t("call_participant_joined").format(name=name(jid)))
            if calls.get("announce_participant_left", True):
                for jid in left:
                    self.output(self.i18n.t("call_participant_left").format(name=name(jid)))
        dialog = getattr(self, "_call_participants_dialog", None)
        if dialog is not None and getattr(dialog, "_call_id", None) == call_id:
            dialog._refresh_participants()

    def _call_invite_chat(self, call_id, chat_jid):
        """The chat of *call_id* when people may be added to it: only the call
        this device is in now, group or private (core/call_invites.py); ''
        otherwise."""
        active = getattr(self, "_active_voice_call", None) or {}
        if not active_participants_available(active, getattr(self, "_voice_call_last_announced_state", "")):
            return ""
        chat = str(active.get("peer_jid") or chat_jid or "")
        if active.get("call_id") != call_id or not chat.endswith(("@g.us", "@lid", "@s.whatsapp.net", "@c.us")):
            return ""
        return chat
