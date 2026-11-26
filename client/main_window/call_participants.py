"""Explicit participant views for the active call and the Calls tab."""
from core.call_log import call_log_participants
from core.call_log_participants import (
    is_upgraded_call,
    order_call_participants,
    other_call_participants,
    participants_title,
)
from core.call_matching import is_placeholder_call_id
from core.ongoing_calls import is_own_chat_call
from core.call_participants import active_participants_available, held_here
from core.jid_device import strip_device_suffix
from core.call_roster import note_seen, remember, user_of
from core.unsaved_contact_marker import is_unsaved_contact


class CallParticipantsMixin:
    def call_log_participants_text(self, msg, chat_jid="") -> str:
        """"A, B e mais 3 pessoas" for a call record of a one-to-one chat that
        became a group call, as the phone names it; "" for any other call
        (core/call_log_participants.py)."""
        jids = call_log_participants(msg)
        if not jids:
            return ""
        key = (msg.get("key") or {}) if isinstance(msg, dict) else {}
        chat_jid = str(chat_jid or key.get("remoteJid") or "")
        mine = {user_of(j) for j in (getattr(self, "my_jid", ""), getattr(self, "my_lid", ""))} - {""}
        person = lambda jid: CallParticipantsMixin._call_person(self, jid)
        others = other_call_participants(jids, mine, person)
        if not is_upgraded_call(chat_jid, others):
            return ""
        # whoever called this account: the chat's other end, unless it was
        # this account calling (WhatsApp sorts that person first)
        caller = "" if key.get("fromMe") else chat_jid
        ordered = order_call_participants(
            others, caller, lambda jid: not is_unsaved_contact(self, jid), person)
        names = [self.chat_display_name({"remoteJid": jid, "name": ""}) for jid in ordered]
        return participants_title(names, self.i18n.t)

    def ongoing_call_people_text(self, ongoing) -> str:
        """"A, B e mais 3 pessoas" for a live call (core/ongoing_calls.py)
        named after the people connected to it, as the phone names it: one
        filed under this account's own chat (it started the call on its
        phone), or a one-to-one call that became a group call. "" for any
        other call, which keeps its chat's name."""
        if not isinstance(ongoing, dict) or not ongoing.get("people"):
            return ""
        chat_jid = str(ongoing.get("chatJid") or "")
        mine = {user_of(j) for j in (getattr(self, "my_jid", ""), getattr(self, "my_lid", ""))} - {""}
        person = lambda jid: CallParticipantsMixin._call_person(self, jid)
        others = other_call_participants(ongoing["people"], mine, person)
        own = is_own_chat_call(ongoing, self._is_self_jid)
        if not others or not (own or is_upgraded_call(chat_jid, others)):
            return ""
        # whoever started the call first, as for a call record: the chat's
        # person, unless this account started it
        ordered = order_call_participants(
            others, "" if own else chat_jid, lambda jid: not is_unsaved_contact(self, jid), person)
        names = [self.chat_display_name({"remoteJid": jid, "name": ""}) for jid in ordered]
        return participants_title(names, self.i18n.t)

    def _show_active_call_participants(self, _event=None):
        active = getattr(self, "_active_voice_call", None) or {}
        if not active_participants_available(active, getattr(self, "_voice_call_last_announced_state", "")):
            return
        self.show_call_participants(active.get("call_id"), active.get("peer_jid"),
                                    parent=getattr(self, "voice_call_window", None))

    def show_call_participants(self, call_id, chat_jid, *, parent=None, title=None, people=()):
        """*title* names a call filed under the own chat after its people;
        a lock on *chat_jid* or on any of *people* closes the window."""
        if not call_id or is_placeholder_call_id(call_id) or getattr(self, "_shutting_down", False):
            return
        jids = [chat_jid, *people]
        allowed = lambda: not any(
            getattr(self, "is_chat_locked", lambda _jid: False)(jid) for jid in jids)
        if not allowed():
            # The words the dialog itself uses for a call it may not show;
            # they do not reveal that the chat is locked.
            self.output(self.i18n.t("call_participants_ended"), interrupt=True)
            return
        from ui.dialogs.call_participants import CallParticipantsDialog
        dialog = CallParticipantsDialog(parent or self, self, call_id,
                                        title or self.chat_display_name(chat_jid), allowed,
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
        joined, left = remember(rosters, call_id, connected, mine,
                                lambda jid: CallParticipantsMixin._call_person(self, jid))
        # everyone ever connected, for the "left" rows of the participants list
        note_seen(self.__dict__.setdefault("_call_seen", {}), call_id,
                  [(jid, "") for jid in connected], mine)
        if self._call_held_here(call_id):
            if len(rosters[call_id]) >= 2:
                refresh = getattr(self, "_sync_voice_call_bar", None)
                if refresh is not None:
                    refresh()
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

    def _call_person(self, jid):
        """The one address a call's participant is compared by: the number of
        an @lid this account has a mapping for, the address itself otherwise
        (core.call_roster.person_key)."""
        # the mapping is keyed without the companion-device part ("7:4@lid")
        jid = strip_device_suffix(str(jid or ""))
        return getattr(self, "_lid_to_phone", {}).get(jid) or jid

    def _call_held_here(self, call_id):
        """True while *call_id* is the call this device is in, live: its
        participants window follows the participant events and needs no
        timed refresh."""
        return held_here(getattr(self, "_active_voice_call", None), call_id=call_id,
                         state=getattr(self, "_voice_call_last_announced_state", ""))

    def _participants_dialog_follow_call(self):
        """The call an open participants window follows on events is no
        longer held here (it ended, or this device left it): no event will
        come for it any more, so the window asks once and shows the outcome,
        and goes back to refreshing on its own if the call goes on."""
        dialog = getattr(self, "_call_participants_dialog", None)
        if (dialog is not None and getattr(dialog, "_follows_events", False)
                and not self._call_held_here(getattr(dialog, "_call_id", None))):
            dialog._follows_events = False
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
