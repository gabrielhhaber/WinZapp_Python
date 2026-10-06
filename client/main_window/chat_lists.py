"""Account-scoped WhatsApp lists; one bounded background request at a time."""

import threading
import wx

from core.chat_lists import ListSnapshot, list_identity, valid_chat_id
from core.chat_list_transport import ListResult, request_lists


class WhatsAppListsMixin:
    def _wa_lists_context(self):
        return (
            getattr(self, "_wa_lists_generation", 0),
            getattr(self, "account_id", None), getattr(self, "my_jid", ""),
            self.wpp_server, self.wpp_port, self.token,
        )

    def _wa_lists_state(self):
        if getattr(self, "_wa_lists_snapshot_context", None) != self._wa_lists_context():
            return ListSnapshot()
        return getattr(self, "_wa_lists_snapshot", ListSnapshot())

    def _invalidate_wa_lists(self):
        self._wa_lists_generation = getattr(self, "_wa_lists_generation", 0) + 1
        self._wa_lists_snapshot = ListSnapshot()
        self._wa_lists_snapshot_context = None
        panel = getattr(self, "conversations_panel", None)
        if panel is not None and hasattr(panel, "_refresh_wa_list_choices"):
            wx.CallAfter(panel._refresh_wa_list_choices)
        dialog = getattr(self, "_wa_lists_dialog", None)
        if dialog is not None:
            wx.CallAfter(dialog.invalidate_list_manager)

    def _wa_list_filter_identities(self):
        list_id = getattr(self.conversations_panel, "_wa_list_id", None)
        item = self._wa_lists_state().find(list_id)
        mapping = dict(getattr(self, "_lid_to_phone", {}))
        return {list_identity(jid, mapping) for jid in item.members} if item else set()

    def _wa_list_candidates(self):
        """Names already safe for the main/archived lists, never the vault."""
        result, seen = [], set()
        mapping = dict(getattr(self, "_lid_to_phone", {}))
        for panel_name in ("conversations_panel", "archived_conversations_panel"):
            panel = getattr(self, panel_name, None)
            if panel is None:
                continue
            chats = getattr(panel, "_all_chats_list", panel.chats_list)
            names = getattr(panel, "_all_chat_names", panel.chat_names)
            for chat, name in zip(chats, names):
                jid = chat.get("remoteJid", "")
                identity = list_identity(jid, mapping)
                if not identity or identity in seen or self.is_chat_locked(jid):
                    continue
                seen.add(identity)
                result.append((jid, name))
        return sorted(result, key=lambda row: row[1].casefold())

    def _wa_list_member_command(self, item, action, selected):
        """Only visible selected chats change; unknown/locked members survive."""
        mapping = dict(getattr(self, "_lid_to_phone", {}))
        candidates = {jid for jid, _name in self._wa_list_candidates()}
        if not selected or not set(selected) <= candidates:
            return None
        targets = []
        for jid in selected:
            matched = sorted(member for member in item.members
                             if list_identity(member, mapping) == list_identity(jid, mapping))
            if action == "removeChats":
                targets.extend(matched)
            elif action == "addChats" and not matched:
                targets.append(self._resolve_jid_for_send(jid))
        targets = list(dict.fromkeys(targets))
        if not targets or len(targets) > 500 or not all(valid_chat_id(jid) for jid in targets):
            return None
        return {"action": action, "id": item.id, "chatIds": targets}

    def _request_wa_lists(self, done, command=None):
        """Called on UI thread. Late replies cannot cross logout/reset/token change."""
        if getattr(self, "_wa_lists_job", None) is not None:
            done(ListResult(None, "busy"))
            return
        if (getattr(self, "_shutting_down", False)
                or getattr(self, "offline_mode", False) or not self.token):
            done(ListResult(None, "failed"))
            return
        snapshot = self._wa_lists_state()
        if command is not None:
            if not snapshot.can_edit:
                done(ListResult(None, "refused"))
                return
            if command["action"] in ("addChats", "removeChats"):
                mapping = dict(getattr(self, "_lid_to_phone", {}))
                visible = {list_identity(jid, mapping) for jid, _name in self._wa_list_candidates()}
                if any(list_identity(jid, mapping) not in visible for jid in command["chatIds"]):
                    done(ListResult(None, "refused"))
                    return
        context, job = self._wa_lists_context(), object()
        self._wa_lists_job = job
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/custom-lists"
        headers = {"Authorization": f"Bearer {self.token}"}

        def finish(result):
            if getattr(self, "_wa_lists_job", None) is job:
                self._wa_lists_job = None
            if getattr(self, "_shutting_down", False):
                return
            if context != self._wa_lists_context():
                done(ListResult(None, "cancelled"))
                return
            if result.snapshot is not None:
                self._wa_lists_snapshot = result.snapshot
                self._wa_lists_snapshot_context = context
                self.conversations_panel._refresh_wa_list_choices()
            elif result.outcome not in ("busy", "cancelled"):
                # Keep the selected cached filter, but require a successful
                # read before another write after a failed/unconfirmed read.
                self._wa_lists_snapshot = ListSnapshot(self._wa_lists_state().lists, False)
                self._wa_lists_snapshot_context = context
            done(result)

        def worker():
            try:
                if context != self._wa_lists_context() or getattr(self, "_shutting_down", False):
                    result = ListResult(None, "cancelled")
                elif command is not None and any(
                    self.is_chat_locked(jid) for jid in command.get("chatIds", [])
                ):
                    result = ListResult(None, "refused")
                else:
                    result = request_lists(url, headers, command)
            except Exception:
                # Release the job even on an unexpected guard/transport error.
                # Never expose native exception text or repeat a possible write.
                result = ListResult(None, "unconfirmed" if command else "failed")
            wx.CallAfter(finish, result)

        try:
            threading.Thread(target=worker, daemon=True, name="whatsapp-lists").start()
        except Exception:
            finish(ListResult(None, "failed"))
