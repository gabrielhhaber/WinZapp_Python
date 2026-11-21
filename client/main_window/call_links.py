"""CallLinksMixin -- part of MainWindow (see main_window/__init__.py).

WhatsApp call links (core/call_links.py): creating one from Calls (Alt+6,
ui/call_link_dialog.py), joining the call of a link, created here or received
in a message, and, for the call's admin, the people waiting to enter
(ui/call_waiting_room_dialog.py).

Joining goes through calls._start_call(call_link=...), the same path as
joining an ongoing group call: microphone, speakers, cancel and cleanup are
that path's. A link call has no chat and its id is unknown until the join
answers, so the call events that arrive before it cannot be matched to it:
the join's answer replays the call's "connected" through
on_voice_call_state_event, which is idempotent.

While the active call is a link call, its waiting room is read every few
seconds; who starts waiting is spoken once, by name, never by number.
"""

import logging
import threading

import wx

from core.call_links import (
    arrivals_announcement,
    newly_waiting,
    parse_call_link,
    waiting_jids,
    waiting_people,
    waiting_room_admin,
)

# How often the waiting room of the link call is read while it lasts.
WAITING_ROOM_POLL_MS = 4000


class CallLinksMixin:
    """Call links: create, join, and the waiting room of a link call."""

    def _call_links_supported(self) -> bool:
        """WPPConnect runs WhatsApp Web's own call-link code; the Neonize bridge
        says callLinks in /send-capabilities when its build has them."""
        if not self._uses_neonize():
            return True
        return bool(getattr(self, "_neonize_calls", False)) and bool(
            getattr(self, "_neonize_call_links", False))

    def _call_link_engine_ready(self) -> bool:
        """WPPConnect can always be asked; the Neonize bridge only says whether
        its build joins call links once its send-capabilities probe answered
        (main_window/sending.py), and asking before would say "unavailable"."""
        return not self._uses_neonize() or hasattr(self, "_neonize_call_links")

    # ── Creating ─────────────────────────────────────────────────────────────

    def create_call_link(self, is_video: bool, done):
        """Ask the engine for a new call link, off the wx thread. ``done(link,
        error)`` runs on the wx thread: the link, or "" and a speakable error."""
        if not self._call_links_supported():
            done("", self.i18n.t("call_log_action_unavailable"))
            return

        def work():
            link, error = "", ""
            try:
                response = self._raise_for_call_response(
                    self._post_call_control("link/create", {"isVideo": bool(is_video)}, timeout=30),
                    "create call link")
                body = response.json().get("response") or {}
                _, link = parse_call_link(body.get("link") if isinstance(body, dict) else "")
                if not link:
                    error = self.i18n.t("call_link_create_failed_generic")
            except Exception as exc:
                logging.exception("[call_link] creating a call link failed")
                error = self._call_error_text(exc)
            wx.CallAfter(done, link, error)

        threading.Thread(target=work, daemon=True, name="call-link-create").start()

    # ── Joining ──────────────────────────────────────────────────────────────

    def join_call_link(self, link: str, *, confirm_parent=None) -> bool:
        """Join the call of *link*; with *confirm_parent*, ask first (a link
        received in a message). False when nothing was started."""
        is_video, canonical = parse_call_link(link)
        if not canonical:
            self.output(self.i18n.t("call_link_invalid"), interrupt=True)
            return False
        if (getattr(self, "_active_voice_call", None) is not None
                or getattr(self, "_active_incoming_calls", None)):
            # said before asking "Join?": the question would end in a refusal
            self.output(self.i18n.t("voice_call_already_active"), interrupt=True)
            return False
        if confirm_parent is not None:
            question = self.i18n.t(
                "call_link_join_video_confirm" if is_video else "call_link_join_voice_confirm")
            answer = wx.MessageBox(question, self.i18n.t("call_link_title"),
                                   wx.YES_NO | wx.YES_DEFAULT | wx.ICON_QUESTION, confirm_parent)
            if answer != wx.YES:
                return False
        self._start_call("", self.i18n.t("call_link_call_name"), is_video=bool(is_video),
                         is_group=True, call_link=canonical)
        return True

    def _on_call_link_joined(self, call_record, body):
        """The join answered with the call's id: the call it opened is live
        here (or waits to be let in), say so the way a real event would."""
        if getattr(self, "_active_voice_call", None) is not call_record:
            return
        body = body if isinstance(body, dict) else {}
        call_record["call_link_admin"] = body.get("admin") is True
        call_id = str(call_record.get("call_id") or "")
        if not call_id or call_id.startswith("outgoing:"):
            return
        if body.get("waiting") is True:
            # not "connected" yet: the call's admin has to let this user in
            self.output(self.i18n.t("call_link_waiting_for_admin"), interrupt=True)
            self._sync_call_link_controls()
            return
        self.on_voice_call_state_event({
            "id": call_id, "state": "ACTIVE", "event": "state", "peerJid": "",
            # the epoch the microphone and speaker bind to: the bridge's own
            # event for it can land before the record has the call's id and be
            # dropped as another call's
            "mediaEpoch": body.get("mediaEpoch"),
            "outgoing": False, "isVideo": bool(call_record.get("is_video")), "isGroup": True,
        })
        self._sync_call_link_controls()

    def _active_call_link(self):
        """The active call record when it is a joined link call, else None."""
        active = getattr(self, "_active_voice_call", None)
        if not active or not active.get("call_link"):
            return None
        call_id = str(active.get("call_id") or "")
        return active if call_id and not call_id.startswith("outgoing:") else None

    # ── The waiting room ─────────────────────────────────────────────────────

    def _make_call_waiting_room_button(self, panel):
        """The call window's "waiting room" button, shown only in a link call."""
        button = wx.Button(panel, label=self.i18n.t("call_link_waiting_room_button"))
        button.Bind(wx.EVT_BUTTON, lambda event: self.show_call_waiting_room())
        button.Hide()
        return button

    def _sync_call_link_controls(self):
        """Keep the room read while the active call is a link call, and show
        the waiting-room button while this user is its admin (only the admin
        sees or decides who waits); stop both when it is not a link call."""
        record = self._active_call_link()
        is_admin = record is not None and bool(record.get("call_link_admin"))
        button = getattr(self, "voice_call_window_waiting_room_button", None)
        if button is not None and bool(button.IsShown()) != is_admin:
            button.Show(is_admin)
            button.GetParent().Layout()
        timer = getattr(self, "_call_link_room_timer", None)
        if record is None:
            if timer is not None:
                timer.Stop()
            self._call_link_room_call = ""
            self._call_link_waiting = []
            dialog = getattr(self, "_call_waiting_room_dialog", None)
            if dialog is not None:
                self._call_waiting_room_dialog = None
                dialog.Destroy()
            return
        if getattr(self, "_call_link_room_call", "") != record["call_id"]:
            self._call_link_room_call = record["call_id"]
            self._call_link_waiting = []
            self._call_link_announced = set()
        if timer is None:
            timer = self._call_link_room_timer = wx.Timer()
            timer.Bind(wx.EVT_TIMER, lambda event: self._poll_call_link_room())
        if not timer.IsRunning():
            timer.Start(WAITING_ROOM_POLL_MS)

    def _poll_call_link_room(self):
        if getattr(self, "_shutting_down", False):
            return
        record = self._active_call_link()
        if record is None:
            self._sync_call_link_controls()
            return
        if getattr(self, "_call_link_room_busy", False):
            return
        self._call_link_room_busy = True
        call_id = record["call_id"]

        def work():
            people, admin = None, False
            try:
                response = self._post_call_control("link/waiting-room", {"callId": call_id})
                if response.status_code < 400:
                    answer = response.json().get("response")
                    people, admin = waiting_people(answer), waiting_room_admin(answer)
            except Exception:
                logging.debug("[call_link] waiting room not read", exc_info=True)
            wx.CallAfter(self._apply_call_link_room, call_id, people, admin)

        threading.Thread(target=work, daemon=True, name="call-link-room").start()

    def _apply_call_link_room(self, call_id, people, admin):
        self._call_link_room_busy = False
        if people is None or getattr(self, "_call_link_room_call", "") != call_id:
            return
        # Each answer says whether this user is the admin now (it can change:
        # the join answer may come before the engine knows); a guest or a
        # member sees no room, hears no arrivals and cannot decide.
        record = self._active_call_link()
        if record is not None and bool(record.get("call_link_admin")) != bool(admin):
            record["call_link_admin"] = bool(admin)
            self._sync_call_link_controls()
        if not admin:
            people = []
        arrivals = newly_waiting(getattr(self, "_call_link_announced", set()), people)
        self._call_link_waiting = people
        self._call_link_announced = waiting_jids(people)
        button = getattr(self, "voice_call_window_waiting_room_button", None)
        if button is not None:
            label = (self.i18n.t("call_link_waiting_room_button_count").format(count=len(people))
                     if people else self.i18n.t("call_link_waiting_room_button"))
            if button.GetLabel() != label:
                button.SetLabel(label)
        dialog = getattr(self, "_call_waiting_room_dialog", None)
        if dialog is not None:
            dialog.set_people(people)
        if arrivals:
            self.output(arrivals_announcement(self.i18n, arrivals), interrupt=False)

    def show_call_waiting_room(self):
        """Open the waiting room of the link call (one window at a time)."""
        record = self._active_call_link()
        if record is None:
            self.output(self.i18n.t("call_link_no_waiting_room"), interrupt=True)
            return
        dialog = getattr(self, "_call_waiting_room_dialog", None)
        if dialog is not None:
            dialog.Raise()
            dialog.focus_list()
            return
        from ui.call_waiting_room_dialog import CallWaitingRoomDialog
        parent = getattr(self, "voice_call_window", None) or self
        dialog = CallWaitingRoomDialog(parent, self, record["call_id"])
        self._call_waiting_room_dialog = dialog
        dialog.set_people(getattr(self, "_call_link_waiting", []))
        dialog.Show()
        self._poll_call_link_room()

    def decide_call_link_person(self, call_id, person, admit: bool, done):
        """Let *person* in (or not), off the wx thread; ``done(ok)`` on the wx
        thread, after the result was spoken."""
        from core.call_links import person_label
        name = person_label(self.i18n, person)

        def work():
            ok = False
            try:
                self._raise_for_call_response(
                    self._post_call_control("link/admit", {
                        "callId": call_id, "jid": person["jid"], "admit": bool(admit)}),
                    "call link admit")
                ok = True
            except Exception:
                logging.exception("[call_link] waiting room decision failed")

            def finish():
                key = ("call_link_admitted" if admit else "call_link_denied") if ok \
                    else "call_link_decision_failed"
                self.output(self.i18n.t(key).format(name=name), interrupt=True)
                done(ok)
                self._poll_call_link_room()
            wx.CallAfter(finish)

        threading.Thread(target=work, daemon=True, name="call-link-admit").start()
